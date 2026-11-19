"""People's connection to a local LastDB.

Record reads and writes use `POST /v1/read` and `POST /v1/write`. Named
databases and sharing use `POST /v1/x/databases`. The installer places the
schemas in `lastdb-app.json`. People does not declare those while it runs.

The launcher sets `LASTDB_SOCKET_PATH` to the app socket and `LASTDB_APP` to
the app id. The app id is sent as `X-LastDB-App` on `/v1/` calls. Without
them, People uses the owner socket `<home>/data/folddb.sock` and sends no app
id header. `declare_schema` sends the catalog `app_id` on either socket.
"""

from __future__ import annotations

from contextlib import closing
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time


class PeopleError(Exception):
    pass


class LastDBError(PeopleError):
    def __init__(self, method, path, status, body):
        self.status = status
        self.body = body
        self.code, detail = error_detail(body)
        if detail is None:
            detail = body if isinstance(body, str) else "request failed"
        super().__init__(f"{method} {path} returned HTTP {status} ({detail})")


def error_detail(body):
    if not isinstance(body, dict):
        return None, None
    error = body.get("error")
    if isinstance(error, dict):
        return error.get("code"), error.get("message") or error.get("code")
    return error, error


# The only 503 People treats as "the cloud cannot confirm this yet".
# LastDB at nightly 7c389d44f maps every failed member list, including the
# cloud's not-found for a mutation log head that create has not uploaded
# and a node with no cloud account, to this one sentence
# (config.rs set_config / read_config, database_share.rs). The databases
# extension does not say which of the two it is. A settings refusal keeps
# the service text, so it does not match.
SHARE_UNCONFIRMED = "share configuration is not confirmed"
SHARE_CONFIRM_SECONDS = 120


def share_configuration_unconfirmed(error):
    """True only when the reply is that exact 503 sentence."""
    return (isinstance(error, LastDBError) and error.status == 503
            and str(error).endswith(f"({SHARE_UNCONFIRMED})"))


def call_until_share_confirmed(operation, seconds=None):
    """Call until the share configuration can be confirmed, or the bound passes.

    The wait is only for `share configuration is not confirmed`. Another
    status, another 503, and a transport error all surface at once. When
    the wait ends, the last 503 is raised unchanged. The default bound is
    120 seconds, for a sharing update that must commit. A caller that must
    not block on a node with no cloud account passes a shorter bound.
    """
    deadline = time.monotonic() + (SHARE_CONFIRM_SECONDS if seconds is None else seconds)
    pause = 0.5
    while True:
        try:
            return operation()
        except LastDBError as error:
            if not share_configuration_unconfirmed(error):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(pause, remaining))
            pause = min(pause * 2, 5)


class LocalConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=95)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def point_key(key):
    """A hash key, plus a range when the schema has one."""
    if isinstance(key, str):
        return {"hash": key}
    body = {"hash": key["hash"]}
    if key.get("range"):
        body["range"] = key["range"]
    return body


def record_mutation(schema, key, fields, previous):
    """One conditional write. Create when unread; replace or delete at the read revision."""
    mutation = {"schema": schema, "key": point_key(key)}
    if fields is None:
        revision = previous.get("revision") if previous else None
        if not revision:
            raise PeopleError("a delete needs the revision from a read")
        return {**mutation, "op": "delete", "revision": revision}
    if not previous:
        return {**mutation, "op": "create", "record": fields}
    revision = previous.get("revision")
    if not revision:
        raise PeopleError("a replace needs the revision from a read")
    return {**mutation, "op": "replace", "revision": revision, "record": fields}


def read_result(item):
    if not isinstance(item, dict):
        raise PeopleError("LastDB returned an invalid read result")
    if "error" in item:
        raise LastDBError("POST", "/v1/read", 400, item)
    cursor = item.get("next_cursor")
    if "records" in item and "keys" not in item:
        return {"records": [record_row(row) for row in item["records"]], "next_cursor": cursor}
    if "keys" in item and "records" not in item and isinstance(item["keys"], list):
        return {"keys": item["keys"], "next_cursor": cursor}
    raise PeopleError("LastDB returned a read result with no records or keys")


def record_row(record):
    fields = record.get("record") if isinstance(record, dict) else None
    revision = record.get("revision") if isinstance(record, dict) else None
    if not isinstance(fields, dict) or not isinstance(revision, str) or not revision:
        raise PeopleError("LastDB returned an invalid record")
    row = {"fields": fields, "revision": revision, "key": record.get("key"), "file": record.get("file")}
    # The node sets these. A missing key stays omitted. Null is not a value.
    for name in ("created_by", "updated_by"):
        value = record.get(name)
        if value is None:
            continue
        if not isinstance(value, str) or not value:
            raise PeopleError("LastDB returned an invalid record")
        row[name] = value
    return row


class LastDB:
    def __init__(self, home, executable):
        self.home = Path(home).resolve()
        configured = os.environ.get("LASTDB_SOCKET_PATH")
        self.socket = Path(configured) if configured else self.home / "data" / "folddb.sock"
        self.executable = Path(executable).resolve()
        self.app_id = os.environ.get("LASTDB_APP")

    def state_dir(self):
        """People's own files. The launcher names this directory in LASTDB_APP_DATA."""
        configured = os.environ.get("LASTDB_APP_DATA")
        return Path(configured) if configured else self.home

    def scratch(self):
        root = os.environ.get("LASTDB_APP_DATA")
        return tempfile.TemporaryDirectory(prefix="people-contact-", dir=root or None)

    def call(self, method, path, body=None, *, request_id=None, expect_ok=True, statuses=(200,)):
        payload = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        headers = {"X-LastDB-Client": "people", "Content-Type": "application/json"}
        if request_id is not None:
            headers["Idempotency-Key"] = request_id
        if self.app_id and path.startswith("/v1/"):
            headers["X-LastDB-App"] = self.app_id
        with closing(LocalConnection(self.socket)) as connection:
            connection.request(method, path, payload, headers)
            with connection.getresponse() as response:
                raw = response.read(8 * 1024 * 1024 + 1)
                status = response.status
        if len(raw) > 8 * 1024 * 1024:
            raise PeopleError("LastDB reply exceeds the People response limit")
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            result = raw.decode(errors="replace").strip()
        if status not in statuses or not isinstance(result, dict) or (expect_ok and result.get("ok") is not True):
            raise LastDBError(method, path, status, result)
        return result

    def databases(self, action, **fields):
        """One `POST /v1/x/databases` action. A refusal becomes LastDBError.

        `request_id` is the Idempotency-Key header. `create` forwards it.
        Success is the owner route's JSON, including HTTP 202 from `submit`.
        """
        request_id = fields.pop("request_id", None)
        body = {"action": action}
        body.update({key: value for key, value in fields.items() if value is not None})
        return self.call("POST", "/v1/x/databases", body, request_id=request_id, statuses=(200, 202))

    def read(self, reads):
        if not 1 <= len(reads) <= 64:
            raise PeopleError("a read holds 1 to 64 items")
        body = self.call("POST", "/v1/read", {"reads": reads}, expect_ok=False)
        results = body.get("results")
        if not isinstance(results, list) or len(results) != len(reads):
            raise PeopleError("LastDB returned an incomplete read")
        return [read_result(item) for item in results]

    def write(self, mutations):
        if not mutations:
            raise PeopleError("a write holds at least one mutation")
        body = self.call("POST", "/v1/write", {"mutations": mutations}, expect_ok=False)
        revisions = body.get("revisions")
        if (body.get("written") != len(mutations) or not isinstance(revisions, list)
                or len(revisions) != len(mutations)
                or any(not isinstance(item, str) or not item for item in revisions)):
            raise PeopleError("LastDB returned an incomplete write")
        return {"written": len(mutations), "revisions": revisions}

    def get_record(self, schema, key):
        records = self.read([{"schema": schema, "key": {"hash": key}}])[0]["records"]
        return records[0] if records else None

    def read_range(self, schema, hash_key, *, limit=None, cursor=None, keys_only=False):
        item = {"schema": schema, "range": {"hash": hash_key}}
        if limit is not None:
            item["limit"] = limit
        if cursor:
            item["cursor"] = cursor
        if keys_only:
            item["keys_only"] = True
        return self.read([item])[0]

    def read_keys(self, schema, keys):
        if not keys:
            return []
        pages = self.read([{"schema": schema, "key": point_key(key)} for key in keys])
        return [page["records"][0] if page["records"] else None for page in pages]

    def write_record(self, schema, key, fields, previous):
        written = self.write([record_mutation(schema, key, fields, previous)])
        return {"revision": written["revisions"][0]}

    def command(self, *arguments):
        # `share contact` and `share verify-contact` read the data directory
        # and do not open a socket. The program name is `lastdb`, so the
        # launcher does not intercept this child.
        command = [str(self.executable), "--data-dir", str(self.home), *arguments]
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        if result.returncode:
            raise PeopleError(result.stderr.strip() or f"LastDB {arguments[0]} failed")
        return json.loads(result.stdout)

    def verify_contact(self, path, expected_key):
        return self.command("share", "verify-contact", str(path), "--recipient-key", expected_key)

    def verify_saved_contact(self, code, expected_key):
        with self.scratch() as directory:
            path = Path(directory) / "recipient.contact"
            path.write_text(code, encoding="ascii")
            return self.verify_contact(path, expected_key)

    def own_key(self):
        with self.scratch() as directory:
            path = Path(directory) / "publisher.contact"
            return self.command("share", "contact", "--out", str(path))["recipient_public_key"]
