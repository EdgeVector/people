"""Compact sync, ops, and log-sync state for one people-direct node.

Secrets are removed with redact_secrets before any clip. A down node still
contributes the log-sync files on disk. failure_evidence adds the daemon
log lines and the small log-sync files. The compact node line is unchanged.
"""

import http.client
import json
import os
from pathlib import Path
import re
import stat
from contextlib import closing

from node import Connection
from support import OperationError, decode_json, redact_secrets


# Signed-history database ids are Hex32 (64 lowercase hex), not UUIDs.
DATABASE_ID = re.compile(r"[0-9a-f]{64}")
LOG_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
# PrivateFormat strips messages; match severity= so WARN turns still count.
# HTTP statuses are bounded so a timestamp does not match 4xx or 5xx.
LOG_HINT = re.compile(
    r"severity=|log-sync|append|received|retry|unauthorized|error|(?<![0-9])[45][0-9]{2}(?![0-9])",
    re.IGNORECASE,
)
FILE_NAME = re.compile(r"[A-Za-z0-9._-]{1,40}")


def node_dump(roster, database):
    parts = []
    for name, node in roster:
        try:
            parts.append(compact_node(name, node, database))
        except Exception:
            parts.append("%s dump=failed" % name)
    return " ".join(parts)


def compact_node(name, node, database):
    # Keep publisher last_error and Hex32 disk bits; attach() still clips the dump.
    return ("%s %s" % (name, node_entry(node, database)))[:120]


def node_entry(node, database):
    if not live(node):
        return "%s sync=down ops=down" % files_text(node, database)
    return "%s %s %s" % (files_text(node, database), ops_text(node), sync_text(node, database))


def live(node):
    process = getattr(node, "process", None)
    return process is not None and process.poll() is None


def sync_text(node, database):
    cloud = cloud_sync(owner_status(node))
    if cloud is None:
        return "sync=unread"
    err = clip_public(cloud.get("last_error"), 120) if isinstance(cloud.get("last_error"), str) else "-"
    text = "sync=%s/%s/%s err=%s n=%s" % (
        bit(cloud.get("connected")), bit(cloud.get("captures_accepted")),
        bit(cloud.get("payment_hold")), err, count_of(cloud.get("databases")))
    progress = database_progress(cloud.get("databases"), database)
    return text if progress is None else "%s %s" % (text, progress)


def cloud_sync(body):
    if not isinstance(body, dict):
        return None
    status = body.get("status")
    cloud = status.get("cloud_sync") if isinstance(status, dict) else None
    return cloud if isinstance(cloud, dict) else None


def owner_status(node):
    try:
        with closing(Connection(node.socket, timeout=5)) as connection:
            connection.request("GET", "/api/status", headers={"X-LastDB-Client": "cloud-backup-ci"})
            reply = connection.getresponse()
            raw = reply.read(65536)
            if reply.status != 200:
                return None
            return decode_json(raw)
    except (OSError, http.client.HTTPException, OperationError):
        return None


def database_progress(databases, database):
    if not isinstance(database, str) or not isinstance(databases, dict) or database not in databases:
        return None
    item = databases[database]
    if not isinstance(item, dict):
        return "db=?"
    upload = item.get("upload") if isinstance(item.get("upload"), dict) else {}
    download = item.get("download") if isinstance(item.get("download"), dict) else {}
    return "db=%s/%s/%s" % (
        num(upload.get("spool_head")), num(upload.get("spool_confirmed_through")),
        num(download.get("applied_through")))


def ops_text(node):
    try:
        body = node.programs.command(node.home, ["ops", "--json"], timeout=10)
    except (OperationError, OSError):
        return "ops=unread"
    calls = body.get("calls") if isinstance(body, dict) else None
    if not isinstance(calls, list):
        return "ops=absent"
    kept = [one_call(row) for row in calls if wanted(row)]
    return "ops=" + (",".join(item for item in kept[:3] if item) or "-")


def wanted(row):
    if not isinstance(row, dict) or not isinstance(row.get("call"), str):
        return False
    call = row["call"]
    refused = row.get("refused") if isinstance(row.get("refused"), dict) else {}
    if "x/databases" in call or "/v1/write" in call:
        return True
    if not refused:
        return False
    return any(word in call for word in ("sync", "backup", "mutation", "share"))


def one_call(row):
    # Keep a short path. The failure redaction would otherwise swallow a long route.
    call = row["call"][:28] if re.fullmatch(r"[A-Za-z0-9_./:-]{1,160}", row["call"]) else "?"
    count = row.get("count")
    text = "%s:%s" % (call, count if type(count) is int else "?")
    refused = row.get("refused") if isinstance(row.get("refused"), dict) else {}
    parts = []
    for code, item in refused.items():
        if isinstance(code, str) and code.isdigit() and type(item) is int and item:
            parts.append("!%s:%s" % (code, item))
        if len(parts) == 2:
            break
    return text + "".join(parts)


def files_text(node, database):
    if not isinstance(database, str) or not DATABASE_ID.fullmatch(database):
        return "files=-"
    root = Path(node.home) / "log-sync" / database
    birth = "birth" if (root / "birth.json").is_file() else "-"
    return "files=%s/%s/%s" % (birth, outbox_text(root / "outbox.json"), error_bits(root))


def outbox_text(path):
    raw = read_limited(path, 256 * 1024)
    if raw is None:
        return "outbox=-"
    try:
        entries = decode_json(raw)
    except OperationError:
        return "outbox=bad"
    if not isinstance(entries, list):
        return "outbox=bad"
    if not entries:
        return "outbox=0"
    kinds = []
    for entry in entries[:8]:
        kind = entry.get("kind") if isinstance(entry, dict) else None
        kinds.append(kind if isinstance(kind, str) and kind else "?")
    extra = "" if len(entries) <= 8 else "+%s" % (len(entries) - 8)
    return "outbox=%s:%s%s" % (len(entries), ",".join(kinds), extra)


def error_bits(directory):
    if directory.is_symlink() or not directory.is_dir():
        return "-"
    try:
        entries = sorted(directory.iterdir(), key=lambda item: item.name)
    except OSError:
        return "-"
    for path in entries:
        if "error" not in path.name or path.is_symlink() or not path.is_file():
            continue
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", path.name):
            continue
        raw = read_limited(path, 256)
        if raw is None:
            continue
        text = raw.decode("utf-8", "replace").replace("\n", " ").strip()
        return clip_public(text, 12)
    return "-"


def read_limited(path, limit):
    try:
        info = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        return None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        return os.read(descriptor, limit)
    finally:
        os.close(descriptor)


def clip_public(value, limit):
    """Redact first, then keep the front. A clip must not leave a token prefix."""
    return clip_end(value, limit, tail=False)


def clip_tail(value, limit):
    """Redact first, then keep the end of one log line."""
    return clip_end(value, limit, tail=True)


def clip_end(value, limit, tail):
    if not isinstance(value, str) or not value:
        return "-"
    redacted = redact_secrets(value.replace("\n", " ").strip())
    if not redacted:
        return "-"
    return redacted[-limit:] if tail else redacted[:limit]


def bit(value):
    if value is True:
        return "1"
    if value is False:
        return "0"
    return "-"


def count_of(value):
    return str(len(value)) if isinstance(value, dict) else "-"


def num(value):
    return str(value) if type(value) is int else "-"


def failure_evidence(roster, database):
    """Per node: matching daemon lines, then session, prefix, and error files.

    The daemon lines stay when the log-sync file read fails.
    """
    parts = [node_evidence(name, node, database) for name, node in roster]
    try:
        cloud = ana_cloud(roster)
    except Exception:
        cloud = "Ana cloud=unread"
    if cloud:
        parts.append(cloud)
    try:
        from grant_probe import grant_state
        by_name = {name: node for name, node in roster}
        if "publisher" in by_name and "Ana" in by_name:
            parts.append(grant_state(by_name["publisher"], by_name["Ana"], database))
    except Exception:
        parts.append("grant-state=failed")
    return "\n".join(parts)[:48000]


def node_evidence(name, node, database):
    try:
        log = log_text(node)
    except Exception:
        log = "log=failed"
    try:
        files = sync_files(node, database)
    except Exception:
        files = "files=failed"
    return "%s %s %s" % (name, log, files)


def log_text(node):
    name = os.environ.get("PEOPLE_DIRECT_DAEMON_LOG", "")
    if not LOG_NAME.fullmatch(name):
        return "log=off"
    raw, truncated = tail_bytes(Path(node.home) / name, 262144)
    if raw is None:
        return "log=-"
    text = raw.decode("utf-8", "replace")
    if truncated:
        cut = text.find("\n")
        text = text[cut + 1:] if cut >= 0 else ""
    lines = text.splitlines()
    matched = [clip_tail(line, 300) for line in lines if LOG_HINT.search(line)]
    matched = [line for line in matched if line != "-"]
    body = " || ".join(matched[-15:]) if matched else "-"
    return "log=%s/%s %s %s" % (len(matched), len(lines), "tail" if truncated else "all", body)


def tail_bytes(path, limit):
    try:
        info = path.lstat()
    except OSError:
        return None, False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None, False
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None, False
    try:
        size = os.fstat(descriptor).st_size
        truncated = size > limit
        if truncated:
            os.lseek(descriptor, size - limit, os.SEEK_SET)
        return os.read(descriptor, limit), truncated
    except OSError:
        return None, False
    finally:
        os.close(descriptor)


def sync_files(node, database):
    if not isinstance(database, str) or not DATABASE_ID.fullmatch(database):
        return "birth=- outbox=- error=-"
    root = Path(node.home) / "log-sync" / database
    birth = "birth=yes" if (root / "birth.json").is_file() else "birth=-"
    parts = [birth, outbox_text(root / "outbox.json")]
    parts.extend(error_files(root))
    return " ".join(parts)


def small_text(path, limit):
    state = file_state(path, limit)
    if state != "ok":
        return state
    raw = read_limited(path, limit)
    if raw is None:
        return "-"
    return clip_public(raw.decode("utf-8", "replace"), 1200)


def file_state(path, limit):
    try:
        info = path.lstat()
    except OSError:
        return "-"
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return "-"
    if info.st_size > limit:
        return "too-large"
    return "ok"


def error_files(directory):
    if directory.is_symlink() or not directory.is_dir():
        return ["error=-"]
    try:
        entries = sorted(directory.iterdir(), key=lambda item: item.name)
    except OSError:
        return ["error=-"]
    found = []
    for path in entries:
        if "error" not in path.name or not FILE_NAME.fullmatch(path.name):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        found.append("%s=%s" % (path.name, small_text(path, 1024)))
        if len(found) == 4:
            break
    return found or ["error=-"]


def ana_cloud(roster):
    """Ana's owner-socket cloud sync section. Other nodes keep the compact sync line."""
    node = None
    for name, item in roster:
        if name == "Ana":
            node = item
    if node is None:
        return ""
    cloud = cloud_sync(owner_status(node))
    if cloud is None:
        return "Ana cloud=unread"
    try:
        text = json.dumps(cloud, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError):
        return "Ana cloud=unread"
    return "Ana cloud=%s" % clip_public(text, 1500)
