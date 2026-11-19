"""Comment ownership, schema upgrade, and proposal settlement for people-circles."""

import uuid

from circle_report import (
    cell, check, comment_line, delete_line, fail, inbox_line, mark, require_absent, saw, status_text,
    views_line, wait, wait_pages,
)
from people_app import observe_access, owned_item, pick, read_records, sdk, write_owned
from people_log_views import inbox, pending_positions, published_views
from support import OperationError, summary


def _recipe():
    import people_circles as recipe
    return recipe


def standard_comment(database, definition, key, text, author):
    recipe = _recipe()
    update = recipe.mutation(recipe.STANDARD_COMMENT, definition, key, text)
    update["key_value"] = {"hash": "seed-album", "range": key}
    update["record"] = {
        "target": {"database": database, "schema": recipe.ALBUM, "key": "seed"},
        "author_id": author,
        "created_at": "2026-11-14T00:00:00Z",
        "social_comment_text": text,
    }
    return update


def standard_rows(node, database):
    recipe = _recipe()
    rows = read_records(node, database, recipe.STANDARD_COMMENT, [
        {"hash": "seed-album", "range": "publisher"}, {"hash": "seed-album", "range": "receiver"},
    ])
    return {row["key"]["range"]: row for row in rows}


def texts(rows):
    found = {}
    if not isinstance(rows, dict):
        return found
    for key, row in rows.items():
        fields = row.get("fields") if isinstance(row, dict) else None
        value = fields.get("social_comment_text") if isinstance(fields, dict) else None
        if isinstance(value, str):
            found[key] = value
    return found


def replace_comment(update, row, text):
    return {
        **update, "mutation_type": "replace", "expected": {"type": "revision", "revision": row["revision"]},
        "record": {**row["fields"], "social_comment_text": text},
    }


def member_author(row, node_key, author_field, code):
    fields = row.get("fields") if isinstance(row, dict) else None
    author = fields.get("author_id") if isinstance(fields, dict) else None
    match = (isinstance(row, dict) and row.get("created_by") == node_key
             and row.get("updated_by") == node_key and author == author_field)
    check(match, code, "%s author_id=%s" % (cell("own", row, "social_comment_text"), mark(author)))


def refuse_created_by(node, database, definition):
    update = standard_comment(database, definition, "forged-author", "Refused", "spoofed-author")
    inside, named = owned_item(database, update), owned_item(database, update)
    inside["record"] = {**inside["record"], "created_by": "forged"}
    named["created_by"] = "forged"
    for item in (inside, named):
        try:
            node.call("/v1/write", {"mutations": [item]})
        except OperationError as error:
            status = (error.socket_refusal or {}).get("status")
            check(status == 400, "created_by_write_refused", saw("write", status=status))
        else:
            fail("created_by_write_refused", saw("write", accepted=1))


def exercise_comment_ownership(publisher, recipient, database, definitions, scope, reader):
    recipe = _recipe()
    definition = definitions[recipe.STANDARD_COMMENT]
    owner_comment = standard_comment(database, definition, "publisher", "Publisher wrote this", reader)
    mine = standard_comment(database, definition, "receiver", "Ana wrote this", "spoofed-author")
    refuse_created_by(publisher, database, definition)
    write_owned(publisher, database, owner_comment)
    queue_and_restart(publisher, recipient, database, scope, [mine])
    expected = {"publisher": "Publisher wrote this", "receiver": "Ana wrote this"}
    named = (("publisher", publisher), ("Ana", recipient))
    pages = _comments(named, database, expected, "standard_comments_visible")
    _authors(pages, reader, "spoofed-author", "standard_verified_author", True)
    current = standard_rows(publisher, database)["receiver"]
    write_owned(publisher, database, replace_comment(mine, current, "Moderated by publisher"))
    expected["receiver"] = "Moderated by publisher"
    moderated = _comments((("Ana", recipient),), database, expected, "standard_publisher_edit_visible")[0]
    edit = replace_comment(mine, moderated["receiver"], "Edited by Ana")
    edit["record"]["author_id"] = "spoofed-editor"
    cross_author = replace_comment(owner_comment, moderated["publisher"], "Forbidden edit")
    queue_and_restart(publisher, recipient, database, scope, [cross_author, edit])
    expected["receiver"] = "Edited by Ana"
    pages = _comments(named, database, expected, "standard_comment_edit_applied")
    _authors(pages, reader, "spoofed-editor", "standard_edited_author_unchanged", False)
    # The member proposal must name Ana's revision. The publisher stamps a different one.
    current = next(page for (name, _node), page in zip(named, pages) if name == "Ana")["receiver"]
    delete = {**mine, "mutation_type": "delete", "record": {},
              "expected": {"type": "revision", "revision": current["revision"]}}
    album = recipe.mutation("social/Album", definitions["social/Album"], "member-album", "")
    album["record"] = {"title": "Not allowed", "description": "Member write", "cover_photo_id": "none",
                       "created_at": "2026-11-14T00:00:00Z"}
    queue_and_restart(publisher, recipient, database, scope, [delete, album])
    expected.pop("receiver")
    wait_pages(named, "standard_own_delete_applied", lambda node: _delete_view(node, database),
               _gone(expected), delete_line)
    recipe.report("Ana edited and deleted her own comment. The publisher discarded her edit of the publisher's comment and her Album write.")


def exercise_upgrade(publisher, recipient, database, previous, scope, member_writes):
    recipe = _recipe()
    new = recipe.declare(publisher, database, recipe.COMMENT, expanded=True)
    check(new != previous, "circle_schema_upgrade_new_definition", saw("definition", changed=new != previous))
    seeded = recipe.mutation(recipe.COMMENT, new, "upgrade", "Before edit")
    seeded["record"]["caption"] = "Keep the optional field"
    write_owned(publisher, database, seeded)
    recipe.wait_text("Ana", recipient, database, recipe.COMMENT, "upgrade", "Before edit",
                     "circle_upgraded_record_received")
    observe_access(recipient, database, "active", 1, role="submit", member_writes=member_writes)
    current = recipe.query(recipient, database, recipe.COMMENT, "upgrade")[0]
    update = recipe.mutation(recipe.COMMENT, new, "upgrade", "After edit")
    update.update(mutation_type="replace", expected={"type": "revision", "revision": current["revision"]},
                  record={**current["fields"], "text": "After edit"},
                  source_file_name="replacement.txt", metadata={"share_author": "replacement-author"})
    stale = {**update, "record": {**update["record"], "text": "Stale edit"}}
    queue_and_restart(publisher, recipient, database, scope, [update, stale])
    for name, node in (("publisher", publisher), ("Ana", recipient)):
        recipe.wait_text(name, node, database, recipe.COMMENT, "upgrade", "After edit",
                         "circle_upgraded_record_edited")
        row = recipe.query(node, database, recipe.COMMENT, "upgrade")
        found = row[0] if row else None
        fields = found.get("fields") if isinstance(found, dict) else None
        check(fields == update["record"], "circle_upgraded_fields_preserved",
              "%s %s" % (cell(name, found, "text"), cell("cap", found, "caption")))
        recipe.wait_text(name, node, database, recipe.COMMENT, "allowed", "From Ana",
                         "circle_old_records_preserved")
    recipe.report("Ana edited the upgraded Comment at the same sharing revision. The added field and the older record remained. The publisher discarded the stale edit.")
    return new


def exercise_reader(publisher, recipient, database, definitions, scope):
    recipe = _recipe()
    observe_access(recipient, database, "active", 2)
    recipe.wait_text("Ana", recipient, database, recipe.COMMENT, "allowed", "From Ana",
                     "circle_reader_retains_access")
    update = recipe.mutation(recipe.COMMENT, definitions[recipe.COMMENT], "reader", "Reader submission")
    recipe.refuse_submission(recipient, database, update)
    page = inbox(publisher, scope, database)
    entries = page.get("entries") if isinstance(page, dict) else None
    check(entries == [], "circle_reader_not_queued", inbox_line(page))
    for name, node in (("publisher", publisher), ("Ana", recipient)):
        require_absent(name, recipe.query(node, database, recipe.COMMENT, "reader"),
                       "circle_reader_write_discarded", "text")
    recipe.report("Ana's read role was refused with 403. The publisher's inbox did not receive the proposal, and neither node has the reader record.")


def verify_results(recipient, database, queued):
    results = sdk(recipient, "submission-status", database=database, ids=[*queued, str(uuid.uuid4())])
    found = _statuses(results)
    check(found == ["discarded", "published", "discarded", "unknown"],
          "circle_sdk_final_proposal_results", "statuses=%s n=%s" % (status_text(found), len(found)))
    _recipe().report("The receiving SDK read the final outcome of each of Ana's proposals by UUID.")


def verify_settled_retry(publisher, recipient, database, scope, update, mutation_id):
    recipe = _recipe()
    pending = inbox(publisher, scope, database)
    entries = pending.get("entries") if isinstance(pending, dict) else None
    check(entries == [], "circle_retry_starts_with_settled_inbox", inbox_line(pending))
    published = published_views(recipient, scope, database)
    _same_database(recipient, database)
    recipe.submit(recipient, database, update, mutation_id=mutation_id)
    results = sdk(recipient, "submission-status", database=database, ids=[mutation_id])
    found = _statuses(results)
    check(found == ["published"], "circle_retry_preserves_outcome",
          "statuses=%s n=%s" % (status_text(found), len(found)))
    again = inbox(publisher, scope, database)
    check(again == pending, "circle_retry_does_not_enqueue",
          "%s was=%s" % (inbox_line(again), inbox_line(pending)))
    views = published_views(recipient, scope, database)
    check(views == published, "circle_retry_does_not_publish",
          "%s was=%s" % (views_line(views), views_line(published)))
    recipe.report("Ana retried a settled mutation with the same UUID and operation. Its published outcome and both log positions stayed the same.")


def queue_and_restart(publisher, recipient, database, scope, updates, *, check_status=False):
    recipe = _recipe()
    _same_database(recipient, database)
    publisher.stop()
    try:
        page = inbox(publisher, scope, database)
        entries = page.get("entries") if isinstance(page, dict) else None
        check(entries == [], "circle_empty_inbox", inbox_line(page))
        published_before = published_views(recipient, scope, database)
        queued = [recipe.submit(recipient, database, update) for update in updates]
        if check_status:
            results = sdk(recipient, "submission-status", database=database, ids=queued)
            found = _statuses(results)
            check(isinstance(results, list) and all(item == "submitted" for item in found),
                  "circle_sdk_proposals_pending", "statuses=%s n=%s" % (status_text(found), len(found)))
        through = pending_positions(publisher, recipient, scope, database, queued, published_before)
        summary("ExeMem queued the mutations in the publisher's inbox and refused Ana's inbox read and settlement. The published log did not advance.")
    finally:
        publisher.start()
    wait("circle_publisher_consumed_submissions", lambda: inbox(publisher, scope, database),
         lambda page: _consumed(page, through),
         lambda page, reason: "%s %s through=%s" % (reason, inbox_line(page), mark(through)))
    return queued


def _comments(named, database, expected, stage):
    def read(node):
        return standard_rows(node, database)

    def done(pages):
        return all(isinstance(page, dict) and texts(page) == expected for page in pages)

    return wait_pages(named, stage, read, done, comment_line)


def _authors(pages, reader, author_field, code, publisher_author):
    for page in pages:
        member_author(page["receiver"], reader, author_field, code)
        if publisher_author:
            row = page.get("publisher") if isinstance(page, dict) else None
            created = row.get("created_by") if isinstance(row, dict) else None
            check(created not in (None, reader), "standard_author_field_is_not_creator",
                  cell("pub", row, "social_comment_text"))


def _delete_view(node, database):
    return standard_rows(node, database), _recipe().query(node, database, "social/Album", "member-album")


def _gone(expected):
    def done(pages):
        return all(isinstance(item, tuple) and isinstance(item[0], dict)
                   and texts(item[0]) == expected and not item[1] for item in pages)
    return done


def _statuses(results):
    if not isinstance(results, list):
        return []
    return [row.get("status") if isinstance(row, dict) else None for row in results]


def _same_database(node, database):
    chosen = pick(node, _recipe().SCOPE, "write", database)
    match = isinstance(chosen, dict) and chosen.get("databaseId") == database
    check(match, "circle_picker_write", saw("picker", id_match=match))


def _consumed(page, through):
    if not isinstance(page, dict):
        return False
    try:
        cursor = int(page["next"])
    except (KeyError, TypeError, ValueError):
        return False
    return cursor >= through and page.get("entries") == []
