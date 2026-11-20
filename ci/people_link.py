"""Prove Deliver (grant#any read) on the signed-history model.

People creates the database. The owner grants public read on the chain, mints
an open link for key delivery, and the reader accepts without a member grant.
No snapshots. LastDB #1805 refuses revoke of `any`, so this recipe proves the
open path and that LastDB rejects closing via revoke-any.
"""

import os

from people_app import install, people, write_owned
from share_node import (
    accept_link, await_empty_grants, connect, declare, ensure_relay, grant_public_read,
    hex32_id, keyed, mint_link, no_cloud_account, note, proposal, refuse_proposal, report,
    revoke_public_read, until, write_member,
)
from support import require, run_operation


BEFORE = "Before public read"
AFTER = "After public read"


def create(publisher, name):
    created = people(publisher, "db", "create", name)
    database = hex32_id(created["id"], "link_database")
    require(created.get("sharingMode") == "whole_database", "link_database_mode")
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


def operation(programs, root, publisher, recipient, api_key):
    if revision := os.environ.get("PEOPLE_INSTALL_REVISION"):
        install(publisher, root, revision)
    name = "ci-link-%s-%s" % (os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"])
    database = create(publisher, name)
    definition = declare(publisher, database)
    write_owned(publisher, database, note(BEFORE, "before"))
    connect(programs, publisher, api_key)
    await_empty_grants(publisher, database)
    ensure_relay(publisher, database)
    grant_public_read(publisher, database)
    write_owned(publisher, database, note(AFTER, "after"))
    no_cloud_account(recipient)
    accepted = accept_link(recipient, mint_link(publisher, database), "open")
    require(accepted.get("id") == database, "link_accept_database")
    prove_reader(recipient, database, definition)
    # #1805: public read cannot be revoked. Prove the refusal; keep grant#any.
    revoke_public_read(publisher, database)
    report("LastDB refused revoke of grant#any; Deliver stays open on the chain.")


if __name__ == "__main__":
    run_operation(operation)
