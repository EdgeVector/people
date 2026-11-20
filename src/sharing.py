"""Share, Deliver and Org through publisher-signed chain grants.

People asks LastDB to author grant and revoke entries. The signed chain is
the only authority. People may carry signed heads for comparison and
bootstrap the first relay URL from a publisher-signed relays entry. A grant
that exists only in People is never treated as access.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import uuid

from lastdb import LastDBError, PeopleError, no_cloud_account, raise_chain_refusal


TRANSPORT_ERRORS = (OSError, http.client.HTTPException)
PENDING_SCHEMA = "people/SharingUpdate"
ROLES = frozenset({"read", "submit", "write", "revoked"})
UNREAD = object()


def can_manage_sharing(database):
    return database.get("kind") == "shareable" and database.get("sharingMode") == "whole_database"


def member_row(node_key, role):
    """Display shape derived from a folded chain role. Not a side-channel grant."""
    if role == "revoked":
        return {"nodeKey": node_key, "role": "revoked", "state": "revoked"}
    return {"nodeKey": node_key, "role": role, "state": "active"}


def members_from_grants(reply):
    members = []
    for row in reply.get("members") or []:
        key, role = row.get("nodeKey"), row.get("role")
        if not isinstance(key, str) or role not in ROLES:
            raise PeopleError("LastDB returned an invalid grant member")
        members.append(member_row(key, role))
    if reply.get("any") == "read":
        members.append(member_row("any", "read"))
    return members


class Sharing:
    def __init__(self, node, people):
        self.node = node
        self.people = people

    def create(self, name, *, request_id=None):
        return self.node.databases(
            "create", name=name, kind="shareable", sharingMode="whole_database",
            request_id=request_id)["database"]

    def databases(self):
        return self.node.databases("list")["databases"]

    def owned(self, selector):
        matches = [row for row in self.databases()
                   if selector in (row.get("id"), row.get("name"))]
        if len(matches) != 1 or not can_manage_sharing(matches[0]):
            raise PeopleError(f"{selector} needs an owned database with a supported sharing format")
        return matches[0]

    def grants(self, database_id):
        """Folded GrantState from the signed chain. People does not invent this."""
        reply = self.chain_call(lambda: self.node.databases("grants", database=database_id))
        if reply.get("databaseId") != database_id:
            raise PeopleError("LastDB returned grants for a different database")
        return reply

    def heads(self, database_id):
        """Publisher-signed heads for comparison only. Never grant authority."""
        reply = self.chain_call(lambda: self.node.databases("heads", database=database_id))
        if reply.get("databaseId") != database_id:
            raise PeopleError("LastDB returned heads for a different database")
        return reply

    def relays(self, database_id):
        reply = self.chain_call(lambda: self.node.databases("relays", database=database_id))
        if reply.get("databaseId") != database_id:
            raise PeopleError("LastDB returned relays for a different database")
        return reply

    def bootstrap_relay(self, database_id):
        """First relay URL from the publisher-signed relays entry."""
        reply = self.chain_call(lambda: self.node.databases("bootstrap_relay", database=database_id))
        if reply.get("databaseId") != database_id or not isinstance(reply.get("url"), str) or not reply["url"]:
            raise PeopleError("LastDB did not bootstrap a relay URL from the signed relays entry")
        return reply

    def configuration(self, database_id):
        """Owner view: chain members, heads for comparison, and relay list."""
        grants = self.grants(database_id)
        kept = {
            "databaseId": database_id,
            "members": members_from_grants(grants),
            "heads": self.heads(database_id).get("heads") or [],
            "relays": self.relays(database_id).get("urls") or [],
        }
        if "any" in grants:
            kept["any"] = grants["any"]
        tip = grants.get("writerSeq")
        if tip is not None:
            kept["writerSeq"] = tip
        entry = grants.get("entryHash")
        if entry is not None:
            kept["entryHash"] = entry
        return kept

    def all_configurations(self, selector):
        databases = [self.owned(selector)] if selector else [
            row for row in self.databases() if can_manage_sharing(row)
        ]
        with ThreadPoolExecutor(max_workers=4) as pool:
            return list(pool.map(self.configuration_row, databases))

    def configuration_row(self, database):
        database_id = database["id"]
        row = {"databaseId": database_id, "name": database["name"]}
        try:
            return {**row, **self.configuration(database_id)}
        except LastDBError as error:
            return {**row, "status": "unavailable", "httpStatus": error.status}
        except TRANSPORT_ERRORS:
            return {**row, "status": "unavailable"}

    def grant(self, database_id, grantee, role):
        """Append a publisher-signed grant through LastDB. First grant has no cutoffs."""
        if role not in ("read", "submit", "write"):
            raise PeopleError("choose read, submit, or write")
        ack = self.chain_call(lambda: self.node.databases(
            "grant", database=database_id, grantee=grantee, role=role))
        return checked_ack(ack, database_id, role)

    def revoke(self, database_id, grantee):
        """Append a publisher-signed revoke through LastDB. Cutoffs are node's job."""
        ack = self.chain_call(lambda: self.node.databases(
            "revoke", database=database_id, grantee=grantee))
        return checked_ack(ack, database_id, "revoked")

    def update_member(self, database_id, node_key, active, *, role="read"):
        starting = UNREAD
        for _ in range(3):
            previous_request = self.node.get_record(PENDING_SCHEMA, database_id)
            if previous_request and previous_request["fields"]["request"]:
                self.finish_update(database_id, previous_request)
                continue
            current = self.configuration(database_id)
            members = {row["nodeKey"]: row for row in current["members"] if row["nodeKey"] != "any"}
            previous = members.get(node_key)
            if active and previous and previous["state"] == "active" and previous["role"] != role:
                if previous["role"] in ("submit", "write") and role == "read":
                    raise PeopleError(
                        "this person already has contributor access; revoke it before granting reader access")
            desired = "active" if active else "revoked"
            if previous and previous["state"] == desired and (not active or previous["role"] == role):
                return tip_receipt(current, node_key, previous["role"] if previous else role)
            if starting is not UNREAD and previous != starting:
                raise PeopleError("this person's access changed concurrently; review the current grant")
            starting = previous
            if previous is None and not active:
                raise PeopleError("the recipient has never been granted access to this database")
            op = {"op": "grant", "nodeKey": node_key, "role": role} if active else {
                "op": "revoke", "nodeKey": node_key}
            update = {"requestId": str(uuid.uuid4()), "ops": [op],
                      "expectedEntryHash": current.get("entryHash")}
            try:
                receipt = self.save_update(database_id, previous_request, update)
            except LastDBError as error:
                if error.status != 409:
                    raise
                continue
            if receipt is not None:
                return receipt
        raise PeopleError("the grant chain changed concurrently; retry the operation")

    def apply_ops(self, database_id, ops, policy):
        previous = self.node.get_record(PENDING_SCHEMA, database_id)
        if previous and previous["fields"]["request"]:
            raise PeopleError("finish the pending sharing change first")
        current = self.configuration(database_id)
        request = {"requestId": str(uuid.uuid4()), "ops": ops,
                   "expectedEntryHash": current.get("entryHash"), "circles": policy}
        receipt = self.save_update(database_id, previous, request)
        if receipt is None:
            raise PeopleError("sharing changed while you edited it; review the current grants")
        return receipt

    def save_update(self, database_id, previous, request):
        encoded = json.dumps(request, separators=(",", ":"))
        written = self.node.write_record(PENDING_SCHEMA, database_id, {"request": encoded}, previous)
        saved = {"revision": written["revision"], "fields": {"request": encoded}}
        return self.finish_update(database_id, saved)

    def resume_pending(self, database_id):
        saved = self.node.get_record(PENDING_SCHEMA, database_id)
        if saved and saved["fields"]["request"]:
            self.finish_update(database_id, saved)

    def pending_edit(self, database_id):
        saved = self.node.get_record(PENDING_SCHEMA, database_id)
        if not saved or not saved["fields"]["request"]:
            return None
        request = json.loads(saved["fields"]["request"])
        return {"ops": request["ops"], "expectedEntryHash": request.get("expectedEntryHash")}

    def finish_update(self, database_id, saved):
        request = json.loads(saved["fields"]["request"])
        try:
            receipt = self.apply(database_id, request)
        except LastDBError as error:
            if error.status == 409 and not no_cloud_account(error):
                self.clear_update(database_id, saved)
                return None
            if error.status not in (400, 403, 404):
                raise
            self.clear_update(database_id, saved)
            raise
        if "circles" in request:
            from circles import store_policy
            store_policy(self.node, database_id, request["circles"])
        self.clear_update(database_id, saved)
        return receipt

    def clear_update(self, database_id, saved):
        try:
            self.node.write_record(PENDING_SCHEMA, database_id, {"request": ""}, saved)
        except LastDBError as error:
            if error.status != 409:
                raise

    def apply(self, database_id, update):
        receipt = None
        for op in update["ops"]:
            if op["op"] == "grant":
                receipt = self.grant(database_id, op["nodeKey"], op["role"])
            elif op["op"] == "revoke":
                receipt = self.revoke(database_id, op["nodeKey"])
            else:
                raise PeopleError("unknown sharing operation")
        return receipt

    def chain_call(self, operation):
        """One signed-history call. The node waits for enrollment. People does not."""
        try:
            return operation()
        except LastDBError as error:
            raise_chain_refusal(error)
            raise

    def seal(self, database_id, node_key, contact_code, tip, publisher):
        response = self.chain_call(lambda: self.node.databases(
            "invite", database=database_id, contactCode=contact_code, expectedNodeKey=node_key))
        if response["databaseId"] != database_id or not response.get("invitation"):
            raise PeopleError("LastDB returned a mismatched invitation")
        return {"databaseId": database_id, "entryHash": tip.get("entryHash"),
                "writerSeq": tip.get("writerSeq"), "publisher": publisher,
                "invitation": response["invitation"]}

    def accept(self, receipt, publisher, name):
        if receipt.get("publisher") != publisher:
            raise PeopleError("invitation publisher differs from the trusted publisher")
        database = self.chain_call(lambda: self.node.databases(
            "accept", invitation=receipt["invitation"], publisher=publisher,
            database_id=receipt["databaseId"], name=name))["database"]
        # Member discovers the first relay from the publisher-signed relays entry.
        try:
            self.bootstrap_relay(receipt["databaseId"])
        except LastDBError as error:
            if not no_cloud_account(error):
                raise
        return database


def checked_ack(ack, database_id, role):
    if (ack.get("databaseId") != database_id or ack.get("role") != role
            or not isinstance(ack.get("entryHash"), str) or not ack["entryHash"]
            or type(ack.get("writerSeq")) is not int):
        raise PeopleError("LastDB returned a mismatched grant acknowledgement")
    return {"databaseId": database_id, "entryHash": ack["entryHash"],
            "writerSeq": ack["writerSeq"], "role": role}


def tip_receipt(current, node_key, role):
    return {"databaseId": current["databaseId"], "entryHash": current.get("entryHash"),
            "writerSeq": current.get("writerSeq"), "nodeKey": node_key, "role": role}


def read_invitation(stream):
    raw = stream.read(8193)
    if len(raw) > 8192:
        raise PeopleError("invitation receipt exceeds its size limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise PeopleError("expected a People invitation receipt")
    return value
