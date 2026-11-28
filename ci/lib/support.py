#!/usr/bin/env python3
"""Private, bounded I/O and the two-node run for the DEV checks.

Carried from LastDB's scripts/cloud_backup/support.py.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time

from harness import summary_line


CHUNK = 1024 * 1024
# The saved cloud credentials of a LastDB home (CLOUD_CREDENTIALS_FILE in lastdb_node).
CREDENTIALS_FILE = "cloud-credentials.json"


# A long base64 or hex token can be a key, a phrase payload, or an invitation.
# A node key is exactly 64 lowercase hex characters and is public in recipe output.
SECRET_TOKEN = re.compile(r"[A-Za-z0-9+/_=-]{40,}")
NODE_KEY = re.compile(r"[0-9a-f]{64}\Z")


class OperationError(Exception):
    """An error code. Command text is attached only so failure() can redact it."""

    def __init__(self, code, service_field=None, *, cli_refusal=None, socket_refusal=None,
                 command_output=None):
        super().__init__(code)
        self.service_field = service_field
        self.cli_refusal = cli_refusal
        self.socket_refusal = socket_refusal
        self.command_output = command_output


def redact_secrets(text):
    """Replace a long token. A node key is public and stays."""
    def keep(match):
        token = match.group(0)
        return token if NODE_KEY.fullmatch(token) else "[redacted]"

    return SECRET_TOKEN.sub(keep, text)


def redact_command_text(text, limit=600):
    """Drop long tokens, then keep the tail. A cut token must not leave a short secret."""
    return redact_secrets(text)[-limit:]


def failure(error):
    """The public failure fields of one error; the code stays the compared value."""
    value = {"failure": str(error)}
    if error.service_field is not None:
        value["service_field"] = error.service_field
    if error.cli_refusal is not None:
        value["cli_refusal"] = error.cli_refusal
    if error.socket_refusal is not None:
        value["socket_refusal"] = error.socket_refusal
    output = error.command_output
    if output is not None:
        stderr, stdout = output
        raw = stderr if stderr else stdout
        # people-direct appends daemon lines and log-sync files after this marker.
        # Other recipes stay on the short tail.
        # Direct evidence keeps the full dump. Other recipes keep enough for a
        # relay status+body and a raw sharing-status JSON (previously 600).
        # Grant-403 lines carry the full relay grant JSON; keep a longer tail.
        if isinstance(raw, str) and "\ndirect-logs\n" in raw:
            limit = 65536
        elif isinstance(raw, str) and "grant-403 " in raw:
            limit = 8000
        else:
            limit = 2000
        value["detail"] = redact_command_text(raw, limit)
    return value


def require(condition, code, detail=None):
    if not condition:
        raise OperationError(code, command_output=(detail, "") if detail else None)


def pairs(items):
    result = {}
    for key, value in items:
        require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def decode_json(raw):
    def reject(_value):
        raise OperationError("nonfinite_json")

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=reject)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise OperationError("invalid_json") from error


def stable(before, after):
    return (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) == (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
    )


def private_file(path, hardlinks=False):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        require(
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid == os.getuid()
            and metadata.st_mode & 0o077 == 0
            and (metadata.st_nlink >= 1 if hardlinks else metadata.st_nlink == 1),
            "private_file",
        )
        return os.fdopen(descriptor, "rb"), metadata
    except BaseException:
        os.close(descriptor)
        raise


def read_json(path, max_bytes=262144):
    stream, before = private_file(Path(path))
    with stream:
        require(before.st_size <= max_bytes, "json_size")
        raw = stream.read(max_bytes + 1)
        require(len(raw) <= max_bytes and stable(before, os.fstat(stream.fileno())), "json_changed")
    return decode_json(raw)


def dev_endpoint():
    # The compiled endpoint profile of the LastDB build under test.
    path = Path(os.environ.get("LASTDB_SOURCE", "")) / "lastdb_node/environments.json"
    require(path.is_file() and path.stat().st_size <= 65536, "compiled_profile")
    value = decode_json(path.read_bytes())
    endpoint = value["environments"]["dev"]["exemem_api"]
    require(
        isinstance(endpoint, str)
        and re.fullmatch(r"https://[a-z0-9]+\.execute-api\.us-west-2\.amazonaws\.com", endpoint),
        "compiled_dev_endpoint",
    )
    return endpoint


@contextmanager
def running(node, *, remove_saved=False):
    try:
        node.start()
        yield
        node.stop(remove_saved=remove_saved)
    finally:
        node.cleanup_stop()


def retry(stage, command, complete, *, seconds=150):
    deadline = time.monotonic() + seconds
    last_error = None
    last_value = None
    while time.monotonic() < deadline:
        try:
            result = command()
        except OperationError as error:
            last_error = error
        else:
            last_value = result
            if complete(result):
                return result
        time.sleep(2)
    detail = timeout_detail(last_error, last_value)
    raise OperationError(
        stage,
        cli_refusal=last_error.cli_refusal if last_error else None,
        socket_refusal=last_error.socket_refusal if last_error else None,
        command_output=detail if detail else (
            last_error.command_output if last_error else None),
    )


def timeout_detail(last_error, last_value):
    """Status + body for a timed-out wait. Secrets are redacted."""
    parts = []
    if last_error is not None:
        if last_error.socket_refusal:
            refusal = last_error.socket_refusal
            parts.append("socket=%s:%s" % (refusal.get("status"), refusal.get("error_head")
                                           or refusal.get("reason")))
        if last_error.cli_refusal:
            parts.append("cli=%s" % redact_command_text(str(last_error.cli_refusal), 200))
        if last_error.command_output:
            stderr, stdout = last_error.command_output
            raw = stderr if stderr else stdout
            if raw:
                parts.append(redact_command_text(str(raw), 400))
    if last_value is not None:
        if isinstance(last_value, dict) and isinstance(last_value.get("grant_403"), str):
            # Full relay grant JSON on its own line (not clipped into last=).
            parts.append(redact_secrets(last_value["grant_403"]))
        try:
            text = json.dumps(last_value, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            text = str(last_value)
        parts.append("last=%s" % redact_command_text(text, 400))
    return ("\n".join(parts), "") if parts else None


def summary(message):
    summary_line(message, echo=False)


def accepted_ci():
    """True for main Actions, or a source-pin workflow_dispatch (any ref).

    pull_request events stay refused. A pinned LastDB SHA on workflow_dispatch
    is how a PR branch proves DEV sharing before merge.
    """
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return False
    if os.environ.get("GITHUB_REF") == "refs/heads/main":
        return True
    if os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        return False
    sha = (os.environ.get("LASTDB_SOURCE_COMMIT") or "").strip().lower()
    return bool(re.fullmatch(r"[0-9a-f]{40}", sha))


def _run_two_node_operation(operation):
    from node import OwnedNode, Programs

    os.umask(0o077)
    parser = argparse.ArgumentParser(description=operation.__module__)
    parser.add_argument("--bin-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    require(accepted_ci(), "share_accepted_ci_only")
    dev_endpoint()
    programs = Programs(args.bin_dir.resolve(strict=True), args.version, dict(os.environ))
    with tempfile.TemporaryDirectory(prefix="ldb-share-", dir="/tmp") as temporary:
        root = Path(temporary).resolve()

        def fresh(name):
            # A new identity made offline. Nothing is shared with another run or
            # another leg: no account, API key or recovery phrase comes from a secret.
            home = root / name
            initialized = programs.command(
                None,
                ["admin", "backup", "init", "--into", str(home),
                 "--recovery-phrase-out", str(home / "recovery-phrase.txt"), "--json"],
                timeout=20,
            )
            return OwnedNode(home, programs, account=initialized["account_id"])

        publisher, recipient = fresh("publisher"), fresh("recipient")
        with running(publisher, remove_saved=True), running(recipient):
            operation(programs, root, publisher, recipient)
    message = "Both generated DEV daemons stopped cleanly and their temporary homes were removed."
    print(message, flush=True)
    summary(message)


def run_operation(operation):
    try:
        _run_two_node_operation(operation)
    except OperationError as error:
        print(json.dumps(failure(error)), flush=True)
        raise SystemExit(1) from None
