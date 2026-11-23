"""Prove that Share members propose changes to a shared database, against the DEV cloud.

People saves the contacts, grants submit or read, mints consent codes, and accepts
them by hand-off. A database with no write member holds no lease. Only the
publisher's signature puts a record into it. A member proposes a change on the
node's submissions route. The publisher accepts it (published) or signs not
accepted. A /v1/write to a received database is refused for every member. People
has no command for those socket steps. Org members change records directly under a
lease: that is people-org.
"""

import os
from concurrent.futures import ThreadPoolExecutor

from direct_publish import begin_ana_publish, note_ana_publish
from direct_report import access_report, attach, label, row_report, watch, watch_rows
from people_app import install, people, write_owned
from share_node import (
    await_decisions, await_empty_grants, connect, declare, ensure_relay, hex32_id, keyed,
    local_node, no_cloud_account, node_key, note, proposal, refuse_proposal,
    replacement, report, submit_and_wait, submit_proposal, write_member,
)
from support import OperationError, run_operation, running


def fail(code, detail, **fields):
    raise OperationError(code, command_output=(detail, ""), **fields)


def yes(value):
    return "1" if value else "0"


def create(publisher, name):
    created = people(publisher, "db", "create", name)
    database = created.get("id")
    try:
        database = hex32_id(database, "direct_database")
    except OperationError:
        fail("direct_database", "id=%s mode=%s" % (yes(False), label(created.get("sharingMode"))))
    if created.get("sharingMode") != "whole_database":
        fail("direct_database", "id=%s mode=%s" % (yes(True), label(created.get("sharingMode"))))
    report("Owned DEV mutation log %s for %s." % (database, name))
    return database


def grant(publisher, database, person, scope, granted):
    # Share submit uses all. Org (--write write) gives the database a lease, which people-org proves.
    arguments = ("grant", "set", database, person, "--write", scope)
    preview = people(publisher, *arguments)
    token = preview.get("token")
    token_ok = isinstance(token, str) and bool(token)
    if preview.get("applied") is not False or not token_ok:
        fail("direct_grant_preview", "applied=%s token=%s" % (label(preview.get("applied")), yes(token_ok)))
    applied = people(publisher, *arguments, "--apply", "--token", preview["token"])
    if applied.get("applied") is not True:
        fail("direct_grant_apply", "applied=%s" % label(applied.get("applied")))
    granted.append(person)


def handoff(publisher, member, database, person, publisher_key, local_name, role, node_key):
    from people_chain_views import expect_member, expect_self_role
    # Invite by node key; display name can match several synced contacts.
    del person
    receipt = people(publisher, "invite", database, node_key)
    matched_db = receipt.get("databaseId") == database
    matched_publisher = receipt.get("publisher") == publisher_key
    consent = receipt.get("consent")
    consent_ok = isinstance(consent, str) and consent.startswith("people-consent-v1.")
    if not (matched_db and matched_publisher and consent_ok):
        fail("direct_consent", "database=%s publisher=%s consent=%s" % (
            yes(matched_db), yes(matched_publisher), yes(consent_ok)))
    # Wait for the signed grant on the publisher chain before the member reads the relay.
    expect_member(publisher, database, node_key, role=role)
    accepted = people(member, "db", "accept", "--publisher", publisher_key, "--name", local_name,
                      stdin=consent)
    if accepted.get("id") != database or accepted.get("sharingMode") != "whole_database":
        fail("direct_accept", "id=%s mode=%s" % (
            yes(accepted.get("id") == database), label(accepted.get("sharingMode"))))
    expect_self_role(member, database, role)


def enroll(programs, publisher, members, root):
    publisher_key = node_key(programs, publisher, root, "publisher")
    keys = {}
    for person, node in members:
        key = node_key(programs, node, root, person)
        if key == publisher_key or key in keys.values():
            fail("direct_distinct_members", "person=%s distinct=0" % person)
        people(publisher, "person", "add", person, "--node-key", key, "--contact", str(root / ("%s.contact" % person)))
        keys[person] = key
    return publisher_key, keys


def wait_role(node, database, role, write, stage, name):
    def done(shown):
        access = (shown.get("database") or {}).get("selfAccess") or {}
        # #1805 selfAccess is `{role}` only.
        return access.get("role") == role

    del write
    watch(stage, lambda: people(node, "db", "show", database), done,
          lambda shown, reason: access_report(name, shown, reason))


def authored(named, database, key, text, author, stage):
    def done(rows):
        return all(row and row["fields"].get("text") == text and row.get("created_by") == author
                   and row.get("updated_by") == author for row in rows)

    # The visibility wait is the poll bound. Samples stop when the records
    # arrive or when that wait ends. The timeline is Ana's publish only.
    begin_ana_publish()
    ana = next((node for name, node in named if name == "Ana"), None)

    def observe():
        if ana is not None:
            note_ana_publish(ana, database)

    watch_rows(named, database, key, stage, done, observe)


def converge(everyone, database, key, authors, winner):
    def done(rows):
        if any(row is None for row in rows):
            return False
        text = rows[0]["fields"].get("text")
        creator = rows[0].get("created_by")
        same = all(row["fields"].get("text") == text and row.get("created_by") == creator
                   and row.get("updated_by") == creator for row in rows)
        if not same:
            return False
        if text != winner or authors.get(text) != creator:
            raise OperationError("direct_author_mismatch")
        return True

    watch_rows(everyone, database, key, "direct_same_key_converge", done)
    report("The same key converged. Every node holds the proposal that the publisher accepted first.")


def same_key(everyone, database, definition, authors):
    updates = ((everyone[1][1], "from-a"), (everyone[2][1], "from-b"))
    key = "race"
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(
            lambda item: submit_proposal(item[0], database, proposal(definition, key, item[1])), updates))
    found = [await_decisions(node, database, [mutation_id], "direct_same_key_decided")[0]
             for (node, _text), mutation_id in zip(updates, ids)]
    if sorted(found) != ["not_accepted", "published"]:
        fail("direct_same_key_outcomes", "a=%s b=%s" % (label(found[0]), label(found[1])))
    converge(everyone, database, key, authors, updates[found.index("published")][1])
    report("The proposal that lost the same-key race ended not_accepted on its own node.")


def follow(everyone, database, definition, author):
    submit_and_wait(everyone[1][1], database, proposal(definition, "after-race", "After the race"),
                    "direct_follow_decided")
    authored(everyone, database, "after-race", "After the race", author, "direct_follow")
    report("A later proposal was accepted and is visible on every member, including the reader.")


def refuse_write(node, name, database, key, stage):
    """A /v1/write to a received database is a 403 for every member. No record lands locally."""
    outcome = write_member(node, database, note("Direct write", key))
    if outcome != "refused":
        fail(stage, "outcome=%s" % label(outcome))
    row = keyed(node, database, [key])[0]
    if row is not None:
        fail("%s_kept" % stage, row_report((name,), [row], "differed"))


def refuse_reader(member, database, definition):
    refuse_proposal(member, database, proposal(definition, "reader-write", "From the reader"),
                    "direct_reader_proposal")
    refuse_write(member, "Cam", database, "reader-write", "direct_reader_write")
    report("C was refused on the submissions route (403) and on /v1/write, and holds no local record.")


def close_direct(everyone, database, definition, keys):
    ana, bo = everyone[1][1], everyone[2][1]
    # Nothing unaccepted was ever written locally.
    refuse_write(ana, "Ana", database, "direct-closed", "direct_write_refused_after_lowering")
    for name, node in everyone:
        row = keyed(node, database, ["direct-closed"])[0]
        if row is not None:
            fail("direct_refused_record_kept", row_report((name,), [row], "differed"))
    mine, theirs = keyed(ana, database, ["direct-1"])[0], keyed(bo, database, ["direct-1"])[0]
    if not (mine and mine.get("revision") and theirs and theirs.get("revision")):
        fail("direct_revision_missing", row_report(("Ana", "Bo"), [mine, theirs], "differed"))
    foreign = submit_proposal(bo, database, replacement(definition, "direct-1", "Edited by Bo", theirs["revision"]))
    found = await_decisions(bo, database, [foreign], "direct_foreign_edit_decided")
    if found != ["not_accepted"]:
        fail("direct_foreign_edit_outcome", "status=%s" % label(found[0]))
    submit_and_wait(ana, database, replacement(definition, "direct-1", "Edited by Ana", mine["revision"]),
                    "direct_own_edit_decided")
    authored(everyone, database, "direct-1", "Edited by Ana", keys["Ana"], "direct_own_edit_visible")
    submit_and_wait(bo, database, proposal(definition, "proposed", "Through a proposal"), "direct_proposal_decided")
    watch_rows(everyone, database, "proposed", "direct_proposal",
               lambda rows: all(row and row["fields"].get("text") == "Through a proposal" for row in rows))
    report("A's edit of her own record was published, B's edit of it was not accepted, "
           "and B's new record was published. A direct write was still refused and left nothing to undo.")


def share(programs, root, publisher, member_a, member_b, member_c, database, definition, granted):
    members = (("Ana", member_a), ("Bo", member_b), ("Cam", member_c))
    for node in (member_a, member_b, member_c):
        no_cloud_account(node)
    publisher_key, keys = enroll(programs, publisher, members, root)
    # Grant by node key: personal-DB sync can leave several contacts with the
    # same display name on the fixed DEV publisher account.
    grant(publisher, database, keys["Ana"], "all", granted)
    grant(publisher, database, keys["Bo"], "all", granted)
    grant(publisher, database, keys["Cam"], "read", granted)
    for person, node, local_name, role in (
            ("Ana", member_a, "from-ana", "submit"),
            ("Bo", member_b, "from-bo", "submit"),
            ("Cam", member_c, "from-cam", "read")):
        handoff(publisher, node, database, person, publisher_key, local_name, role, keys[person])
    everyone = (("publisher", publisher), ("Ana", member_a), ("Bo", member_b), ("Cam", member_c))
    for person, node in members:
        watch_rows(((person, node),), database, "seed", "direct_seed",
                   lambda rows: rows[0] and rows[0]["fields"].get("text") == "Seed")
    wait_role(member_a, database, "submit", None, "direct_access_a", "Ana")
    wait_role(member_b, database, "submit", None, "direct_access_b", "Bo")
    wait_role(member_c, database, "read", None, "direct_access_c", "Cam")
    refuse_write(member_a, "Ana", database, "direct-refused", "direct_write_refused")
    submit_and_wait(member_a, database, proposal(definition, "direct-1", "From Ana"), "direct_member_decided")
    authored(everyone, database, "direct-1", "From Ana", keys["Ana"], "direct_member_visible")
    report("A's accepted proposal is on every node, with A's node key as created_by and updated_by.")
    same_key(everyone, database, definition, {"from-a": keys["Ana"], "from-b": keys["Bo"]})
    follow(everyone, database, definition, keys["Ana"])
    refuse_reader(member_c, database, definition)
    close_direct(everyone, database, definition, keys)


def revoke(publisher, database, granted, failed):
    error = None
    for person in granted:
        try:
            result = people(publisher, "grant", "revoke", database, person, "--apply")
            if result.get("applied") is not True or result.get("state") != "revoked":
                fail("direct_revoke", "person=%s applied=%s state=%s" % (
                    person, label(result.get("applied")), label(result.get("state"))))
        except OperationError as failure:
            error = error or failure
    if granted and error is None:
        report("Signed revokes were confirmed for the final recipients.")
    if error and not failed:
        raise error


def operation(programs, root, publisher, recipient):
    database = None
    roster = [("publisher", publisher), ("Ana", recipient)]
    try:
        if revision := os.environ.get("PEOPLE_INSTALL_REVISION"):
            install(publisher, root, revision)
        name = "ci-direct-%s-%s" % (os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"])
        database = create(publisher, name)
        definition = declare(publisher, database)
        write_owned(publisher, database, note("Seed", "seed"))
        connect(programs, publisher)
        await_empty_grants(publisher, database)
        ensure_relay(publisher, database)
        member_b = local_node(programs, root, "bo")
        member_c = local_node(programs, root, "cam")
    except OperationError as error:
        attach(error, roster, database, evidence=True)
        raise
    roster = [("publisher", publisher), ("Ana", recipient), ("Bo", member_b), ("Cam", member_c)]
    granted, failed = [], False
    try:
        with running(member_b), running(member_c):
            try:
                share(programs, root, publisher, recipient, member_b, member_c, database, definition, granted)
            except OperationError as error:
                attach(error, roster, database, evidence=True)
                raise
    except Exception:
        failed = True
        raise
    finally:
        try:
            revoke(publisher, database, granted, failed)
        except OperationError as error:
            attach(error, roster, database, evidence=True)
            raise


if __name__ == "__main__":
    # OwnedNode discards daemon stderr unless this name is set. The failure
    # detail reads that file from each node home. Other recipes leave it unset.
    os.environ["PEOPLE_DIRECT_DAEMON_LOG"] = "daemon.log"
    run_operation(operation)
