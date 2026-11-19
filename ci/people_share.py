"""Exercise People sharing with two isolated LastDB nodes in DEV."""

import fcntl
import json
import os
import time
import uuid

from people_app import (SETUP, expect_setup_member_writes_pending, install, observe_access,
                        open_content_apps, pick, read_album, read_records, report_content_flow, sdk,
                        people, write_owned)
from support import require, retry, run_operation, summary


SPEC = {"name": "social", "version": 1}
SCHEMA = "social/Album"
ALBUMS = ("Before sharing", "While shared", "While revoked")


def album(node, database, index):
    return read_records(node, database, SCHEMA, [f"album-{index}"])


def write_album(node, database, index):
    """Create one album in a database this node owns. POST /v1/write."""
    write_owned(node, database, {
        "schema": SCHEMA, "key_value": {"hash": f"album-{index}"}, "mutation_type": "create",
        "record": {"title": ALBUMS[index], "description": "Generated DEV record",
                   "cover_photo_id": "none", "created_at": "2026-11-13T00:00:00Z"},
    })


def wait_for_album(node, database, index):
    retry(f"people_album_{index}", lambda: album(node, database, index),
          lambda rows: len(rows) == 1 and rows[0]["fields"]["title"] == ALBUMS[index])


def roster(node, database):
    applied = people(node, "db", "show", database)["sharing"]
    require(applied["databaseId"] == database, "people_roster_database")
    if "members" in applied:
        # The owner view adds each member's saved name and circles to the applied roster.
        applied["members"] = [{key: value for key, value in member.items() if key not in ("name", "circles")}
                              for member in applied["members"]]
    return applied


def expect_roster(node, database, revision, member=None):
    config = roster(node, database)
    require(config.get("revision") == str(revision) and config.get("members") == ([member] if member else []),
            "people_applied_configuration")


def share(publisher, database, contact, node_key, revision):
    arguments = ["invite", database, node_key]
    if contact is not None:
        arguments.extend(["--name", "Ana", "--contact", str(contact)])
    receipt = people(publisher, *arguments)
    require(receipt["databaseId"] == database and receipt["revision"] == str(revision)
            and isinstance(receipt.get("invitation"), str) and bool(receipt["invitation"]),
            "people_invitation_handoff")
    expect_roster(publisher, database, revision, {"nodeKey": node_key, "role": "read", "state": "active"})
    return receipt


def revoke(publisher, database, reader_key, revision):
    result = people(publisher, "grant", "revoke", database, "Ana", "--apply")
    require(result == {"applied": True, "databaseId": database, "revision": str(revision),
                       "nodeKey": reader_key, "state": "revoked"}, "people_revoke_ack")
    expect_roster(publisher, database, revision, {"nodeKey": reader_key, "role": "read", "state": "revoked"})


def transfer(programs, root, publisher, recipient, api_key):
    name = f"ci-people-{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}"
    if revision := os.environ.get("PEOPLE_INSTALL_REVISION"):
        install(publisher, root, revision)
    # The recipient has no cloud account. Create must still place the schemas and say the map is pending.
    expect_setup_member_writes_pending(recipient, name + "-offline")
    created = sdk(publisher, "create", name=name)
    database = str(uuid.UUID(created["id"]))
    require(database == created["id"] and created["spec"] == SPEC, "people_created_database")
    summary(f"Owned DEV mutation log `{database}` for `{name}`.")
    print(f"People DEV database {database}", flush=True)
    schema = json.loads((SETUP / "Album.json").read_text())
    publisher.call(f"/api/databases/{database}/apps/declare-schema", schema)
    write_album(publisher, database, 0)
    # Enroll the stopped publisher and verify the next boot, as in people_circles.
    # Live enrollment can re-exec the daemon with the same PID and a new start time.
    publisher.stop()
    connected = programs.command(publisher.home, [
        "backup", "connect", "--existing", "--env", "dev", "--api-key-stdin",
        "--expected-user-hash", publisher.account, "--json",
    ], input_bytes=api_key, timeout=20)
    require(connected["environment"] == "dev" and connected["account_id"] == publisher.account
            and connected["credentials_saved"] is True, "people_dev_connection")
    publisher.keep_saved_credentials = True
    publisher.start()
    # The publisher is connected, so setup creation can store the member-write map.
    verify_shared_albums_setup(publisher, name + "-albums")
    contact = root / "reader.contact"
    reader_key = programs.command(recipient.home, [
        "share", "contact", "--out", str(contact),
    ])["recipient_public_key"]
    publisher_key = programs.command(publisher.home, [
        "share", "contact", "--out", str(root / "publisher.contact"),
    ])["recipient_public_key"]
    require(reader_key != publisher_key, "people_separate_identity")
    retry("people_initial_configuration", lambda: roster(publisher, database),
          lambda config: config.get("revision") == "0" and config.get("members") == [])
    person = people(publisher, "person", "add", "Ana", "--node-key", reader_key, "--contact", str(contact))
    require(person == {"name": "Ana", "nodeKey": reader_key}
            and person in people(publisher, "person", "list"), "people_app_saved_contact")
    share_albums(publisher, recipient, name, database, reader_key, publisher_key)
    check_role_and_overview(programs, root, publisher, database, name, contact, reader_key)


def verify_shared_albums_setup(node, name):
    setup = {"setupName": "shared-albums", "requiredVersion": 1}
    created = people(node, "db", "create", name, "--setup", setup["setupName"])
    database = created["databaseId"]
    require(str(uuid.UUID(database)) == database and created["name"] == name
            and created["setupName"] == setup["setupName"] and created["version"] == 1
            and created["databaseId"] == database and created["memberWrites"] != "pending",
            "people_named_setup_database")
    definitions = created["definitions"]
    require(set(definitions) == {"social/Album", "social/AlbumItem", "social/Photo", "social/Comment"},
            "people_named_setup_definitions")
    reopened = sdk(node, "setup", **setup, databaseId=database)
    repeated = sdk(node, "setup", **setup, localName=name)
    require(reopened == repeated == created, "people_named_setup_stable_id")
    incomplete = sdk(node, "create-shared", name=f"{name}-repair")
    recovered = sdk(node, "setup", **setup, databaseId=incomplete["id"], finishExisting=True)
    require(recovered["databaseId"] == incomplete["id"]
            and recovered["definitions"] == definitions, "people_named_setup_repaired_by_id")
    expected_writes = json.loads((SETUP / "member-writes.json").read_text())
    for database_id in (database, recovered["databaseId"]):
        stored = node.call(f"/api/databases/{database_id}/share/config").get("memberWrites")
        require(stored == expected_writes, "people_named_setup_member_writes")
    people(node, "db", "verify", database, "--setup", setup["setupName"], "--version", "2",
           refusal=b"supported People database setup")
    write_album(node, database, 0)
    selections = [(app, pick(node, {"name": "whole-database", "version": 1}, "read", database))
                  for app in ("album-viewer", "photo-journal")]
    require(all(selection["databaseId"] == database for _, selection in selections)
            and read_album(node, selections, database, "album-0", ALBUMS[0]),
            "people_named_setup_two_apps")
    schema = json.loads((SETUP / "Album.json").read_text())
    schema["schema"]["fields"].append("event_label")
    schema["schema"]["field_types"]["event_label"] = "String"
    schema["schema"]["field_descriptions"]["event_label"] = "Optional label for the occasion."
    upgraded = node.call(f"/api/databases/{database}/apps/declare-schema", schema)["definition_id"]
    reopened = sdk(node, "setup", **setup, databaseId=database)
    require(reopened["databaseId"] == database and reopened["definitions"][SCHEMA] == upgraded
            and definitions[SCHEMA] != upgraded, "people_named_setup_compatible_expansion")
    require(read_album(node, selections, database, "album-0", ALBUMS[0]),
            "people_named_setup_upgrade_kept_record")
    message = "Two scoped apps read the same album, and shared-albums v1 reopened after an additive schema upgrade."
    print(message, flush=True)
    summary(message)


def share_albums(publisher, recipient, name, database, reader_key, publisher_key):
    try:
        receipt = sdk(publisher, "invite", database=name, recipient={"name": "Ana", "nodeKey": reader_key})
        require(receipt["databaseId"] == database and receipt["publisher"] == publisher_key and receipt["revision"] == "1",
                "people_publisher_identity")
        received = sdk(recipient, "accept", publisher=publisher_key, name="family", receipt=receipt)
        require(received.get("id") == database and received.get("spec") == SPEC
                and received.get("access") == "reader", "people_received_metadata")
        wait_for_album(recipient, database, 0)
        recipient.stop()
        recipient.start()
        listed = next(row for row in recipient.call("/api/databases")["databases"]
                      if row["id"] == database)
        require(listed.get("spec") == SPEC, "people_spec_after_restart")
        observe_access(recipient, database, "active", 1)
        selections = open_content_apps(recipient, database)
        require(read_album(recipient, selections, database, "album-0", ALBUMS[0]), "people_apps_initial_album")
        grants = people(publisher, "grant", "list", database)
        require(len(grants) == 1 and grants[0]["user"] == "Ana", "people_remembers_contact")
        share(publisher, database, None, reader_key, 1)
        write_album(publisher, database, 1)
        wait_for_album(recipient, database, 1)
        require(read_album(recipient, selections, database, "album-1", ALBUMS[1]), "people_apps_second_album")
        require(sdk(publisher, "revoke", database=name, nodeKey=reader_key) == "2", "people_sdk_revoke")
        expect_roster(publisher, database, 2, {"nodeKey": reader_key, "role": "read", "state": "revoked"})
        write_album(publisher, database, 2)
        for _ in range(12):
            require(album(recipient, database, 2) == [], "people_revoked_read_new_album")
            time.sleep(1)
        observe_access(recipient, database, "revoked")
        require(read_album(recipient, selections, database, "album-2")
                and read_album(recipient, selections, database, "album-0", ALBUMS[0]), "people_apps_revoked")
        wait_for_album(recipient, database, 0)
        share(publisher, database, None, reader_key, 3)
        wait_for_album(recipient, database, 2)
        observe_access(recipient, database, "active", 3)
        require(read_album(recipient, selections, database, "album-2", ALBUMS[2]), "people_apps_caught_up")
    finally:
        applied = roster(publisher, database)
        if any(member["state"] == "active" for member in applied["members"]):
            final_revision = int(applied["revision"]) + 1
            revoke(publisher, database, reader_key, final_revision)
            print(f"Final ExeMem revoke acknowledged at revision {final_revision}.", flush=True)
            summary(f"Final ExeMem revoke acknowledged at revision {final_revision}.")
    message = ("The People SDK created a named database and returned a sealed invitation when called by name. "
               "The recipient opened the same database ID through the SDK. "
               "ExeMem confirmed grant, revoke and regrant. The recipient kept the People v1 spec across "
               "restart, read later albums, stopped receiving them after revoke, and caught up after regrant.")
    print(message, flush=True)
    summary(message)
    report_content_flow()


def check_role_and_overview(programs, root, publisher, other_database, name, contact, reader_key):
    # Use a second generated database so the original transfer keeps its four revisions.
    created = people(publisher, "db", "create", f"{name}-roles", "--filtered")
    database = str(uuid.UUID(created["id"]))
    summary(f"Owned DEV role-check mutation log `{database}`.")
    print(f"People DEV role-check database {database}", flush=True)
    retry("people_role_configuration", lambda: roster(publisher, database),
          lambda config: config.get("revision") == "0")
    grant_file = root / "contributor.grant"
    arguments = ("invite", database, reader_key)
    try:
        programs.command(publisher.home, [
            "--db", database, "share", "contributors", "grant", "--recipient", str(contact),
            "--recipient-key", reader_key, "--out", str(grant_file),
        ], timeout=30)
        contributor = {"nodeKey": reader_key, "role": "submit", "state": "active"}
        expect_roster(publisher, database, 1, contributor)
        people(publisher, *arguments, refusal=b"already has contributor access")
        expect_roster(publisher, database, 1, contributor)
        people(publisher, "grant", "revoke", database, reader_key, "--apply")
        expect_roster(publisher, database, 2, {**contributor, "state": "revoked"})
        share(publisher, database, None, reader_key, 3)
        check_overview(publisher, other_database, database, reader_key)
    finally:
        applied = roster(publisher, database)
        if any(member["state"] == "active" for member in applied.get("members", [])):
            people(publisher, "grant", "revoke", database, reader_key, "--apply")
    expect_roster(publisher, database, 4, {"nodeKey": reader_key, "role": "read", "state": "revoked"})
    message = ("People refused an active contributor, restored a revoked contributor as a reader, "
               "and kept a confirmed roster visible while another database's roster was unavailable.")
    print(message, flush=True)
    summary(message)


def check_overview(publisher, busy_database, confirmed_database, reader_key):
    # Hold the existing update lock on this generated home to make one real
    # LastDB roster temporarily unavailable. No data changes under this lock.
    lock = publisher.home / "share-members" / f"{busy_database}.lock"
    with lock.open("rb") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        configurations = {target: people(publisher, "db", "show", target)["sharing"]
                          for target in (busy_database, confirmed_database)}
        require(configurations[busy_database].get("status") == "unavailable"
                and configurations[busy_database].get("httpStatus") == 503
                and configurations[confirmed_database].get("revision") == "3", "people_partial_rosters")
        rows = {row["databaseId"]: row for row in people(publisher, "grant", "list")}
        require(rows[busy_database].get("status") == "unavailable"
                and rows[confirmed_database].get("nodeKey") == reader_key
                and rows[confirmed_database].get("role") == "read", "people_partial_overview")


if __name__ == "__main__":
    run_operation(transfer)
