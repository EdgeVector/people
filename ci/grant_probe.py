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
        "grant-state pub=%s rdr=%s disk=%s authored=%s" % (
            clip_public(sync_text(publisher, database), 200),
            clip_public(sync_text(reader, database), 200),
            files_text(publisher, database),
            authored_grants(publisher, database),
        )
    ]
    for label, node in (("publisher", publisher), ("Ana", reader)):
        line = grant_403_for_node(node)
        if line:
            parts.append("%s %s" % (label, line))
    return "\n".join(parts)


def grant_403_roster(roster):
    """One full grant-403 line per node that has a relay grant denial."""
    lines = []
    for name, node in roster:
        line = grant_403_for_node(node)
        if line:
            lines.append("%s %s" % (name, line))
    return "\n".join(lines)


def grant_403_for_node(node):
    cloud = cloud_sync(owner_status(node))
    if not isinstance(cloud, dict):
        return None
    return grant_403_line(cloud.get("last_error"))


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
