"""Relay 403 diagnostics: publisher authored rows and full grant JSON."""

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
    """Local authored grants + outbox beside a relay 403; full grant JSON below."""
    parts = [
        "grant-state pub=%s rdr=%s disk=%s authored=%s orphan=%s" % (
            clip_public(sync_text(publisher, database), 200),
            clip_public(sync_text(reader, database), 200),
            files_text(publisher, database),
            authored_grants(publisher, database),
            orphan_authored(publisher, database),
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


def orphan_authored(node, database):
    """Birth/grant in authored.json but absent from outbox.json (wiped before upload)."""
    authored = _kind_seqs(node, database, "authored.json")
    if authored is None:
        return "-"
    pending = _kind_seqs(node, database, "outbox.json")
    if pending is None:
        pending = set()
    missing = sorted(authored - pending)
    if not missing:
        return "0"
    return ",".join(missing[:12]) + ("" if len(missing) <= 12 else "+%s" % (len(missing) - 12))


def _kind_seqs(node, database, name):
    if not isinstance(database, str) or not DATABASE_ID.fullmatch(database):
        return None
    path = Path(node.home) / "log-sync" / database / name
    raw = read_limited(path, 512 * 1024)
    if raw is None:
        return None if name == "authored.json" else set()
    try:
        entries = decode_json(raw)
    except OperationError:
        return None
    if not isinstance(entries, list):
        return None
    out = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        if kind not in ("birth", "grant"):
            continue
        seq = entry.get("writer_seq")
        out.add("%s@%s" % (kind, seq if type(seq) is int else "?"))
    return out


def authored_grants(node, database):
    """Publisher-authored birth/grant rows on disk (kind/seq/grantee/role)."""
    if not isinstance(database, str) or not DATABASE_ID.fullmatch(database):
        return "authored=-"
    path = Path(node.home) / "log-sync" / database / "authored.json"
    raw = read_limited(path, 512 * 1024)
    if raw is None:
        return "authored=-"
    try:
        entries = decode_json(raw)
    except OperationError:
        return "authored=bad"
    if not isinstance(entries, list):
        return "authored=bad"
    parts = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        if kind not in ("birth", "grant"):
            continue
        seq = entry.get("writer_seq")
        if kind == "birth":
            parts.append("birth@%s" % (seq if type(seq) is int else "?"))
            continue
        grantee = entry.get("grantee")
        role = entry.get("role")
        parts.append("grant@%s:%s:%s" % (
            seq if type(seq) is int else "?",
            grantee if isinstance(grantee, str) else "?",
            role if isinstance(role, str) else "?",
        ))
    if not parts:
        return "authored=0"
    extra = "" if len(parts) <= 12 else "+%s" % (len(parts) - 12)
    return "authored=%s:%s%s" % (len(parts), ",".join(parts[:12]), extra)
