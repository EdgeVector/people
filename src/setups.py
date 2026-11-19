"""Set up databases from the versioned schemas shipped with People."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import sys
import time
from uuid import uuid4

from lastdb import (
    SHARE_CONFIRM_SECONDS, LastDBError, PeopleError, call_until_share_confirmed,
    share_configuration_unconfirmed,
)


SHARED_ALBUMS = "shared-albums"
VERSION = 1
ALBUM_SCHEMAS = ("Album", "AlbumItem", "Photo", "Comment")
CATALOG = Path(__file__).resolve().parents[1] / "setups" / "shared-albums-v1"
MEMBER_WRITES = "member-writes.json"
WRITE_BEHAVIOURS = {"owner_only", "own_records", "shared"}
SCHEMA = "people/DatabaseSetup"
# Create must not spend the sharing-update wait on a node that can never confirm.
SETUP_CONFIRM_SECONDS = 20
LABEL = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
DEFINITION_ID = re.compile(r"[0-9a-f]{64}\Z")
CONTRACT_FIELDS = ("descriptive_name", "purpose_statement", "schema_type")


class Setups:
    def __init__(self, node, sharing):
        self.node = node
        self.sharing = sharing

    def open(self, args):
        setup = args["setupName"]
        version = args.get("requiredVersion", VERSION)
        if setup != SHARED_ALBUMS or type(version) is not int or version != VERSION:
            raise PeopleError("choose a supported People database setup and version")
        selectors = set(args) & {"databaseId", "localName"}
        if len(selectors) != 1:
            raise PeopleError("choose one database ID or one new local name")
        selector = selectors.pop()
        value = args[selector]
        if not isinstance(value, str) or not LABEL.fullmatch(value):
            raise PeopleError("enter a valid database ID or local name")
        finish = args.get("finishExisting") is True
        if finish and selector != "databaseId":
            raise PeopleError("select an owned database by ID to finish setup")
        definitions = {}
        for name in ALBUM_SCHEMAS:
            request = json.loads((CATALOG / f"{name}.json").read_text())
            definitions[request["schema"]["name"]] = request
        if selector == "databaseId":
            database = self.database(value)
            if finish:
                if database["kind"] != "shareable" or not database.get("name"):
                    raise PeopleError("select a named database you own to finish setup")
                database, verified, member_writes = self.provision(
                    database["name"], definitions, existing=database,
                    confirm_seconds=SHARE_CONFIRM_SECONDS, required=True)
            else:
                verified = self.verify(value, definitions)
                member_writes = self.member_writes_state(
                    database, read_member_writes(), seconds=SETUP_CONFIRM_SECONDS)
        else:
            database, verified, member_writes = self.provision(
                value, definitions, confirm_seconds=SETUP_CONFIRM_SECONDS, required=False)
        announce_pending(database, setup, member_writes)
        return {"databaseId": database["id"], "name": database.get("name"),
                "setupName": setup, "version": version, "definitions": verified,
                "memberWrites": member_writes}

    def database(self, database_id):
        row = next((row for row in self.sharing.databases() if row["id"] == database_id), None)
        if (row is None or row.get("sharingMode") != "whole_database"
                or row.get("kind") not in ("shareable", "received") or row.get("state") != "ready"):
            raise PeopleError("select a ready whole-database share by ID")
        return row

    def provision(self, name, definitions, *, existing=None, confirm_seconds, required):
        # Reserve the ID before issuing the create request so a lost reply is retryable.
        key = f"{SHARED_ALBUMS}:{VERSION}:{name}"
        saved = self.node.get_record(SCHEMA, key)
        if saved is None:
            saved = self.reserve(name, key, existing)
        database_id = saved["fields"]["database_id"]
        if existing and existing["id"] != database_id:
            raise PeopleError("this setup is already bound to a different database")
        if existing or saved["fields"]["ready"]:
            database = existing or self.database(database_id)
        else:
            database = self.sharing.create(name, whole=True, request_id=database_id)
        if database["id"] != database_id or database["kind"] != "shareable":
            raise PeopleError("this setup belongs to an owned database")
        database_id = database["id"]
        expected = read_member_writes()
        if saved["fields"]["ready"]:
            verified = self.verify(database_id, definitions)
            return database, verified, self.member_writes_state(
                database, expected, seconds=confirm_seconds)
        installed = self.verify(database_id, definitions, allow_missing=True)
        missing = [request for schema, request in definitions.items() if schema not in installed]
        if missing:
            with ThreadPoolExecutor(max_workers=len(missing)) as pool:
                list(pool.map(lambda request: self.install(database_id, request), missing))
        stored = self.ensure_member_writes(
            database_id, expected, seconds=confirm_seconds, required=required)
        verified = self.verify(database_id, definitions)
        if stored is None:
            return database, verified, "pending"
        fields = {"database_id": database_id, "ready": True}
        try:
            self.node.write_record(SCHEMA, key, fields, saved)
        except LastDBError as error:
            if error.status != 409 or (self.node.get_record(SCHEMA, key) or {}).get("fields") != fields:
                raise
        return database, verified, stored

    def reserve(self, name, key, existing):
        if existing is None:
            if any(row.get("name") == name for row in self.sharing.databases()):
                raise PeopleError("that name is already in use; select the owned database by ID to finish setup")
        fields = {"database_id": existing["id"] if existing else str(uuid4()), "ready": False}
        try:
            written = self.node.write_record(SCHEMA, key, fields, None)
        except LastDBError as error:
            if error.status != 409:
                raise
            saved = self.node.get_record(SCHEMA, key)
            if not saved:
                raise
            return saved
        return {"fields": fields, "revision": written["revision"]}

    def install(self, database_id, request):
        try:
            self.node.databases("declare_schema", **declaration(database_id, request))
        except LastDBError as error:
            if error.status != 409:
                raise
            # A concurrent install can win. The full catalog check verifies
            # what actually exists before declaring this setup ready.

    def verify(self, database_id, definitions, *, allow_missing=False):
        listed = self.node.databases(
            "schemas", database=database_id, include_full=True, include_definition_ids=True)
        rows = {row["name"]: row for row in listed["schemas"]}
        verified = {}
        for name, request in definitions.items():
            row = rows.get(name)
            if row is None and allow_missing:
                continue
            if not matches(row, request):
                raise PeopleError(f"{name} does not match the shared-albums version 1 setup")
            verified[name] = row["definition_id"]
        return verified

    def ensure_member_writes(self, database_id, expected, *, seconds, required):
        """Store the setup map. Return it, or None when a create stays unconfirmed.

        Create answers ready before the cloud has a mutation log head. Until
        that head exists, the share configuration read and this write both
        answer the unconfirmed 503. A node with no cloud account returns that
        same sentence, and the app API does not say which one it is. Create
        waits at most 20 seconds and then leaves the map pending. `db verify
        --finish` waits the sharing bound and raises when the map is still
        unconfirmed, because that command's job is to set it. The same body
        is sent again: a lost answer must not sign a second statement.
        """
        if expected is None:
            return {}
        deadline = time.monotonic() + seconds

        def confirmed(operation):
            return call_until_share_confirmed(operation, seconds=max(0, deadline - time.monotonic()))

        try:
            current = confirmed(lambda: self.node.databases("share_config", database=database_id))
        except LastDBError as error:
            if required or not share_configuration_unconfirmed(error):
                raise
            return None
        if current.get("memberWrites") == expected:
            return expected
        request_id = str(uuid4())
        config = {
            "expectedRevision": current["revision"],
            "requestId": request_id,
            "members": current.get("members") or [],
            "memberWrites": expected,
        }
        try:
            ack = confirmed(lambda: self.node.databases(
                "set_share_config", database=database_id, config=config))
        except LastDBError as error:
            if required or not share_configuration_unconfirmed(error):
                raise
            return None
        if (ack.get("databaseId") != database_id or ack.get("requestId") != request_id
                or ack.get("committed") is not True):
            raise PeopleError("LastDB returned a mismatched sharing acknowledgement")
        return expected

    def member_writes_state(self, database, expected, *, seconds):
        """The stored map, or pending when this node cannot confirm the share configuration."""
        if expected is None:
            return {}
        if database.get("kind") != "shareable":
            stored = (database.get("selfAccess") or {}).get("memberWrites")
            return stored if isinstance(stored, dict) else "pending"
        try:
            current = call_until_share_confirmed(
                lambda: self.node.databases("share_config", database=database["id"]),
                seconds=seconds)
        except LastDBError as error:
            if share_configuration_unconfirmed(error):
                return "pending"
            raise
        stored = current.get("memberWrites")
        if stored != expected:
            raise PeopleError("the member-write map does not match the setup")
        return stored if isinstance(stored, dict) else {}


def announce_pending(database, setup, member_writes):
    """One sentence on stderr when this node owns the database and did not store the map."""
    if member_writes != "pending" or database.get("kind") != "shareable":
        return
    database_id = database["id"]
    print(
        "The member-write map was not set because the share configuration is not confirmed, "
        f"so once this node is connected run `people db verify {database_id} --setup {setup} --finish`.",
        file=sys.stderr,
    )


def read_member_writes(catalog=CATALOG):
    """The setup's map, or None when the setup has no member-writes file."""
    path = catalog / MEMBER_WRITES
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise PeopleError("the setup member-write map is invalid") from error
    if (not isinstance(value, dict) or any(
            not isinstance(name, str) or behaviour not in WRITE_BEHAVIOURS
            for name, behaviour in value.items())):
        raise PeopleError("the setup member-write map is invalid")
    return value


def declaration(database_id, request):
    """The declare_schema body. Both sockets send the catalog app_id.

    The catalog name is the stored name, such as social/Album. People does
    not list that name in access.write. The databases extension is the grant
    for this call.
    """
    body = {"database": database_id, "app_id": request["app_id"], "schema": request["schema"]}
    if "retention" in request:
        body["retention"] = request["retention"]
    return body


def matches(row, request):
    if not row or row.get("state") != "Available" or row.get("owner_app_id") != request["app_id"]:
        return False
    definition = row.get("definition_id")
    expected = request["schema"]
    keys = row.get("record_keys") or {}
    required_fields = set(expected["fields"])
    return (isinstance(definition, str) and bool(DEFINITION_ID.fullmatch(definition))
            and row.get("file_required", False) == expected.get("file_required", False)
            and required_fields.issubset(row.get("fields") or [])
            and keys.get("primary") == expected["record_keys"]["primary"]
            and keys.get("alternates", {}) == expected["record_keys"]["alternates"]
            and all(row.get(field) == expected[field] for field in CONTRACT_FIELDS)
            and all((row.get(kind) or {}).get(field) == expected[kind][field]
                    for kind in ("field_types", "field_descriptions") for field in required_fields))
