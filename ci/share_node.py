"""Owner-socket steps the People command does not provide.

People grants, revokes, seals invitations, and accepts them on the signed
chain. These helpers still cover open-link mint/accept, proposal submit, and
direct writes on the owner socket. They never print a link, a key, or a
credential. Snapshots and unsigned share-config settings are gone.
"""

import json
import os
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


def owner_databases(node, action, **fields):
    """One owner-socket POST /v1/x/databases action (signed-history grant surface)."""
    body = {"action": action, **{key: value for key, value in fields.items() if value is not None}}
    status, raw = exchange(node, "POST", "/v1/x/databases", body)
    if status not in (200, 202):
        pinned(f"databases_{action}", status, raw)
    return decoded(status, raw, f"databases_{action}", ok=False)


def chain_grants(node, database):
    reply = owner_databases(node, "grants", database=database)
    require(reply.get("databaseId") == database, "chain_grants_database")
    return reply


def await_empty_grants(node, database):
    """Wait until the folded chain has no members (fresh birth)."""
    return until("chain_grants_ready", lambda: chain_grants(node, database),
                 lambda row: (row.get("members") or []) == [] and row.get("any") in (None,))


def owner_grant(node, database, grantee, role):
    """Append a publisher-signed grant on the owner socket (outside People)."""
    ack = owner_databases(node, "grant", database=database, grantee=grantee, role=role)
    require(ack.get("databaseId") == database and ack.get("role") == role
            and isinstance(ack.get("entryHash"), str) and type(ack.get("writerSeq")) is int,
            "owner_grant_ack")
    return ack


def owner_revoke(node, database, grantee):
    ack = owner_databases(node, "revoke", database=database, grantee=grantee)
    require(ack.get("databaseId") == database and ack.get("role") == "revoked"
            and isinstance(ack.get("entryHash"), str), "owner_revoke_ack")
    return ack


def grant_public_read(node, database):
    """Deliver mode: grant#any read on the signed chain."""
    return owner_grant(node, database, "any", "read")


def revoke_public_read(node, database):
    return owner_revoke(node, database, "any")


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


def refuse_append(node, database, update, code):
    """A revoked or non-grantee member's next append must be rejected."""
    status, raw = exchange(node, "POST", "/v1/write", {"mutations": [owned_item(database, update)]})
    if status in (401, 403):
        return
    pinned(code, status, raw)


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


HEX32 = re.compile(r"[0-9a-f]{64}\Z")
RELAY_ORIGIN = re.compile(
    r"https://[a-z0-9]+\.execute-api\.[a-z0-9-]+\.amazonaws\.com\Z")


def hex32_id(value, code="hex32_id"):
    """Database and stream/device ids are 64 lowercase hex (not UUID)."""
    require(isinstance(value, str) and HEX32.fullmatch(value), code)
    return value


def relay_url():
    """ExeMem DEV relay origin. Paths are /relay/v1/... under this base. No credentials."""
    url = os.environ.get("RELAY_URL", "").strip().rstrip("/")
    require(bool(url) and RELAY_ORIGIN.fullmatch(url), "relay_url")
    return url


def author_relays(node, database, urls=None):
    """Publisher-signed relays entry. Defaults to RELAY_URL from the workflow."""
    urls = urls or [relay_url()]
    reply = owner_databases(node, "relays", database=database, urls=urls)
    require(reply.get("databaseId") == database, "author_relays")
    listed = reply.get("urls")
    if listed is not None:
        require(isinstance(listed, list) and listed == urls, "author_relays_urls")
    return reply


def bootstrap_relay(node, database):
    """People/LastDB bootstrap of the first relay URL from the signed relays entry."""
    reply = owner_databases(node, "bootstrap_relay", database=database)
    require(reply.get("databaseId") == database and isinstance(reply.get("url"), str) and reply["url"],
            "bootstrap_relay")
    return reply


def ensure_relay(node, database):
    """Author the DEV relay URL into the chain, then bootstrap the local node."""
    author_relays(node, database)
    return bootstrap_relay(node, database)
