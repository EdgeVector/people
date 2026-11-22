"""Relay 403 diagnostics, and the entries each node holds for a database."""

import re
from pathlib import Path

from direct_nodes import (
    DATABASE_ID,
    clip_public,
    cloud_sync,
    files_text,
    owner_status,
    read_limited,
    sync_text,
)
from support import OperationError, decode_json, redact_secrets


def grant_state(publisher, reader, database):
    """Sync state and log-sync files beside a relay 403; full grant JSON below."""
    parts = [
        "grant-state pub=%s rdr=%s disk=%s" % (
            clip_public(sync_text(publisher, database), 200),
            clip_public(sync_text(reader, database), 200),
            files_text(publisher, database),
        )
    ]
    upload = upload_error_line(publisher)
    if upload:
        parts.append(upload)
    for label, node in (("publisher", publisher), ("Ana", reader)):
        line = grant_403_for_node(node, database)
        if line:
            parts.append("%s %s" % (label, line))
    return "\n".join(parts)


def grant_403_roster(roster):
    """Per-database grant-403 lines for each node that has a relay grant denial."""
    lines = []
    for name, node in roster:
        line = grant_403_for_node(node)
        if line:
            lines.append("%s %s" % (name, line))
    return "\n".join(lines)


def grant_403_for_node(node, database=None):
    """Per-database grant-403 lines from cloud_sync.databases, not node-wide last_error."""
    cloud = cloud_sync(owner_status(node))
    if not isinstance(cloud, dict):
        return None
    databases = cloud.get("databases")
    if not isinstance(databases, dict):
        return None
    lines = []
    for database_id, item in databases.items():
        if not isinstance(database_id, str) or not isinstance(item, dict):
            continue
        if database is not None and database_id != database:
            continue
        for side in ("upload", "download"):
            progress = item.get(side)
            err = progress.get("last_error") if isinstance(progress, dict) else None
            line = grant_403_line(err)
            if line:
                lines.append("%s %s" % (database_id, line))
    return "\n".join(lines) if lines else None


def grant_403_line(last_error):
    """Full redacted relay grant 403 JSON on one line, or None."""
    if not isinstance(last_error, str) or '"field":"grant"' not in last_error:
        return None
    text = redact_secrets(last_error.replace("\n", " ").strip())
    if not text:
        return None
    brace = text.find("{")
    body = text[brace:] if brace >= 0 else text
    return "grant-403 %s" % body


def upload_error_line(node):
    """Publisher last_error when it is a log/append failure (upload, not pull)."""
    cloud = cloud_sync(owner_status(node))
    if not isinstance(cloud, dict):
        return None
    err = cloud.get("last_error")
    if not isinstance(err, str) or "/relay/v1/log/append" not in err:
        return None
    text = redact_secrets(err.replace("\n", " ").strip())
    return "upload-error %s" % text if text else None


def chain_roster(roster, database):
    """One line per node: the entries it signed, how many the relay took, and the entries it received."""
    return "\n".join("%s chain %s uploaded=%s %s" % (
        name, authored_entries(node, database), uploaded_count(node, database),
        received_entries(node, database)) for name, node in roster)


def uploaded_count(node, database):
    """uploaded.json holds one number: how many authored entries the relay acknowledged."""
    raw = _read(node, database, "uploaded.json")
    if raw is None:
        return "-"
    text = raw.decode("utf-8", "replace").strip()
    return text if text.isdigit() and len(text) <= 9 else "bad"


def authored_count(node, database):
    """How many entries this node signed, or None when the log is missing or malformed."""
    entries = _entries(node, database, "authored.json")
    return len(entries) if isinstance(entries, list) else None


def authored_entries(node, database):
    """Every entry this node signed, as kind@seq with the role, stream or citation that decides it."""
    return "authored=%s" % _listed(_entries(node, database, "authored.json"), False)


def received_entries(node, database):
    """Every entry this node accepted from another stream, with the first 6 hex of its signer."""
    return "received=%s" % _listed(_entries(node, database, "received.json"), True)


def _read(node, database, name):
    if not isinstance(database, str) or not DATABASE_ID.fullmatch(database):
        return None
    return read_limited(Path(node.home) / "log-sync" / database / name, 512 * 1024)


def _entries(node, database, name):
    raw = _read(node, database, name)
    if raw is None:
        return None
    try:
        entries = decode_json(raw)
    except OperationError:
        return "bad"
    return entries if isinstance(entries, list) else "bad"


def _listed(entries, signer):
    if entries is None:
        return "-"
    if entries == "bad":
        return "bad"
    labels = [_label(entry, signer) for entry in entries if isinstance(entry, dict)]
    if not labels:
        return "0"
    # The newest entries decide a stuck proposal, so a long list keeps its tail.
    shown = labels[-40:]
    skipped = "" if len(labels) <= 40 else "..."
    return "%s:%s%s" % (len(labels), skipped, ",".join(shown))


def _hex(value, width):
    return value[:width] if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) else "?"


def _word(value):
    return value if isinstance(value, str) and re.fullmatch(r"[a-z_]{1,16}", value) else "?"


def _label(entry, signer):
    kind = _word(entry.get("kind"))
    seq = entry.get("writer_seq")
    text = "%s@%s" % (kind, seq if type(seq) is int else "?")
    if signer:
        text = "%s:%s" % (_hex(entry.get("node_key"), 6), text)
    if kind == "grant":
        grantee = entry.get("grantee")
        who = "any" if grantee == "any" else _hex(grantee, 8)
        return "%s:%s:%s" % (text, who, _word(entry.get("role")))
    if kind == "data":
        accepted = "+from" if isinstance(entry.get("incorporated_from"), dict) else ""
        return "%s:%s%s" % (text, _word(entry.get("stream")), accepted)
    if kind == "heads":
        seen = entry.get("seen_heads")
        return "%s:cites%s" % (text, len(seen) if isinstance(seen, list) else "?")
    return text
