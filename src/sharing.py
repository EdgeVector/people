"""Share, Deliver and Org through publisher-signed chain grants.

People asks LastDB to author grant and revoke entries. The signed chain is
the only authority. People may carry signed heads for comparison and
bootstrap the first relay URL from a publisher-signed relays entry. A grant
that exists only in People is never treated as access.

Invites are People consent codes: a hand-off string the publisher mints after
a chain grant, and the recipient pastes into their People app. ExeMem and the
relay never see an invite; they only see signed grant entries on the chain.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import base64
import http.client
import json
import uuid

from lastdb import LastDBError, PeopleError, no_cloud_account, raise_chain_refusal


TRANSPORT_ERRORS = (OSError, http.client.HTTPException)
PENDING_SCHEMA = "people/SharingUpdate"
ROLES = frozenset({"read", "submit", "write", "revoked"})
UNREAD = object()
CONSENT_PREFIX = "people-consent-v1."
OPEN_PREFIX = "lastdb-read-v1."
MAX_CONSENT = 8192


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
        """Append a publisher-signed revoke through LastDB. Cutoffs are node's job.

        LastDB refuses `grantee=any` (`public read cannot be revoked`).
        """
        if grantee == "any":
            raise PeopleError("public read cannot be revoked")
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
        expected = update.get("expectedEntryHash")
        tip = self.configuration(database_id).get("entryHash")
        if expected != tip:
            raise LastDBError(
                "POST", "/v1/x/databases", 409,
                {"error": {"code": "grant_chain_moved",
                           "message": "grant chain moved since this update was planned"}})
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

    def mint_consent(self, database_id, tip, publisher):
        """Mint a People consent code. LastDB only supplies the open-link material."""
        response = self.chain_call(lambda: self.node.databases("share_link", database=database_id))
        link = response.get("link")
        if (response.get("databaseId") != database_id or not isinstance(link, str)
                or not link.startswith(OPEN_PREFIX)):
            raise PeopleError("LastDB returned a mismatched open link")
        payload = {
            "v": 1,
            "databaseId": database_id,
            "publisher": publisher,
            "entryHash": tip["entryHash"],
            "open": link,
        }
        if "writerSeq" in tip:
            payload["writerSeq"] = tip["writerSeq"]
        return {
            "databaseId": database_id,
            "entryHash": tip["entryHash"],
            "writerSeq": tip.get("writerSeq"),
            "publisher": publisher,
            "consent": encode_consent(payload),
        }

    def accept(self, consent, publisher, name):
        payload = decode_consent(consent)
        if payload["publisher"] != publisher:
            raise PeopleError("consent publisher differs from the trusted publisher")
        database = self.chain_call(lambda: self.node.databases(
            "accept", link=payload["open"], name=name))["database"]
        if database.get("id") != payload["databaseId"]:
            raise PeopleError("LastDB accepted a different database")
        # Member discovers the first relay from the publisher-signed relays entry.
        try:
            self.bootstrap_relay(payload["databaseId"])
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


def encode_consent(payload):
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    code = f"{CONSENT_PREFIX}{body}"
    if len(code) > MAX_CONSENT:
        raise PeopleError("consent code exceeds its size limit")
    return code


def decode_consent(code):
    if not isinstance(code, str) or not code.startswith(CONSENT_PREFIX):
        raise PeopleError("expected a People consent code")
    body = code[len(CONSENT_PREFIX):]
    if not body or len(code) > MAX_CONSENT:
        raise PeopleError("expected a People consent code")
    try:
        padded = body + "=" * (-len(body) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as error:
        raise PeopleError("expected a People consent code") from error
    if (not isinstance(payload, dict) or payload.get("v") != 1
            or not isinstance(payload.get("databaseId"), str)
            or not isinstance(payload.get("publisher"), str)
            or not isinstance(payload.get("entryHash"), str) or not payload["entryHash"]
            or not isinstance(payload.get("open"), str)
            or not payload["open"].startswith(OPEN_PREFIX)):
        raise PeopleError("expected a People consent code")
    if "writerSeq" in payload and type(payload["writerSeq"]) is not int:
        raise PeopleError("expected a People consent code")
    return payload


def read_consent(stream):
    """Read a consent code: raw string, JSON string, or JSON object with `consent`."""
    raw = stream.read(MAX_CONSENT + 1)
    if len(raw) > MAX_CONSENT:
        raise PeopleError("consent code exceeds its size limit")
    text = raw.decode("utf-8").strip()
    if text.startswith(CONSENT_PREFIX):
        return text
    try:
        value = json.loads(text)
    except (ValueError, UnicodeError) as error:
        raise PeopleError("expected a People consent code") from error
    if isinstance(value, str) and value.startswith(CONSENT_PREFIX):
        return value
    if isinstance(value, dict) and isinstance(value.get("consent"), str):
        return value["consent"]
    raise PeopleError("expected a People consent code")
