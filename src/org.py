"""Org: the `write` role plus a lease length, and the plain text that says what a member can do."""

from datetime import datetime, timezone


# Org: a database with a `write` member holds write leases. People gives the
# database this lease length when its first writer arrives and the publisher set
# none. Three days is the low end of the design's "publisher is on most days" row.
# A lease is renewed at half its length, so the publisher can be away for a day and
# a half before members fall back to proposals. Removing a writer takes up to one
# length. The publisher can set another length with `lastdb share lease`.
ORG_LEASE_MS = 3 * 24 * 60 * 60 * 1000


def utc(milliseconds):
    return datetime.fromtimestamp(milliseconds / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def duration(milliseconds):
    for unit, size in (("day", 86_400_000), ("hour", 3_600_000), ("minute", 60_000)):
        if milliseconds % size == 0:
            count = milliseconds // size
            return f"{count} {unit}{'' if count == 1 else 's'}"
    return f"{milliseconds / 1000:g} seconds"


def member_write_access(row):
    """What a member of a received database can do now, in one plain sentence."""
    access = row.get("selfAccess") or {}
    if access.get("role") not in ("write", "submit"):
        return None
    lease = access.get("lease") or {}
    if access.get("role") == "write" and lease.get("directWrites") and lease.get("directWritesUntilMs"):
        return f"You can write directly until {utc(lease['directWritesUntilMs'])}."
    return "Your changes are proposals. The publisher decides each one."


def publisher_write_access(reply):
    """The lease length of an owned database and the end of its current lease."""
    length = reply.get("leaseLengthMs")
    if length is None:
        return None
    text = f"Members with write access change records directly. Each lease lasts {duration(length)}."
    if isinstance(reply.get("lease"), dict):
        text += f" The current lease ends {utc(reply['lease']['expiresAtMs'])}."
    return text


def writers_after(members, ops):
    """Whether any member holds `write` once these grant and revoke operations are applied."""
    roles = {row["nodeKey"]: row["role"] for row in members if row["state"] == "active" and row["nodeKey"] != "any"}
    for op in ops:
        if op["op"] == "grant":
            roles[op["nodeKey"]] = op["role"]
        else:
            roles.pop(op["nodeKey"], None)
    return "write" in roles.values()
