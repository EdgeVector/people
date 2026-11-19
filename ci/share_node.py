"""Owner-socket steps the People command does not provide.

People can save a contact, grant a person, seal an invitation, and accept that
invitation. It cannot set read or write, publish a snapshot, mint or accept an
open link, or submit a mutation proposal. These helpers do those steps on the
owner socket. They never print a link, a key, or a credential.
"""

import json
import re
import time
import uuid
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from node import Connection, OwnedNode
from people_app import owned_item, record_key
from support import CREDENTIALS_FILE, OperationError, decode_json, require, summary


SCHEMA = "dev-notes/Note"
READ_ONLY = "a received database is read only"
CANNOT_SUBMIT = "this member cannot submit this shared mutation"


def report(message):
    print(message, flush=True)
    summary(message)


def error_head(raw):
    """Leading static words. Digits and tokens are left out."""
    if not isinstance(raw, (bytes, bytearray)):
        raw = str(raw).encode()
    try:
        body = decode_json(raw)
    except OperationError:
        body = None
    if isinstance(body, dict):
        value = body.get("error") if isinstance(body.get("error"), dict) else body
        for name in ("message", "reason", "error"):
            item = value.get(name)
            if isinstance(item, str) and item:
                raw = item.encode()
                break
    text = raw.decode("utf-8", "replace")
    match = re.match(r"[A-Za-z _:'-]{0,160}", text)
    return (match.group(0).rstrip(" :") if match else "") or "unrecognized"


def pinned(code, status, raw):
    raise OperationError(code, socket_refusal={"status": status, "reason": code, "error_head": error_head(raw)})


def exchange(node, method, path, body=None, *, empty=False, headers=None):
    request_headers = {"X-LastDB-Client": "cloud-backup-ci"}
    if body is not None or empty:
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)
    if empty:
        payload = b""
    elif body is None:
        payload = None
    else:
        payload = json.dumps(body, separators=(",", ":")).encode()
    with closing(Connection(node.socket)) as connection:
        connection.request(method, path, payload, request_headers)
        with connection.getresponse() as reply:
            raw = reply.read(65537)
            require(len(raw) <= 65536, "share_socket_size")
            return reply.status, raw


def decoded(status, raw, code, *, ok=True):
    """Decode one JSON object. App routes such as /v1/read have no ok field."""
    if status != 200:
        pinned(code, status, raw)
    value = decode_json(raw)
    require(isinstance(value, dict) and (value.get("ok") is True or not ok), code)
    return value


def until(stage, read, done):
    """Retry for 150 seconds and keep the last socket refusal."""
    deadline = time.monotonic() + 150
    last = None
    while time.monotonic() < deadline:
        try:
            value = read()
        except OperationError as error:
            last = error
        else:
            if done(value):
                return value
        time.sleep(2)
    if last is not None:
        raise OperationError(stage, socket_refusal=last.socket_refusal, cli_refusal=last.cli_refusal)
    raise OperationError(stage)


def as_int(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def member_wire(member):
    wire = {"nodeKey": member["nodeKey"], "role": member["role"], "state": member["state"]}
    if member.get("regrant"):
        wire["regrant"] = True
    if "writeSchemas" in member:
        wire["writeSchemas"] = member["writeSchemas"]
    return wire


def read_config(node, database):
    status, raw = exchange(node, "GET", f"/api/databases/{database}/share/config")
    if status != 200:
        pinned("share_config_read", status, raw)
    value = decoded(status, raw, "share_config_read")
    require(value.get("databaseId") == database, "share_config_database")
    return value


def await_defaults(node, database):
    def read():
        status, raw = exchange(node, "GET", f"/api/databases/{database}/share/config")
        if status != 200:
            pinned("share_config_pending", status, raw)
        return decoded(status, raw, "share_config_read")

    current = until("share_cloud_ready", read,
                    lambda row: str(row.get("revision")) == "0" and row.get("members") == [])
    require(current.get("read") == "members" and current.get("write") == "proposals"
            and current.get("memberWrites") == {}, "share_default_settings")
    return current


def put_settings(node, database, **settings):
    """Change read, write, or memberWrites and leave the member revision in place."""
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        try:
            current = read_config(node, database)
        except OperationError as error:
            if error.socket_refusal and error.socket_refusal.get("status") != 503:
                raise
            time.sleep(2)
            continue
        body = {"expectedRevision": current["revision"], "requestId": str(uuid.uuid4()),
                "members": [member_wire(item) for item in current["members"]], **settings}
        status, raw = exchange(node, "PUT", f"/api/databases/{database}/share/config", body)
        if status == 503:
            time.sleep(2)
            continue
        if status != 200:
            pinned("share_settings_rejected", status, raw)
        ack = decoded(status, raw, "share_settings_ack")
        require(ack.get("committed") is True and ack.get("requestId") == body["requestId"]
                and ack.get("databaseId") == database and ack.get("revision") == current["revision"],
                "share_settings_ack")
        stored = read_config(node, database)
        require(all(stored.get(key) == value for key, value in settings.items()), "share_settings_stored")
        require(stored.get("revision") == current["revision"]
                and stored.get("members") == current["members"], "share_settings_members")
        return stored
    raise OperationError("share_settings")


def snapshot_note(node, database):
    path = Path(node.home) / "log-sync" / database / "snapshot-error"
    if not path.is_file():
        return "ready false"
    try:
        return error_head(path.read_bytes()[:256])
    except OSError:
        return "snapshot error unreadable"


def watch_snapshot(node, database, nonce, seconds):
    path = "/api/backup/snapshot?database=%s&nonce=%s" % (quote(database, safe=""), quote(nonce, safe=""))
    deadline = time.monotonic() + max(0, seconds)
    while time.monotonic() < deadline:
        status, raw = exchange(node, "GET", path)
        if status != 200:
            time.sleep(1)
            continue
        value = decoded(status, raw, "snapshot_status")
        if value.get("ready") is True:
            through = as_int(value.get("snapshot_through"))
            require(through is not None and as_int(value.get("log_head")) == through, "snapshot_head")
            return through
        if value.get("error"):
            pinned("snapshot_failed", status, raw)
        time.sleep(1)
    return None


def publish_snapshot(node, database):
    """Ask the node to publish a snapshot, then wait until that request is ready.

    `lastdb backup snapshot --wait` prints text, so the owner socket is used.
    A snapshot is not only a timer. The node returns ready false, and hides the
    two wait sentences, until the local cursor matches the cloud head.
    """
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        status, raw = exchange(node, "POST", "/api/backup/snapshot", {"database": database})
        if status == 503:
            time.sleep(2)
            continue
        if status != 200:
            pinned("snapshot_queue_rejected", status, raw)
        queued = decoded(status, raw, "snapshot_queue")
        nonce = queued.get("nonce")
        require(queued.get("database_id") == database and isinstance(nonce, str) and nonce, "snapshot_queue")
        found = watch_snapshot(node, database, nonce, min(70, deadline - time.monotonic()))
        if found is not None:
            require(found > 0, "snapshot_through")
            return found
    raise OperationError("snapshot_ready", socket_refusal={
        "status": 200, "reason": "snapshot_ready", "error_head": snapshot_note(node, database),
    })


def mint_link(node, database):
    status, raw = exchange(node, "POST", f"/api/databases/{database}/share/link", empty=True)
    if status != 200:
        pinned("share_link", status, raw)
    value = decoded(status, raw, "share_link")
    token = value.get("link")
    require(value.get("databaseId") == database and isinstance(token, str)
            and token.startswith("lastdb-read-v1."), "share_link")
    return token


def accept_link(node, token, name):
    status, raw = exchange(node, "POST", "/api/databases/accept", {"link": token, "name": name})
    if status != 200:
        pinned("share_accept_link", status, raw)
    database = decoded(status, raw, "share_accept_link").get("database")
    require(isinstance(database, dict) and database.get("sharingMode") == "whole_database", "share_accept_link")
    return database


def declare(node, database):
    app_id, name = SCHEMA.split("/")
    fields = {"text": "Sample text."}
    return node.call(f"/api/databases/{database}/apps/declare-schema", {
        "app_id": app_id,
        "schema": {
            "name": name, "descriptive_name": name, "purpose_statement": "Disposable DEV note.",
            "schema_type": "Hash", "record_keys": {"primary": {"kind": "hash"}, "alternates": {}},
            "fields": ["text"], "field_types": {"text": "String"}, "field_descriptions": fields,
        },
    })["definition_id"]


def note(text, key):
    return {"schema": SCHEMA, "key_value": {"hash": key}, "mutation_type": "create",
            "record": {"text": text}, "alternates": {}}


def proposal(definition, key, text):
    return {"type": "mutation", "schema": SCHEMA, "definition_id": definition,
            "key_value": {"hash": key, "range": None}, "alternates": {},
            "mutation_type": "create", "expected": {"type": "absent"}, "record": {"text": text}}


def shaped(record):
    row = {"fields": record.get("record") if isinstance(record.get("record"), dict) else {}}
    for name in ("created_by", "updated_by"):
        value = record.get(name)
        require(value is None or isinstance(value, str) and value, "record_author")
        if value is not None:
            row[name] = value
    return row


def keyed(node, database, keys):
    """One aligned page per key. An empty page is an absent record."""
    reads = [{"database": database, "schema": SCHEMA, "key": record_key(key)} for key in keys]
    status, raw = exchange(node, "POST", "/v1/read", {"reads": reads})
    if status != 200:
        pinned("record_read", status, raw)
    pages = decoded(status, raw, "record_read", ok=False).get("results")
    require(isinstance(pages, list) and len(pages) == len(keys), "record_read_pages")
    found = []
    for page in pages:
        records = page.get("records") if isinstance(page, dict) and "error" not in page else None
        require(isinstance(records, list) and len(records) <= 1, "record_read_page")
        found.append(shaped(records[0]) if records else None)
    return found


def write_member(node, database, update):
    status, raw = exchange(node, "POST", "/v1/write", {"mutations": [owned_item(database, update)]})
    if status == 200:
        require(decoded(status, raw, "member_write", ok=False).get("written") == 1, "member_write")
        return "committed"
    if status == 403 and READ_ONLY.encode() in raw:
        return "refused"
    pinned("member_write_rejected", status, raw)


def submit_proposal(node, database, update):
    mutation_id = str(uuid.uuid4())
    status, raw = exchange(node, "POST", f"/api/databases/{database}/submissions", update,
                           headers={"Idempotency-Key": mutation_id})
    if status != 202:
        pinned("proposal_rejected", status, raw)
    value = decode_json(raw)
    require(value.get("ok") is True and value.get("status") == "submitted"
            and value.get("mutation_id") == mutation_id, "proposal_receipt")
    return mutation_id


def refuse_proposal(node, database, update, code):
    mutation_id = str(uuid.uuid4())
    status, raw = exchange(node, "POST", f"/api/databases/{database}/submissions", update,
                           headers={"Idempotency-Key": mutation_id})
    if status == 403 and CANNOT_SUBMIT.encode() in raw:
        return
    pinned(code, status, raw)


def connect(programs, publisher, api_key):
    publisher.stop()
    receipt = programs.command(publisher.home, [
        "backup", "connect", "--existing", "--env", "dev", "--api-key-stdin",
        "--expected-user-hash", publisher.account, "--json",
    ], input_bytes=api_key, timeout=20)
    require(receipt["environment"] == "dev" and receipt["account_id"] == publisher.account
            and receipt["credentials_saved"] is True, "dev_connection")
    publisher.keep_saved_credentials = True
    publisher.start()


def local_node(programs, root, name):
    home = root / name
    initialized = programs.command(None, [
        "admin", "backup", "init", "--into", str(home),
        "--recovery-phrase-out", str(home / "recovery-phrase.txt"), "--json",
    ], timeout=20)
    node = OwnedNode(home, programs, account=initialized["account_id"])
    no_cloud_account(node)
    return node


def no_cloud_account(node):
    require(not (node.home / CREDENTIALS_FILE).exists(), "node_cloud_account")


def node_key(programs, node, root, label):
    key = programs.command(node.home, ["share", "contact", "--out", str(root / f"{label}.contact")])[
        "recipient_public_key"]
    require(isinstance(key, str) and re.fullmatch(r"[0-9a-f]{64}", key), "node_key")
    return key


def snapshot_view(home, database):
    root = Path(home) / "received-shares-v1" / database
    through_path = root / "log" / "lastdb-snapshot-through"
    through = as_int(decode_json(through_path.read_bytes()).get("through")) if through_path.is_file() else None
    prefix_path = Path(home) / "log-sync" / database / "prefix.json"
    prefix = decode_json(prefix_path.read_bytes()) if prefix_path.is_file() else {}
    session_path = Path(home) / "log-sync" / database / "session.json"
    session = decode_json(session_path.read_bytes()) if session_path.is_file() else {}
    require(isinstance(prefix, dict) and isinstance(session, dict), "link_snapshot_files")
    return {"adopted": (root / "snapshot-adopted").is_file(), "absent": (root / "snapshot-absent").is_file(),
            "through": through, "prefix": as_int(prefix.get("through")),
            "prefix_database": prefix.get("database_id"), "cursor": as_int(session.get("cursor"))}


def snapshot_story(home, database, requested):
    """Say whether the reader installed the requested snapshot and then replayed."""
    seen = snapshot_view(home, database)
    require(seen["adopted"] and not seen["absent"] and seen["through"] is not None, "link_reader_skipped_snapshot")
    require(seen["prefix_database"] == database and seen["prefix"] is not None, "link_prefix")
    through, prefix, cursor = seen["through"], seen["prefix"], seen["cursor"]
    require(prefix >= through, "link_prefix_through")
    advanced = prefix > through or (cursor is not None and cursor > through)
    if through == requested and advanced:
        return "started at snapshot %s and replayed to prefix %s" % (through, prefix)
    if through > requested:
        return ("installed snapshot %s already covers requested snapshot %s, so the later "
                "records are not distinguished from that later snapshot") % (through, requested)
    raise OperationError("link_snapshot_observation", socket_refusal={
        "status": 200, "reason": "link_snapshot_observation",
        "error_head": "through %s prefix %s cursor %s requested %s" % (through, prefix, cursor, requested),
    })
