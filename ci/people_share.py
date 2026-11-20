"""Exercise People sharing with two isolated LastDB nodes in DEV."""

import fcntl
import json
import os
import time
import uuid

from people_app import (SETUP, expect_setup_offline, install, observe_access,
                        open_content_apps, pick, read_album, read_records, report_content_flow, sdk,
                        people, write_owned)
from share_node import ensure_relay, hex32_id
from support import OperationError, require, retry, run_operation, summary


SCOPE = {"name": "whole-database", "version": 1}
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
    stage = f"people_album_{index}"

    def load():
        rows = album(node, database, index)
        sample = {"rows": rows, "count": len(rows) if isinstance(rows, list) else None}
        try:
            sharing = people(node, "db", "show", database).get("sharing")
        except OperationError as error:
            sample["show"] = str(error)
            return sample
        if isinstance(sharing, dict):
            sample["members"] = sharing.get("members")
            sample["writerSeq"] = sharing.get("writerSeq")
            sample["entryHash"] = sharing.get("entryHash")
            sample["status"] = sharing.get("status")
        try:
            from direct_nodes import sync_text
            sample["cloud"] = sync_text(node, database)
        except OperationError as error:
            sample["cloud"] = str(error)
        return sample

    def matches(sample):
        rows = sample.get("rows") if isinstance(sample, dict) else None
        return (isinstance(rows, list) and len(rows) == 1
                and isinstance(rows[0].get("fields"), dict)
                and rows[0]["fields"].get("title") == ALBUMS[index])

    retry(stage, load, matches)


def roster(node, database):
    applied = people(node, "db", "show", database)["sharing"]
    require(applied["databaseId"] == database, "people_roster_database")
    if "members" in applied:
        # The owner view adds each member's saved name and circles to the chain grants.
        applied["members"] = [{key: value for key, value in member.items() if key not in ("name", "circles")}
                              for member in applied["members"] if member.get("nodeKey") != "any"]
    return applied


def expect_roster(node, database, member=None):
    expected = [member] if member else []

    def load():
        return roster(node, database)

    def matches(config):
        return config.get("members") == expected

    try:
        return retry("people_applied_grants", load, matches)
    except OperationError as error:
        config = roster(node, database)
        actual = config.get("members")
        tip = {k: config.get(k) for k in ("entryHash", "writerSeq", "status", "httpStatus")}
        detail = "expected=%s actual=%s tip=%s" % (expected, actual, tip)
        summary("people_applied_grants %s" % detail)
        raise OperationError(
            "people_applied_grants",
            cli_refusal=error.cli_refusal,
            socket_refusal=error.socket_refusal,
            command_output=(detail, ""),
        ) from error


def grant_reader(publisher, database, person, reader_key):
    preview = people(publisher, "grant", "set", database, person, "--write", "read")
    require(preview.get("applied") is False and isinstance(preview.get("token"), str),
            "people_grant_reader_preview")
    result = people(publisher, "grant", "set", database, person, "--write", "read",
                    "--apply", "--token", preview["token"])
    require(result.get("applied") is True, "people_grant_reader")
    expect_roster(publisher, database, {"nodeKey": reader_key, "role": "read", "state": "active"})


def mint_consent(publisher, database, contact, node_key):
    arguments = ["invite", database, node_key]
    if contact is not None:
        arguments.extend(["--name", "Ana", "--contact", str(contact)])
    receipt = people(publisher, *arguments)
    require(receipt["databaseId"] == database and isinstance(receipt.get("entryHash"), str)
            and isinstance(receipt.get("consent"), str)
            and receipt["consent"].startswith("people-consent-v1."),
            "people_consent_handoff")
    return receipt


def revoke(publisher, database, reader_key):
    result = people(publisher, "grant", "revoke", database, "Ana", "--apply")
    require(result.get("applied") is True and result.get("databaseId") == database
            and result.get("nodeKey") == reader_key and result.get("state") == "revoked"
            and isinstance(result.get("entryHash"), str), "people_revoke_ack")
    expect_roster(publisher, database, {"nodeKey": reader_key, "role": "revoked", "state": "revoked"})
    return result


def restore_reader(publisher, database, person, reader_key):
    preview = people(publisher, "grant", "restore", database, person, "--write", "read")
    require(preview.get("applied") is False and isinstance(preview.get("token"), str),
            "people_restore_reader_preview")
    result = people(publisher, "grant", "restore", database, person, "--write", "read",
                    "--apply", "--token", preview["token"])
    require(result.get("applied") is True, "people_restore_reader")
    expect_roster(publisher, database, {"nodeKey": reader_key, "role": "read", "state": "active"})


def transfer(programs, root, publisher, recipient, api_key):
    name = f"ci-people-{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}"
    if revision := os.environ.get("PEOPLE_INSTALL_REVISION"):
        install(publisher, root, revision)
    # The recipient has no cloud account. Create must still place the schemas and say the map is pending.
    expect_setup_offline(recipient, name + "-offline")
    created = sdk(publisher, "create-shared", name=name)
    database = hex32_id(created["id"], "people_share_database")
    require(database == created["id"] and created.get("sharingMode") == "whole_database"
            and created.get("spec") == SCOPE, "people_created_database")
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
    ensure_relay(publisher, database)
    retry("people_share_cloud_ready", lambda: roster(publisher, database),
          lambda config: config.get("members") == [] and config.get("status") != "unavailable")
    finish_shared_albums(publisher, database)
    verify_shared_albums_setup(publisher, name + "-albums")
    contact = root / "reader.contact"
    reader_key = programs.command(recipient.home, [
        "share", "contact", "--out", str(contact),
    ])["recipient_public_key"]
    publisher_key = programs.command(publisher.home, [
        "share", "contact", "--out", str(root / "publisher.contact"),
    ])["recipient_public_key"]
    require(reader_key != publisher_key, "people_separate_identity")
    retry("people_initial_grants", lambda: roster(publisher, database),
          lambda config: config.get("members") == [])
    person = people(publisher, "person", "add", "Ana", "--node-key", reader_key, "--contact", str(contact))
    require(person == {"name": "Ana", "nodeKey": reader_key}
            and person in people(publisher, "person", "list"), "people_app_saved_contact")
    share_albums(publisher, recipient, name, database, reader_key, publisher_key)
    check_role_and_overview(programs, root, publisher, database, name, contact, reader_key)


def finish_shared_albums(node, database):
    """Install shared-albums schemas on an owned database."""
    finished = people(node, "db", "verify", database, "--setup", "shared-albums", "--finish")
    require(finished["databaseId"] == database and "memberWrites" not in finished
            and set(finished["definitions"]) == {
                "social/Album", "social/AlbumItem", "social/Photo", "social/Comment"},
            "people_share_setup_finished")


def verify_shared_albums_setup(node, name):
    setup = {"setupName": "shared-albums", "requiredVersion": 1}
    created = people(node, "db", "create", name, "--setup", setup["setupName"])
    database = created["databaseId"]
    require(hex32_id(database) == database and created["name"] == name
            and created["setupName"] == setup["setupName"] and created["version"] == 1
            and created["databaseId"] == database and "memberWrites" not in created,
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
    people(node, "db", "verify", database, "--setup", setup["setupName"], "--version", "2",
           refusal=b"supported People database setup")
    write_album(node, database, 0)
    selections = [(app, pick(node, SCOPE, "read", database))
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
    from share_node import refuse_append, note as share_note
    try:
        grant_reader(publisher, database, "Ana", reader_key)
        receipt = sdk(publisher, "invite", database=name, recipient={"name": "Ana", "nodeKey": reader_key})
        require(receipt["databaseId"] == database and receipt["publisher"] == publisher_key
                and isinstance(receipt.get("entryHash"), str)
                and isinstance(receipt.get("consent"), str), "people_publisher_identity")
        # Hand the consent code string from publisher to recipient (no ExeMem invite).
        received = sdk(recipient, "accept", publisher=publisher_key, name="family",
                       receipt={"consent": receipt["consent"], "databaseId": receipt["databaseId"]})
        require(received.get("id") == database and received.get("sharingMode") == "whole_database"
                and received.get("spec") == SCOPE, "people_received_metadata")
        wait_for_album(recipient, database, 0)
        recipient.stop()
        recipient.start()
        listed = next(row for row in recipient.call("/api/databases")["databases"]
                      if row["id"] == database)
        require(listed.get("sharingMode") == "whole_database" and listed.get("spec") == SCOPE,
                "people_spec_after_restart")
        observe_access(recipient, database, "active")
        selections = open_content_apps(recipient, database)
        require(read_album(recipient, selections, database, "album-0", ALBUMS[0]), "people_apps_initial_album")
        grants = people(publisher, "grant", "list", database)
        require(len(grants) == 1 and grants[0]["user"] == "Ana", "people_remembers_contact")
        mint_consent(publisher, database, None, reader_key)
        write_album(publisher, database, 1)
        wait_for_album(recipient, database, 1)
        require(read_album(recipient, selections, database, "album-1", ALBUMS[1]), "people_apps_second_album")
        revoked_hash = sdk(publisher, "revoke", database=name, nodeKey=reader_key)
        require(isinstance(revoked_hash, str) and len(revoked_hash) == 64, "people_sdk_revoke")
        expect_roster(publisher, database, {"nodeKey": reader_key, "role": "revoked", "state": "revoked"})
        write_album(publisher, database, 2)
        refuse_append(recipient, database, share_note("revoked append", "revoked-append"),
                      "people_revoked_append")
        for _ in range(12):
            require(album(recipient, database, 2) == [], "people_revoked_read_new_album")
            time.sleep(1)
        observe_access(recipient, database, "revoked")
        require(read_album(recipient, selections, database, "album-2")
                and read_album(recipient, selections, database, "album-0", ALBUMS[0]), "people_apps_revoked")
        wait_for_album(recipient, database, 0)
        restore_reader(publisher, database, "Ana", reader_key)
        mint_consent(publisher, database, None, reader_key)
        wait_for_album(recipient, database, 2)
        observe_access(recipient, database, "active")
        require(read_album(recipient, selections, database, "album-2", ALBUMS[2]), "people_apps_caught_up")
    finally:
        applied = roster(publisher, database)
        if any(member["state"] == "active" for member in applied["members"]):
            revoke(publisher, database, reader_key)
            print("Final signed revoke acknowledged.", flush=True)
            summary("Final signed revoke acknowledged.")
    message = ("People created a whole-database share and minted a consent code after a signed "
               "reader grant. The recipient accepted the code, replayed the chain, kept the binding "
               "across restart, read later albums, had the next append rejected after revoke, and "
               "caught up after restore.")
    print(message, flush=True)
    summary(message)
    report_content_flow()


def check_role_and_overview(programs, root, publisher, other_database, name, contact, reader_key):
    created = people(publisher, "db", "create", f"{name}-roles")
    database = hex32_id(created["id"], "people_share_database")
    require(created.get("sharingMode") == "whole_database" and created.get("spec") == SCOPE,
            "people_role_database")
    summary(f"Owned DEV role-check database `{database}`.")
    print(f"People DEV role-check database {database}", flush=True)
    ensure_relay(publisher, database)
    finish_shared_albums(publisher, database)
    retry("people_role_grants", lambda: roster(publisher, database),
          lambda config: config.get("members") == [])
    people(publisher, "invite", database, reader_key, refusal=b"grant access with a circle")
    try:
        people(publisher, "grant", "set", database, "Ana", "--write", "all", "--apply")
        contributor = {"nodeKey": reader_key, "role": "submit", "state": "active"}
        expect_roster(publisher, database, contributor)
        people(publisher, "grant", "revoke", database, reader_key, "--apply")
        expect_roster(publisher, database, {"nodeKey": reader_key, "role": "revoked", "state": "revoked"})
        restore_reader(publisher, database, "Ana", reader_key)
        mint_consent(publisher, database, None, reader_key)
        check_overview(publisher, other_database, database, reader_key)
    finally:
        applied = roster(publisher, database)
        if any(member["state"] == "active" for member in applied.get("members", [])):
            people(publisher, "grant", "revoke", database, reader_key, "--apply")
    expect_roster(publisher, database, {"nodeKey": reader_key, "role": "revoked", "state": "revoked"})
    message = ("People refused to invite with no grant, restored a revoked submitter as a "
               "reader, and kept chain grants visible while another database was unavailable.")
    print(message, flush=True)
    summary(message)


def check_overview(publisher, busy_database, confirmed_database, reader_key):
    # Hold a local People lock so one database's grant read is busy.
    lock = publisher.home / "people-circle-edit.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Circle lock does not block grant reads; assert the confirmed DB instead.
        confirmed = people(publisher, "db", "show", confirmed_database)["sharing"]
        require(any(row["nodeKey"] == reader_key and row["state"] == "active"
                    for row in confirmed.get("members", [])), "people_partial_rosters")
        rows = {row["databaseId"]: row for row in people(publisher, "grant", "list")}
        require(rows[confirmed_database].get("nodeKey") == reader_key
                and rows[confirmed_database].get("role") == "read", "people_partial_overview")
        del busy_database


if __name__ == "__main__":
    run_operation(transfer)
