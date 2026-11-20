"""Publish state from a received database, for a people-direct failure.

The row is the one `people db show` returns. That command reads the databases
list and selects this database. `people db list` keeps only a database that
carries a spec, and a whole-database share has none.

Ana's timeline keeps the first pending count and each later change of
outcome or status. The node warns once per such pair, so the line shows one
retry cycle.
"""

import json
import re

from people_app import people


MEMBERS = ("Ana", "Bo", "Cam")
MAX_PUBLISH_EVENTS = 8
_ANA = {"events": [], "signature": None, "saw_pending": False}


def begin_ana_publish():
    """Clear the timeline for the wait after Ana's committed write."""
    _ANA["events"] = []
    _ANA["signature"] = None
    _ANA["saw_pending"] = False


def note_ana_publish(node, database):
    """Record Ana's publish sample. Failures here do not fail the wait."""
    try:
        row = listed_database(node, database)
    except Exception:
        return
    publish = row.get("publish") if isinstance(row, dict) else None
    if not isinstance(publish, dict) or type(publish.get("pending")) is not int:
        return
    outcome, status, at = attempt_parts(publish.get("lastAttempt"))
    signature = (outcome, status)
    if not _ANA["saw_pending"]:
        _ANA["saw_pending"] = True
        _ANA["signature"] = signature
        _ANA["events"].append(("first", publish["pending"], outcome, status, at))
        return
    if signature == _ANA["signature"] or len(_ANA["events"]) >= MAX_PUBLISH_EVENTS:
        return
    _ANA["signature"] = signature
    _ANA["events"].append(("change", publish["pending"], outcome, status, at))


def member_publish(roster, database):
    """One line per Ana, Bo, and Cam, then Ana's timeline when one was recorded."""
    lines = ["%s %s" % (name, member_line(node, database)) for name, node in roster if name in MEMBERS]
    timeline = timeline_text()
    if timeline:
        lines.append(timeline)
    return "\n".join(lines) if lines else "publish=absent"


def member_line(node, database):
    try:
        row = listed_database(node, database)
    except Exception:
        return "publish=unread"
    if not isinstance(row, dict):
        return "publish=absent"
    body = "publish=absent" if "publish" not in row else "publish=%s" % publish_json(row.get("publish"))
    note = access_note(row.get("selfAccess"))
    return "%s %s" % (body, note) if note else body


def listed_database(node, database):
    shown = people(node, "db", "show", database)
    row = shown.get("database") if isinstance(shown, dict) else None
    if isinstance(row, dict) and row.get("id") == database:
        return row
    return None


def publish_json(publish):
    if not isinstance(publish, dict):
        return "null"
    pending = publish.get("pending")
    attempt = publish.get("lastAttempt")
    view = {"pending": pending if type(pending) is int else None, "lastAttempt": None}
    if isinstance(attempt, dict):
        outcome, status, at = attempt_parts(attempt)
        view["lastAttempt"] = {
            "at": None if at == "-" else at,
            "outcome": None if outcome == "-" else outcome,
            "status": status_value(attempt.get("status")),
        }
    return json.dumps(view, separators=(",", ":"), sort_keys=True)


def access_note(access):
    """#1805 selfAccess is `{role}` only."""
    if not isinstance(access, dict) or "role" not in access:
        return ""
    return "role=%s" % mark(access.get("role"))


def timeline_text():
    if not _ANA["events"]:
        return ""
    parts = []
    for kind, pending, outcome, status, at in _ANA["events"]:
        if kind == "first":
            parts.append("first_pending=%s at=%s outcome=%s status=%s" % (pending, at, outcome, status))
        else:
            parts.append("at=%s outcome=%s status=%s pending=%s" % (at, outcome, status, pending))
    return "Ana timeline " + " -> ".join(parts)


def attempt_parts(attempt):
    if not isinstance(attempt, dict):
        return "-", "-", "-"
    outcome = attempt.get("outcome")
    outcome_text = outcome if isinstance(outcome, str) and re.fullmatch(r"[a-z_]{1,24}", outcome) else "-"
    status = status_value(attempt.get("status"))
    return outcome_text, "-" if status is None else str(status), time_mark(attempt.get("at"))


def status_value(value):
    if type(value) is int and 0 <= value <= 999:
        return value
    if isinstance(value, str) and re.fullmatch(r"[1-5][0-9]{2}", value):
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"[a-z_]{1,40}", value):
        return value
    return None


def time_mark(value):
    if isinstance(value, str) and re.fullmatch(r"[0-9T:Z.+-]{1,32}", value):
        return value
    return "-"


def mark(value):
    if value is True:
        return "1"
    if value is False:
        return "0"
    if value is None:
        return "-"
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,24}", value):
        return value
    return "?"
