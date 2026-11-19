"""Prove open-link reading and a snapshot start against the DEV cloud.

People creates the database. The node sets read, publishes the snapshot, mints
the link, and accepts it. People has no command for those steps. The reader
has no cloud account and no member entry.
"""

import os
import time
import uuid

from people_app import install, people, write_owned
from share_node import (
    accept_link, await_defaults, connect, declare, keyed, mint_link, no_cloud_account, note,
    proposal, publish_snapshot, put_settings, refuse_proposal, report, snapshot_story, until,
    write_member,
)
from support import OperationError, require, run_operation


BEFORE = "Before the snapshot"
AFTER = "After the snapshot"
CLOSED = "After read returned to members"


def create(publisher, name):
    created = people(publisher, "db", "create", name)
    database = str(uuid.UUID(created["id"]))
    require(database == created["id"] and created.get("sharingMode") == "whole_database", "link_database")
    report("Owned DEV mutation log %s for %s." % (database, name))
    return database


def wait_notes(node, database, pairs, stage):
    keys = [key for key, _text in pairs]

    def done(rows):
        return all(row and row["fields"].get("text") == text for row, (_key, text) in zip(rows, pairs))

    until(stage, lambda: keyed(node, database, keys), done)


def prove_reader(reader, database, definition, requested):
    wait_notes(reader, database, (("before", BEFORE), ("after", AFTER)), "link_reader_records")
    report("Reader snapshot: %s." % snapshot_story(reader.home, database, requested))
    require(write_member(reader, database, note("Reader write", "reader-write")) == "refused", "link_reader_write")
    require(keyed(reader, database, ["reader-write"])[0] is None, "link_reader_write_kept")
    refuse_proposal(reader, database, proposal(definition, "reader-proposal", "Reader proposal"), "link_reader_proposal")
    require(keyed(reader, database, ["reader-proposal"])[0] is None, "link_reader_proposal_kept")
    report("The reader was refused on the write route and on proposal submission, with no local record kept.")


def hold_closed(publisher, reader, database):
    write_owned(publisher, database, note(CLOSED, "closed"))
    require(keyed(publisher, database, ["closed"])[0]["fields"].get("text") == CLOSED, "link_closed_write")
    for _ in range(12):
        before, after, closed = keyed(reader, database, ["before", "after", "closed"])
        require(before and before["fields"].get("text") == BEFORE, "link_copy_lost")
        require(after and after["fields"].get("text") == AFTER, "link_copy_lost")
        require(closed is None, "link_read_not_closed")
        time.sleep(1)
    report("The reader kept both earlier records and did not receive the record written after read "
           "returned to members. The node does not record that refusal for a received database.")


def restore(publisher, database, opened, failed):
    if not opened:
        return
    try:
        put_settings(publisher, database, read="members")
    except OperationError:
        report("Could not return read to members.")
        if not failed:
            raise


def operation(programs, root, publisher, recipient, api_key):
    if revision := os.environ.get("PEOPLE_INSTALL_REVISION"):
        install(publisher, root, revision)
    name = "ci-link-%s-%s" % (os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"])
    database = create(publisher, name)
    definition = declare(publisher, database)
    write_owned(publisher, database, note(BEFORE, "before"))
    connect(programs, publisher, api_key)
    await_defaults(publisher, database)
    opened = False
    failed = False
    try:
        put_settings(publisher, database, read="anyone")
        opened = True
        requested = publish_snapshot(publisher, database)
        report("Publisher snapshot through %s." % requested)
        write_owned(publisher, database, note(AFTER, "after"))
        no_cloud_account(recipient)
        accepted = accept_link(recipient, mint_link(publisher, database), "open")
        require(accepted.get("id") == database and accepted.get("access") == "reader", "link_accept_database")
        prove_reader(recipient, database, definition, requested)
        put_settings(publisher, database, read="members")
        opened = False
        hold_closed(publisher, recipient, database)
    except Exception:
        failed = True
        raise
    finally:
        restore(publisher, database, opened, failed)


if __name__ == "__main__":
    run_operation(operation)
