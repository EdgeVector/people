"""Prove direct member writes against the DEV cloud.

People saves the contacts, grants Submit or read, seals the invitations, and
accepts them. The node sets write and the member-write map, takes the member
writes, and submits the later proposal. People has no command for those steps.
"""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

from direct_report import access_report, attach, label, row_report, watch, watch_rows
from people_app import install, people, write_owned
from share_node import (
    READ_ONLY, SCHEMA, await_defaults, connect, declare, keyed, local_node, no_cloud_account,
    node_key, note, proposal, put_settings, report, submit_proposal, write_member,
)
from support import OperationError, run_operation, running


def fail(code, detail, **fields):
    raise OperationError(code, command_output=(detail, ""), **fields)


def yes(value):
    return "1" if value else "0"


def create(publisher, name):
    created = people(publisher, "db", "create", name)
    database = str(uuid.UUID(created["id"]))
    if database != created["id"] or created.get("sharingMode") != "whole_database":
        fail("direct_database", "id=%s mode=%s" % (yes(database == created["id"]), label(created.get("sharingMode"))))
    report("Owned DEV mutation log %s for %s." % (database, name))
    return database


def grant(publisher, database, person, scope, granted):
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


def handoff(publisher, member, database, person, publisher_key, local_name):
    receipt = people(publisher, "invite", database, person)
    matched_db = receipt.get("databaseId") == database
    matched_publisher = receipt.get("publisher") == publisher_key
    invitation = receipt.get("invitation")
    invitation_ok = isinstance(invitation, str) and bool(invitation)
    if not (matched_db and matched_publisher and invitation_ok):
        fail("direct_invitation", "database=%s publisher=%s invitation=%s" % (
            yes(matched_db), yes(matched_publisher), yes(invitation_ok)))
    accepted = people(member, "db", "accept", "--publisher", publisher_key, "--name", local_name, stdin=receipt)
    if accepted.get("id") != database or accepted.get("sharingMode") != "whole_database":
        fail("direct_accept", "id=%s mode=%s" % (
            yes(accepted.get("id") == database), label(accepted.get("sharingMode"))))


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
        access = shown["database"]["selfAccess"]
        return (access.get("state") == "active" and access.get("stale") is False and access.get("confirmedAtMs")
                and access.get("role") == role and (write is None or access.get("write") == write))

    watch(stage, lambda: people(node, "db", "show", database), done,
          lambda shown, reason: access_report(name, shown, reason))


def authored(named, database, key, text, author, stage):
    def done(rows):
        return all(row and row["fields"].get("text") == text and row.get("created_by") == author
                   and row.get("updated_by") == author for row in rows)

    watch_rows(named, database, key, stage, done)


def converge(named, database, key, authors):
    def done(rows):
        if any(row is None for row in rows):
            return False
        text = rows[0]["fields"].get("text")
        creator = rows[0].get("created_by")
        same = all(row["fields"].get("text") == text and row.get("created_by") == creator
                   and row.get("updated_by") == creator for row in rows)
        if not same:
            return False
        if authors.get(text) != creator:
            raise OperationError("direct_author_mismatch")
        return True

    watch_rows(named, database, key, "direct_same_key_converge", done)
    report("The same key converged. All three nodes hold the first record in the service's order.")


def same_key(named, database, authors):
    member_a, member_b = named[1][1], named[2][1]
    last = "unread"
    for attempt in range(4):
        key = "race-%s" % attempt
        updates = ((member_a, note("from-a", key)), (member_b, note("from-b", key)))
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda item: write_member(item[0], database, item[1]), updates))
        if outcomes != ["committed", "committed"]:
            fail("direct_same_key_refused", "outcomes=%s" % " ".join(label(item) for item in outcomes),
                 socket_refusal={"status": 403, "reason": "direct_same_key_refused", "error_head": READ_ONLY})
        seen = [keyed(node, database, [key])[0] for node in (member_a, member_b)]
        texts = tuple(row["fields"].get("text") if row else None for row in seen)
        last = " ".join(label(item) for item in texts)
        if texts == ("from-a", "from-b"):
            converge(named, database, key, authors)
            return
    fail("direct_same_key_not_concurrent", "last=%s" % last)


def follow(named, reader, database, author):
    outcome = write_member(named[1][1], database, note("After the race", "after-race"))
    if outcome != "committed":
        fail("direct_follow_write", "outcome=%s" % label(outcome))
    authored((*named, ("Cam", reader)), database, "after-race", "After the race", author, "direct_follow")
    report("A later direct write was visible on every member, including the reader.")


def refuse_reader(member, database):
    outcome = write_member(member, database, note("From the reader", "reader-write"))
    if outcome != "refused":
        fail("direct_reader_write", "outcome=%s" % label(outcome))
    row = keyed(member, database, ["reader-write"])[0]
    if row is not None:
        fail("direct_reader_write_kept", row_report(("Cam",), [row], "differed"))
    report("C was refused before a local record. The write route returned 403, a received database is read only.")


def close_direct(publisher, named, database, definition):
    stored = put_settings(publisher, database, write="proposals")
    write = stored.get("write") if isinstance(stored, dict) else None
    member_writes = stored.get("memberWrites") if isinstance(stored, dict) else None
    schema_write = member_writes.get(SCHEMA) if isinstance(member_writes, dict) else None
    if write != "proposals" or schema_write != "shared":
        fail("direct_proposals_settings", "write=%s schema=%s" % (label(write), label(schema_write)))
    member = named[1][1]
    outcome = write_member(member, database, note("After proposals", "direct-closed"))
    if outcome == "committed":
        watch("direct_undone", lambda: keyed(member, database, ["direct-closed"])[0], lambda row: row is None,
              lambda row, reason: row_report(("Ana",), [row], reason))
        report("A committed the write locally. The refusal removed it, and the key is now absent on A.")
    else:
        row = keyed(member, database, ["direct-closed"])[0]
        if row is not None:
            fail("direct_refused_record_kept", row_report(("Ana",), [row], "differed"))
        report("A was refused before a local record. The write route returned 403, a received database is read only.")
    for name, node in named:
        row = keyed(node, database, ["direct-closed"])[0]
        if row is not None:
            fail("direct_refused_record_kept", row_report((name,), [row], "differed"))
    submit_proposal(named[2][1], database, proposal(definition, "proposed", "Through a proposal"))
    watch_rows(named, database, "proposed", "direct_proposal",
               lambda rows: all(row and row["fields"].get("text") == "Through a proposal" for row in rows))
    report("With write set to proposals, B's change was published through the proposal log.")


def share(programs, root, publisher, member_a, member_b, member_c, database, definition, granted):
    members = (("Ana", member_a), ("Bo", member_b), ("Cam", member_c))
    for node in (member_a, member_b, member_c):
        no_cloud_account(node)
    publisher_key, keys = enroll(programs, publisher, members, root)
    grant(publisher, database, "Ana", "all", granted)
    grant(publisher, database, "Bo", "all", granted)
    grant(publisher, database, "Cam", "read", granted)
    for person, node, local_name in (("Ana", member_a, "from-ana"), ("Bo", member_b, "from-bo"), ("Cam", member_c, "from-cam")):
        handoff(publisher, node, database, person, publisher_key, local_name)
    named = (("publisher", publisher), ("Ana", member_a), ("Bo", member_b))
    for person, node in members:
        watch_rows(((person, node),), database, "seed", "direct_seed",
                   lambda rows: rows[0] and rows[0]["fields"].get("text") == "Seed")
    wait_role(member_a, database, "submit", "direct", "direct_access_a", "Ana")
    wait_role(member_b, database, "submit", "direct", "direct_access_b", "Bo")
    wait_role(member_c, database, "read", None, "direct_access_c", "Cam")
    outcome = write_member(member_a, database, note("From Ana", "direct-1"))
    if outcome != "committed":
        fail("direct_member_write", "outcome=%s" % label(outcome))
    authored((*named, ("Cam", member_c)), database, "direct-1", "From Ana", keys["Ana"], "direct_member_visible")
    report("A's direct write is on every node, with A's node key as created_by and updated_by.")
    same_key(named, database, {"from-a": keys["Ana"], "from-b": keys["Bo"]})
    follow(named, member_c, database, keys["Ana"])
    refuse_reader(member_c, database)
    close_direct(publisher, named, database, definition)


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
        report("ExeMem confirmed the final recipient revocations.")
    if error and not failed:
        raise error


def operation(programs, root, publisher, recipient, api_key):
    database = None
    roster = [("publisher", publisher), ("Ana", recipient)]
    try:
        if revision := os.environ.get("PEOPLE_INSTALL_REVISION"):
            install(publisher, root, revision)
        name = "ci-direct-%s-%s" % (os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"])
        database = create(publisher, name)
        definition = declare(publisher, database)
        write_owned(publisher, database, note("Seed", "seed"))
        connect(programs, publisher, api_key)
        await_defaults(publisher, database)
        put_settings(publisher, database, write="direct", memberWrites={SCHEMA: "shared"})
        member_b = local_node(programs, root, "bo")
        member_c = local_node(programs, root, "cam")
    except OperationError as error:
        attach(error, roster, database)
        raise
    roster = [("publisher", publisher), ("Ana", recipient), ("Bo", member_b), ("Cam", member_c)]
    granted, failed = [], False
    try:
        with running(member_b), running(member_c):
            try:
                share(programs, root, publisher, recipient, member_b, member_c, database, definition, granted)
            except OperationError as error:
                attach(error, roster, database)
                raise
    except Exception:
        failed = True
        raise
    finally:
        try:
            revoke(publisher, database, granted, failed)
        except OperationError as error:
            attach(error, roster, database)
            raise


if __name__ == "__main__":
    run_operation(operation)
