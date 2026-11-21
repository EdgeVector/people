"""Relay 403 diagnostics: publisher authored birth/grant rows on disk."""

from pathlib import Path

from direct_nodes import (
    DATABASE_ID,
    clip_public,
    files_text,
    read_limited,
    sync_text,
)
from support import OperationError, decode_json


def grant_state(publisher, reader, database):
    """Local authored grants + outbox + cloud last_error beside a relay 403."""
    return "grant-state pub=%s rdr=%s disk=%s authored=%s" % (
        clip_public(sync_text(publisher, database), 200),
        clip_public(sync_text(reader, database), 200),
        files_text(publisher, database),
        authored_grants(publisher, database),
    )


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
