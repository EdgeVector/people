"""What a people-direct wait saw, and a compact dump of each node.

failure() keeps the last 600 characters of command_output. Callers trim
before that cut so the record reads stay ahead of the node dump. people-direct
passes evidence=True. That appends a publish section and a direct-logs
section, and failure() then keeps 65536 characters. The compact node line
is unchanged. direct-logs is still attached when an earlier read fails.
The publish section is built in direct_publish.py.

A node entry is files=cursor/prefix/error from log-sync/<database id>/,
then ops (a call containing x/databases or /v1/write, or a refused sync,
backup, mutation, or share call), then sync=connected/captures_accepted/
payment_hold, err (the cloud sync last_error), and n (how many databases
that status lists).
"""

import re
import time

from direct_nodes import clip_public, failure_evidence, node_dump
from direct_publish import member_publish
from share_node import keyed
from support import OperationError, redact_command_text


MARKER = " | nodes="


def label(value):
    """A short enum. Anything else becomes a mark, never the raw value."""
    if value is None:
        return "-"
    if value is True:
        return "1"
    if value is False:
        return "0"
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,24}", value):
        return value
    return "?"


def watch(stage, read, done, describe, observe=None):
    """Retry for 150 seconds, the same bound as the other sharing waits.

    The node pauses 1s when idle, 5s on retry, 30s for a snapshot, and 60s
    on backoff. A payment hold waits 300s. 150s covers a retry and a backoff.
    observe runs on each pass and once more at the end. It cannot fail the wait.
    """
    deadline = time.monotonic() + 150
    last_error = None
    seen = None
    while time.monotonic() < deadline:
        safe_observe(observe)
        try:
            value = read()
        except OperationError as error:
            last_error = error
        else:
            seen = value
            last_error = None
            try:
                ready = done(value)
            except OperationError as error:
                safe_observe(observe)
                remember(error, describe, value, "differed")
                raise
            if ready:
                safe_observe(observe)
                return value
        time.sleep(2)
    safe_observe(observe)
    reason = "differed" if present(seen) else "timed_out"
    text = with_read_error(safe_describe(describe, seen, reason), last_error)
    fields = refusal_fields(seen, last_error)
    raise OperationError(stage, command_output=(text[:560], ""), **fields)


def watch_rows(named, database, key, stage, done, observe=None):
    """Read one key on each named node. A node that errors stays in the report."""
    names = [name for name, _node in named]
    state = {"notes": []}

    def read():
        rows = []
        notes = []
        for name, node in named:
            try:
                rows.append(keyed(node, database, [key])[0])
            except OperationError as error:
                rows.append(None)
                notes.append("%s=%s" % (name, refusal_note(error)))
        state["notes"] = notes
        return rows

    def describe(rows, reason):
        text = row_report(names, rows, reason)
        if state["notes"]:
            text = "%s unread=%s" % (text, ",".join(state["notes"]))
        return text

    def ready(rows):
        return not state["notes"] and done(rows)

    return watch(stage, read, ready, describe, observe)


def row_report(names, rows, reason):
    if not isinstance(rows, list):
        return "%s unread" % reason
    parts = [reason]
    for index, name in enumerate(names):
        row = rows[index] if index < len(rows) else None
        parts.append("%s %s" % (name, one_row(row)))
    return " ".join(parts)


def access_report(name, shown, reason):
    access = None
    if isinstance(shown, dict):
        database = shown.get("database")
        if isinstance(database, dict):
            access = database.get("selfAccess")
    if not isinstance(access, dict):
        return "%s %s access=absent" % (reason, name)
    return "%s %s role=%s" % (reason, name, label(access.get("role")))


def attach(error, roster, database, evidence=False):
    """Add one compact node entry. A second call leaves an existing dump.

    evidence adds the publish section, then each node's daemon lines and
    log-sync files. Circles leave it off, so their detail stays inside the
    600 character tail. A failed dump or publish read still leaves direct-logs.
    """
    existing = safe_detail(error)
    if existing is None:
        return
    dump = guarded(lambda: node_dump(roster, database)[:300], "dump=failed")
    text = existing + MARKER + dump if existing else "nodes=" + dump
    if not evidence:
        # Keep relay last_error and raw SDK status bodies readable.
        error.command_output = (text[:2000], "")
        return
    text += "\npublish\n" + guarded(lambda: member_publish(roster, database), "publish=unread")
    text += "\ndirect-logs\n" + guarded(lambda: failure_evidence(roster, database), "evidence=failed")
    error.command_output = (text, "")


def safe_observe(observe):
    if observe is None:
        return
    try:
        observe()
    except Exception:
        return


def safe_detail(error):
    try:
        return current_detail(error)
    except Exception:
        return ""


def guarded(read, fallback):
    try:
        value = read()
    except Exception:
        return fallback
    return value if isinstance(value, str) and value else fallback


def one_row(row):
    if not isinstance(row, dict):
        return "absent"
    return "text=%s created_by=%s updated_by=%s" % (
        shown_text(row), short_author(row, "created_by"), short_author(row, "updated_by"))


def shown_text(row):
    fields = row.get("fields") if isinstance(row, dict) else None
    text = fields.get("text") if isinstance(fields, dict) else None
    if not isinstance(text, str):
        return "-"
    return clip_public(text.replace("\n", " "), 16)


def short_author(row, name):
    value = row.get(name) if isinstance(row, dict) else None
    if not isinstance(value, str) or len(value) < 12:
        return "-"
    if re.fullmatch(r"[0-9a-fA-F]{12,128}", value):
        return value[:12].lower()
    return "?"


def present(value):
    if isinstance(value, list):
        return any(item is not None for item in value)
    return value is not None


def safe_describe(describe, value, reason):
    try:
        text = describe(value, reason)
    except Exception:
        text = reason
    if not isinstance(text, str) or not text:
        text = reason
    return text


def remember(error, describe, value, reason):
    if error.command_output:
        return
    error.command_output = (safe_describe(describe, value, reason)[:560], "")


def with_read_error(text, error):
    if error is None or not error.command_output:
        return text
    stderr, stdout = error.command_output
    raw = stderr if isinstance(stderr, str) and stderr else stdout
    if not isinstance(raw, str) or not raw:
        return text
    tail = redact_command_text(raw)[:60]
    return "%s read_error=%s" % (text, tail) if tail else text


def refusal_fields(seen, error):
    if seen is not None or error is None:
        return {}
    fields = {}
    if error.socket_refusal is not None:
        fields["socket_refusal"] = error.socket_refusal
    if error.cli_refusal is not None:
        fields["cli_refusal"] = error.cli_refusal
    return fields


def refusal_note(error):
    refusal = error.socket_refusal if isinstance(error.socket_refusal, dict) else {}
    status = refusal.get("status")
    head = refusal.get("error_head")
    if not isinstance(head, str) or not head:
        cli = error.cli_refusal if isinstance(error.cli_refusal, dict) else {}
        head = cli.get("error_head") or cli.get("reason") or "unread"
    status_text = str(status) if type(status) is int else "-"
    return "%s:%s" % (status_text, clip_public(str(head), 24))


def current_detail(error):
    output = getattr(error, "command_output", None)
    if output is None:
        return ""
    if not isinstance(output, tuple) or len(output) != 2:
        return ""
    stderr, stdout = output
    raw = stderr if isinstance(stderr, str) and stderr else stdout
    if not isinstance(raw, str) or not raw:
        return ""
    text = redact_command_text(raw)
    if MARKER in text or text.startswith("nodes="):
        return None
    return text if len(text) <= 240 else text[-200:]
