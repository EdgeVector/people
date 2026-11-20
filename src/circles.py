"""Resolve private circle membership into publisher-signed grant operations.

People computes desired roles from circles. LastDB authors the chain entries.
Chain grants are authoritative; People-local CirclePolicy is intent only.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import re

from lastdb import PeopleError
from sharing import member_row


SCHEMA = "people/CirclePolicy"
FULL = "all"
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
class GrantReview(PeopleError):
    def __init__(self, changes):
        super().__init__("Review grants changed outside People before updating this database.")
        self.changes = changes


class SavedSharingUpdate(PeopleError):
    pass


class Circles:
    def __init__(self, node, people, sharing):
        self.node = node
        self.people = people
        self.sharing = sharing

    def schemas(self, database):
        rows = self.node.databases("schemas", database=database, include_definition_ids=True)
        return {row["name"]: row["definition_id"] for row in rows["schemas"]
                if row.get("definition_id") and row.get("state") == "Available"}

    def load(self, database):
        previous = self.node.get_record(SCHEMA, database)
        saved = json.loads(previous["fields"]["circles"]) if previous else []
        return {"circles": saved, "direct": {}} if isinstance(saved, list) else saved

    def describe(self, database):
        return self.load(database)["circles"]

    def plan(self, database, definitions, circles, restore=(), *, review=None, direct_change=None):
        if self.sharing.pending_edit(database):
            raise SavedSharingUpdate("A sharing request is waiting for confirmation.")
        policy = self.load(database)
        prior_circles = policy["circles"]
        current = self.sharing.configuration(database)
        before = resolve(prior_circles)
        desired = resolve(circles)
        before.update({key: before.get(key, []) for key in policy["direct"]})
        keys = set(desired)
        if self.people.by_keys(sorted(keys)).keys() != keys:
            raise PeopleError("save verified contacts before sharing with them")
        if any(key not in keys for key in restore):
            raise PeopleError("choose circle members to restore")
        members = [row for row in current["members"] if row["nodeKey"] != "any"]
        direct = dict(policy["direct"])
        member_by_key = {row["nodeKey"]: row for row in members}
        if direct_change:
            direct_key = direct_change["nodeKey"]
            if direct_key not in self.people.by_keys([direct_key]):
                raise PeopleError("save this person's verified contact before sharing")
            target = member_by_key.get(direct_key)
            restore = apply_direct_intent(direct, direct_change, target, definitions, restore)
        conflicts = outside_changes(members, before, policy, definitions)
        if conflicts:
            choices = review or {}
            if set(choices) != {row["nodeKey"] for row in conflicts}:
                raise GrantReview(conflicts)
            for row in conflicts:
                key = row["nodeKey"]
                if choices[key] == "keepDirect":
                    direct[key] = scope_for_role(row["current"]["role"])
                elif choices[key] != "usePeople":
                    raise PeopleError("Choose how to resolve each outside grant.")
        for member in members:
            key = member["nodeKey"]
            if key in desired and key not in before and member["state"] == "active":
                direct[key] = scope_for_role(member["role"])
        for key in direct:
            desired.setdefault(key, [])
        desired_roles = desired_member_roles(desired, direct, definitions, restore, members)
        if direct_change and direct_change["operation"] == "revokePerson":
            desired_roles.pop(direct_key, None)
        if len(desired_roles) + sum(1 for row in members if row["state"] == "revoked"
                                    and row["nodeKey"] not in desired_roles) > 98:
            raise PeopleError("This database allows 98 roster entries, including revoked people.")
        ops = grant_ops(members, desired_roles)
        replacement = projected_members(members, desired_roles)
        next_policy = {"circles": circles, "direct": direct,
                       "confirmed": {row["nodeKey"]: grant_value(row)
                                     for row in replacement if row["nodeKey"] in desired_roles}}
        return {"previous": members, "members": replacement, "ops": ops, "policy": next_policy,
                "entryHash": current.get("entryHash"), "writerSeq": current.get("writerSeq")}

    def apply(self, database, plan):
        if plan["ops"]:
            receipt = self.sharing.apply_ops(database, plan["ops"], plan["policy"])
        else:
            store_policy(self.node, database, plan["policy"])
            receipt = {"entryHash": plan.get("entryHash"), "writerSeq": plan.get("writerSeq")}
        return {"entryHash": receipt.get("entryHash"), "writerSeq": receipt.get("writerSeq"),
                "circles": plan["policy"]["circles"]}

    @contextmanager
    def lock(self):
        path = self.node.state_dir() / "people-circle-edit.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise PeopleError("another circle edit is in progress; retry when it finishes") from error
            yield


def store_policy(node, database, circles):
    previous = node.get_record(SCHEMA, database)
    encoded = json.dumps(circles, separators=(",", ":"))
    if not previous or previous["fields"]["circles"] != encoded:
        node.write_record(SCHEMA, database, {"circles": encoded}, previous)


def normalize(requests, definitions):
    if not isinstance(requests, list) or len(requests) > 50:
        raise PeopleError("provide up to 50 circles")
    result = {}
    for circle in requests:
        name, keys, scope = circle["name"], circle["members"], circle["writeSchemas"]
        if not isinstance(name, str) or not (1 <= len(name.strip()) <= 64) or name.strip() in result:
            raise PeopleError("circle names must be unique and at most 64 characters")
        if not isinstance(keys, list) or any(not isinstance(key, str) or not DIGEST.fullmatch(key) for key in keys):
            raise PeopleError("circle members must be verified node keys")
        if scope != FULL:
            if not isinstance(scope, list) or any(not isinstance(item, str) or item not in definitions for item in scope):
                raise PeopleError("select writable schemas installed in this database")
            scope = sorted(set(scope))
        name = name.strip()
        result[name] = {"name": name, "members": sorted(set(keys)), "writeSchemas": scope}
    return list(result.values())


def resolve(circles):
    result = {}
    for circle in circles:
        scope = circle["writeSchemas"]
        for key in circle["members"]:
            previous = result.get(key, [])
            result[key] = FULL if FULL in (scope, previous) else sorted(set(previous) | set(scope))
    return result


def scope_for_role(role):
    """A direct grant remembers read, submit, or write. The setup map decides schemas."""
    if role == "write":
        return "write"
    return "all" if role == "submit" else []


def role_for_scope(scope):
    if scope == "write":
        return "write"
    return "submit" if scope else "read"


def apply_direct_intent(direct, change, target, definitions, restore):
    key, operation = change["nodeKey"], change["operation"]
    if operation == "revokePerson":
        if target is None:
            raise PeopleError("this person has no grant on the database")
        direct.pop(key, None)
        return restore
    scope = change["writeSchemas"]
    if scope == "write":
        stored = "write"
    else:
        scope = normalize([{"name": "Direct grant", "members": [key], "writeSchemas": scope}],
                          definitions)[0]["writeSchemas"]
        stored = scope if scope == FULL else named_scope(scope, [])
    if operation == "restorePerson":
        if target and target["state"] == "revoked":
            restore = [*restore, key]
        elif not target or direct.get(key) != stored:
            raise PeopleError("choose a person whose access is revoked")
    elif target and target["state"] == "revoked":
        raise PeopleError("explicitly restore this person's access")
    direct[key] = stored
    return restore


def grant_value(member):
    return {"state": member["state"], "role": member["role"]}


def outside_changes(members, before, policy, definitions):
    """Chain grants that differ from People's last confirmed intent.

    A grant that exists only in People is ignored here: the chain is authority.
    """
    confirmed = policy.get("confirmed", {})
    changes = []
    for row in members:
        key = row["nodeKey"]
        if key not in before or row["state"] == "revoked":
            continue
        previous = confirmed.get(key)
        if previous is None:
            scope = combine_scope(before[key], policy["direct"].get(key), definitions)
            previous = {"state": "active", "role": role_for_scope(scope)}
        else:
            previous = grant_value(previous)
        current = grant_value(row)
        if previous != current:
            changes.append({"nodeKey": key, "confirmed": previous, "current": current})
    return changes


def desired_member_roles(desired, direct, definitions, restore, members):
    """Map each person People intends to share with to a chain role."""
    roles = {}
    revoked = {row["nodeKey"] for row in members if row["state"] == "revoked"}
    for key, scope in desired.items():
        if key in revoked and key not in restore:
            continue
        roles[key] = role_from_scopes(scope, direct.get(key), definitions)
    return roles


def role_from_scopes(circle, direct, definitions):
    if direct == "write":
        return "write"
    return role_for_scope(combine_scope(circle, direct, definitions))


def grant_ops(members, desired_roles):
    current = {row["nodeKey"]: row for row in members}
    ops = []
    for key, role in sorted(desired_roles.items()):
        previous = current.get(key)
        if previous and previous["state"] == "active" and previous["role"] == role:
            continue
        ops.append({"op": "grant", "nodeKey": key, "role": role})
    for key, row in sorted(current.items()):
        if key in desired_roles or row["state"] == "revoked":
            continue
        ops.append({"op": "revoke", "nodeKey": key})
    return ops


def projected_members(members, desired_roles):
    rows = {row["nodeKey"]: member_row(row["nodeKey"], row["role"] if row["state"] != "revoked" else "revoked")
            for row in members}
    for key, role in desired_roles.items():
        rows[key] = member_row(key, role)
    for key, row in list(rows.items()):
        if key not in desired_roles and row["state"] == "active":
            rows[key] = member_row(key, "revoked")
    return [rows[key] for key in sorted(rows)]


def combine_scope(circle, direct, definitions):
    if FULL in (circle, direct):
        return FULL
    if isinstance(direct, dict):
        return named_scope(circle, direct["names"])
    if direct:
        # A grant made outside People can list definition IDs. Keep it as written.
        if not circle:
            return direct
        registered = {definition: name for name, definition in definitions.items()}
        if any(registered.get(item) not in circle for item in direct):
            raise PeopleError("review the direct grant before combining it with a circle")
    return named_scope(circle, [])


def named_scope(circle, direct):
    names = sorted(set(circle) | set(direct))
    return {"names": names} if names else []
