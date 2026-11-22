"""Hold more members than a signed whole-roster document could.

Signed history grants one publisher-signed entry per member. This recipe
shares a database with one real recipient, then adds 3,000 generated reader
keys through the owner grant action. It checks that the recipient still
receives new records, that changing one member costs about the same at 10
members and at 3,000, and that a revoke still stops the real recipient.
"""

import json
import os
import time
import uuid

from grant_probe import uploaded_count
from people_app import SETUP, observe_access, sdk, people
from people_share import album, wait_for_album, wait_uploaded_past, write_album
from share_node import await_empty_grants, ensure_relay, hex32_id, owner_grant, owner_revoke
from support import require, retry, run_operation, summary


SMALL = 10
MEMBERS = 3000
STEP = 500


def add_readers(node, database, keys):
    for key in keys:
        owner_grant(node, database, key, "read")


def timed_revoke(node, database, key):
    started = time.monotonic()
    owner_revoke(node, database, key)
    return time.monotonic() - started


def scale(programs, root, publisher, recipient, api_key):
    name = f"ci-scale-{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}"
    created = sdk(publisher, "create-shared", name=name)
    database = hex32_id(created["id"], "people_scale_database")
    require(created.get("sharingMode") == "whole_database", "people_scale_database")
    summary(f"Owned DEV database `{database}` for `{name}`.")
    print(f"People DEV scale database {database}", flush=True)
    schema = json.loads((SETUP / "Album.json").read_text())
    publisher.call(f"/api/databases/{database}/apps/declare-schema", schema)
    write_album(publisher, database, 0)
    publisher.stop()
    connected = programs.command(publisher.home, [
        "backup", "connect", "--existing", "--env", "dev", "--api-key-stdin",
        "--expected-user-hash", publisher.account, "--json",
    ], input_bytes=api_key, timeout=20)
    require(connected["environment"] == "dev" and connected["credentials_saved"] is True,
            "people_scale_dev_connection")
    publisher.keep_saved_credentials = True
    publisher.start()
    ensure_relay(publisher, database)
    finished = people(publisher, "db", "verify", database, "--setup", "shared-albums", "--finish")
    require(finished["databaseId"] == database and "memberWrites" not in finished
            and set(finished["definitions"]) == {
                "social/Album", "social/AlbumItem", "social/Photo", "social/Comment"},
            "people_scale_setup_finished")
    contact = root / "reader.contact"
    reader_key = programs.command(recipient.home, [
        "share", "contact", "--out", str(contact),
    ])["recipient_public_key"]
    publisher_key = programs.command(publisher.home, [
        "share", "contact", "--out", str(root / "publisher.contact"),
    ])["recipient_public_key"]
    await_empty_grants(publisher, database)
    people(publisher, "person", "add", "Ana", "--node-key", reader_key, "--contact", str(contact))
    invited = revoked = False
    try:
        # Address the contact by node key. After personal-DB sync, the fixed DEV
        # publisher may hold several people named Ana from earlier legs.
        preview = people(publisher, "grant", "set", database, reader_key, "--write", "read")
        require(preview.get("applied") is False and isinstance(preview.get("token"), str),
                "people_scale_grant_preview")
        people(publisher, "grant", "set", database, reader_key, "--write", "read",
               "--apply", "--token", preview["token"])
        receipt = sdk(publisher, "invite", database=name, recipient={"name": "Ana", "nodeKey": reader_key})
        invited = True
        require(isinstance(receipt.get("consent"), str)
                and receipt["consent"].startswith("people-consent-v1."), "people_scale_consent")
        received = sdk(recipient, "accept", publisher=publisher_key, name="scale",
                       receipt={"consent": receipt["consent"], "databaseId": receipt["databaseId"]})
        require(received.get("id") == database and received.get("sharingMode") == "whole_database",
                "people_scale_received")
        wait_for_album(recipient, database, 0)

        generated = [os.urandom(32).hex() for _ in range(MEMBERS)]
        require(len(set(generated)) == MEMBERS and reader_key not in generated
                and publisher_key not in generated, "people_scale_generated_keys")
        add_readers(publisher, database, generated[:SMALL])
        small_seconds = timed_revoke(publisher, database, generated[0])

        for start in range(SMALL, MEMBERS, STEP):
            add_readers(publisher, database, generated[start:start + STEP])
            print(f"Signed grants now cover {min(start + STEP, MEMBERS)} generated readers.", flush=True)

        large_seconds = timed_revoke(publisher, database, generated[1])
        require(large_seconds < max(5.0, small_seconds * 20), "people_scale_revoke_cost")

        before = uploaded_count(publisher, database)
        floor = int(before) if isinstance(before, str) and before.isdigit() else -1
        write_album(publisher, database, 1)
        # 3,000 grants leave a long upload queue. Wait for uploaded.json to pass
        # the pre-album cursor (authored.json is too large for the probe limit),
        # then give the recipient longer than the old 150s deadline.
        wait_uploaded_past(publisher, database, floor, seconds=300)
        wait_for_album(recipient, database, 1, seconds=300)
        observe_access(recipient, database, "active")

        people(publisher, "grant", "revoke", database, reader_key, "--apply")
        revoked = True
        write_album(publisher, database, 2)
        for _ in range(12):
            require(album(recipient, database, 2) == [], "people_scale_revoked_read")
            time.sleep(1)
        observe_access(recipient, database, "revoked")
    finally:
        if invited and not revoked:
            people(publisher, "grant", "revoke", database, reader_key, "--apply")
    message = ("People granted one real reader on the signed chain, added 3,000 generated "
               "reader grants, kept O(1)-ish revoke cost, and stopped the real recipient after revoke.")
    print(message, flush=True)
    summary(message)


if __name__ == "__main__":
    run_operation(scale)
