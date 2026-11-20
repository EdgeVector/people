"""Prove Deliver (grant#any read) on the signed-history model.

People creates the database. The owner grants public read on the chain, mints
an open link for key delivery, and the reader accepts without a member grant.
No snapshots. Closing Deliver revokes grant#any; the reader's next sync must
not receive later records, and appends stay refused.
"""

import os
import time
import uuid

from people_app import install, people, write_owned
from share_node import (
    accept_link, await_empty_grants, bootstrap_relay, connect, declare, grant_public_read,
    keyed, mint_link, no_cloud_account, note, proposal, refuse_proposal, report,
    revoke_public_read, until, write_member,
)
from support import OperationError, require, run_operation


BEFORE = "Before public read"
AFTER = "After public read"
CLOSED = "After grant#any revoked"


def create(publisher, name):
    created = people(publisher, "db", "create", name)
    database = str(uuid.UUID(created["id"]))
    require(database == created["id"] and created.get("sharingMode") == "whole_database", "link_database")
    report("Owned DEV database %s for Deliver %s." % (database, name))
    return database


def wait_notes(node, database, pairs, stage):
    keys = [key for key, _text in pairs]

    def done(rows):
        return all(row and row["fields"].get("text") == text for row, (_key, text) in zip(rows, pairs))

    until(stage, lambda: keyed(node, database, keys), done)


def prove_reader(reader, database, definition):
    wait_notes(reader, database, (("before", BEFORE), ("after", AFTER)), "link_reader_records")
    report("Reader accepted grant#any and replayed the signed log from birth.")
    require(write_member(reader, database, note("Reader write", "reader-write")) == "refused", "link_reader_write")
    require(keyed(reader, database, ["reader-write"])[0] is None, "link_reader_write_kept")
    refuse_proposal(reader, database, proposal(definition, "reader-proposal", "Reader proposal"),
                    "link_reader_proposal")
    require(keyed(reader, database, ["reader-proposal"])[0] is None, "link_reader_proposal_kept")
    report("The reader was refused on append and proposal paths.")


def hold_closed(publisher, reader, database):
    write_owned(publisher, database, note(CLOSED, "closed"))
    require(keyed(publisher, database, ["closed"])[0]["fields"].get("text") == CLOSED, "link_closed_write")
    for _ in range(12):
        before, after, closed = keyed(reader, database, ["before", "after", "closed"])
        require(before and before["fields"].get("text") == BEFORE, "link_copy_lost")
        require(after and after["fields"].get("text") == AFTER, "link_copy_lost")
        require(closed is None, "link_read_not_closed")
        time.sleep(1)
    report("After revoking grant#any the reader kept earlier records and did not receive later ones.")


def restore(publisher, database, opened, failed):
    if not opened:
        return
    try:
        revoke_public_read(publisher, database)
    except OperationError:
        report("Could not revoke grant#any.")
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
    await_empty_grants(publisher, database)
    bootstrap_relay(publisher, database)
    opened = False
    failed = False
    try:
        grant_public_read(publisher, database)
        opened = True
        write_owned(publisher, database, note(AFTER, "after"))
        no_cloud_account(recipient)
        accepted = accept_link(recipient, mint_link(publisher, database), "open")
        require(accepted.get("id") == database, "link_accept_database")
        prove_reader(recipient, database, definition)
        revoke_public_read(publisher, database)
        opened = False
        hold_closed(publisher, recipient, database)
    except Exception:
        failed = True
        raise
    finally:
        restore(publisher, database, opened, failed)


if __name__ == "__main__":
    run_operation(operation)
