"""Prove direct member writes against the DEV cloud.

People saves the contacts, grants Submit or read, seals the invitations, and
accepts them. The node sets write and the member-write map, takes the member
writes, and submits the later proposal. People has no command for those steps.
"""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

from people_app import install, people, write_owned
from share_node import (
    READ_ONLY, SCHEMA, await_defaults, connect, declare, keyed, local_node, no_cloud_account,
    node_key, note, proposal, put_settings, report, submit_proposal, until, write_member,
)
from support import OperationError, require, run_operation, running


def create(publisher, name):
    created = people(publisher, "db", "create", name)
    database = str(uuid.UUID(created["id"]))
    require(database == created["id"] and created.get("sharingMode") == "whole_database", "direct_database")
    report("Owned DEV mutation log %s for %s." % (database, name))
    return database


def grant(publisher, database, person, scope, granted):
    arguments = ("grant", "set", database, person, "--write", scope)
    preview = people(publisher, *arguments)
    require(preview.get("applied") is False and isinstance(preview.get("token"), str) and preview["token"],
            "direct_grant_preview")
    applied = people(publisher, *arguments, "--apply", "--token", preview["token"])
    require(applied.get("applied") is True, "direct_grant_apply")
    granted.append(person)


def handoff(publisher, member, database, person, publisher_key, local_name):
    receipt = people(publisher, "invite", database, person)
    require(receipt.get("databaseId") == database and receipt.get("publisher") == publisher_key
            and isinstance(receipt.get("invitation"), str), "direct_invitation")
    accepted = people(member, "db", "accept", "--publisher", publisher_key, "--name", local_name, stdin=receipt)
    require(accepted.get("id") == database and accepted.get("sharingMode") == "whole_database", "direct_accept")


def enroll(programs, publisher, members, root):
    publisher_key = node_key(programs, publisher, root, "publisher")
    keys = {}
    for label, node in members:
        key = node_key(programs, node, root, label)
        require(key != publisher_key and key not in keys.values(), "direct_distinct_members")
        people(publisher, "person", "add", label, "--node-key", key, "--contact", str(root / ("%s.contact" % label)))
        keys[label] = key
    return publisher_key, keys


def wait_role(node, database, role, write, stage):
    def done(shown):
        access = shown["database"]["selfAccess"]
        return (access.get("state") == "active" and access.get("stale") is False and access.get("confirmedAtMs")
                and access.get("role") == role and (write is None or access.get("write") == write))

    until(stage, lambda: people(node, "db", "show", database), done)


def wait_rows(nodes, database, key, stage, done):
    until(stage, lambda: [keyed(node, database, [key])[0] for node in nodes], done)


def authored(nodes, database, key, text, author, stage):
    def done(rows):
        return all(row and row["fields"].get("text") == text and row.get("created_by") == author
                   and row.get("updated_by") == author for row in rows)

    wait_rows(nodes, database, key, stage, done)


def converge(nodes, database, key, authors):
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

    wait_rows(nodes, database, key, "direct_same_key_converge", done)
    report("The same key converged. All three nodes hold the first record in the service's order.")


def same_key(nodes, database, authors):
    member_a, member_b = nodes[1], nodes[2]
    for attempt in range(4):
        key = "race-%s" % attempt
        updates = ((member_a, note("from-a", key)), (member_b, note("from-b", key)))
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda item: write_member(item[0], database, item[1]), updates))
        if outcomes != ["committed", "committed"]:
            raise OperationError("direct_same_key_refused", socket_refusal={
                "status": 403, "reason": "direct_same_key_refused", "error_head": READ_ONLY})
        seen = [keyed(node, database, [key])[0] for node in (member_a, member_b)]
        texts = tuple(row["fields"].get("text") if row else None for row in seen)
        if texts == ("from-a", "from-b"):
            converge(nodes, database, key, authors)
            return
    raise OperationError("direct_same_key_not_concurrent")


def follow(nodes, reader, database, author):
    require(write_member(nodes[1], database, note("After the race", "after-race")) == "committed", "direct_follow_write")
    authored((*nodes, reader), database, "after-race", "After the race", author, "direct_follow")
    report("A later direct write was visible on every member, including the reader.")


def refuse_reader(member, database):
    require(write_member(member, database, note("From the reader", "reader-write")) == "refused", "direct_reader_write")
    require(keyed(member, database, ["reader-write"])[0] is None, "direct_reader_write_kept")
    report("C was refused before a local record. The write route returned 403, a received database is read only.")


def close_direct(publisher, nodes, database, definition):
    stored = put_settings(publisher, database, write="proposals")
    require(stored.get("write") == "proposals" and stored.get("memberWrites", {}).get(SCHEMA) == "shared",
            "direct_proposals_settings")
    outcome = write_member(nodes[1], database, note("After proposals", "direct-closed"))
    if outcome == "committed":
        until("direct_undone", lambda: keyed(nodes[1], database, ["direct-closed"])[0], lambda row: row is None)
        report("A committed the write locally. The refusal removed it, and the key is now absent on A.")
    else:
        require(keyed(nodes[1], database, ["direct-closed"])[0] is None, "direct_refused_record_kept")
        report("A was refused before a local record. The write route returned 403, a received database is read only.")
    for node in nodes:
        require(keyed(node, database, ["direct-closed"])[0] is None, "direct_refused_record_kept")
    submit_proposal(nodes[2], database, proposal(definition, "proposed", "Through a proposal"))
    wait_rows(nodes, database, "proposed", "direct_proposal",
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
    for label, node, local_name in (("Ana", member_a, "from-ana"), ("Bo", member_b, "from-bo"), ("Cam", member_c, "from-cam")):
        handoff(publisher, node, database, label, publisher_key, local_name)
    nodes = (publisher, member_a, member_b)
    for node in (member_a, member_b, member_c):
        wait_rows((node,), database, "seed", "direct_seed", lambda rows: rows[0] and rows[0]["fields"].get("text") == "Seed")
    wait_role(member_a, database, "submit", "direct", "direct_access_a")
    wait_role(member_b, database, "submit", "direct", "direct_access_b")
    wait_role(member_c, database, "read", None, "direct_access_c")
    require(write_member(member_a, database, note("From Ana", "direct-1")) == "committed", "direct_member_write")
    authored((*nodes, member_c), database, "direct-1", "From Ana", keys["Ana"], "direct_member_visible")
    report("A's direct write is on every node, with A's node key as created_by and updated_by.")
    same_key(nodes, database, {"from-a": keys["Ana"], "from-b": keys["Bo"]})
    follow(nodes, member_c, database, keys["Ana"])
    refuse_reader(member_c, database)
    close_direct(publisher, nodes, database, definition)


def revoke(publisher, database, granted, failed):
    error = None
    for person in granted:
        try:
            result = people(publisher, "grant", "revoke", database, person, "--apply")
            require(result.get("applied") is True and result.get("state") == "revoked", "direct_revoke")
        except OperationError as failure:
            error = error or failure
    if granted and error is None:
        report("ExeMem confirmed the final recipient revocations.")
    if error and not failed:
        raise error


def operation(programs, root, publisher, recipient, api_key):
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
    granted, failed = [], False
    try:
        with running(member_b), running(member_c):
            share(programs, root, publisher, recipient, member_b, member_c, database, definition, granted)
    except Exception:
        failed = True
        raise
    finally:
        revoke(publisher, database, granted, failed)


if __name__ == "__main__":
    run_operation(operation)
