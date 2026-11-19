"""Change one member while preserving the rest of the applied configuration."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import uuid

from lastdb import LastDBError, PeopleError, no_cloud_account, raise_share_refusal


TRANSPORT_ERRORS = (OSError, http.client.HTTPException)
PENDING_SCHEMA = "people/SharingUpdate"
UNREAD = object()


def can_manage_sharing(database):
    return database.get("kind") == "shareable" and database.get("sharingMode") == "whole_database"


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

    def configuration(self, database_id):
        reply = self.node.databases("share_config", database=database_id)
        if reply["databaseId"] != database_id:
            raise PeopleError("LastDB returned the configuration of a different database")
        kept = {key: reply[key] for key in ("databaseId", "revision", "members")}
        kept.update({key: reply[key] for key in ("read", "write", "memberWrites") if key in reply})
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
            config = self.configuration(database_id)
        except LastDBError as error:
            return {**row, "status": "unavailable", "httpStatus": error.status}
        except TRANSPORT_ERRORS:
            return {**row, "status": "unavailable"}
        return {**row, **config}

    def update_member(self, database_id, node_key, active):
        starting_member = UNREAD
        for _ in range(3):
            previous_request = self.node.get_record(PENDING_SCHEMA, database_id)
            if previous_request and previous_request["fields"]["request"]:
                self.finish_update(database_id, previous_request)
                continue
            current = self.configuration(database_id)
            members = {row["nodeKey"]: row for row in current["members"]}
            previous = members.get(node_key)
            desired = "active" if active else "revoked"
            if active and previous and previous["state"] == "active" and previous["role"] != "read":
                raise PeopleError("this person already has contributor access; revoke it before granting reader access")
            if previous and previous["state"] == desired:
                return current["revision"]
            if starting_member is not UNREAD and previous != starting_member:
                raise PeopleError("this person's access changed concurrently; review the current grant")
            starting_member = previous
            if previous is None and not active:
                raise PeopleError("the recipient has never been granted access to this database")
            scoped = previous is not None and "writeSchemas" in previous
            member = {"nodeKey": node_key, "role": "read" if active else previous["role"],
                      "state": desired}
            if scoped:
                member["writeSchemas"] = []
            if previous and active:
                member["regrant"] = True
            members[node_key] = member
            update = {"expectedRevision": current["revision"], "requestId": str(uuid.uuid4()),
                      "members": list(members.values())}
            try:
                revision = self.save_update(database_id, previous_request, update)
            except LastDBError as error:
                if error.status != 409:
                    raise
                continue
            if revision is not None:
                return revision
        raise PeopleError("the sharing configuration changed concurrently; retry the operation")

    def apply_members(self, database_id, expected_revision, members, circles):
        previous = self.node.get_record(PENDING_SCHEMA, database_id)
        if previous and previous["fields"]["request"]:
            raise PeopleError("finish the pending sharing change first")
        request = {"expectedRevision": expected_revision, "requestId": str(uuid.uuid4()),
                   "members": members, "circles": circles}
        revision = self.save_update(database_id, previous, request)
        if revision is None:
            raise PeopleError("sharing changed while you edited it; review the current roster")
        return revision

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
        return {"expectedRevision": request["expectedRevision"], "members": request["members"]}

    def finish_update(self, database_id, saved):
        request = json.loads(saved["fields"]["request"])
        try:
            revision = self.apply(database_id, request)
        except LastDBError as error:
            # 409 is the share revision conflict, except no cloud account.
            # The extension reports the conflict as `conflict`, with the
            # message that the configuration is at another revision. No
            # cloud account is also HTTP 409 and is final. 400, 403, and
            # 404 are final refusals. A retryable 503 leaves this handler
            # as a PeopleError, so the saved request stays.
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
        return revision

    def clear_update(self, database_id, saved):
        try:
            self.node.write_record(PENDING_SCHEMA, database_id, {"request": ""}, saved)
        except LastDBError as error:
            if error.status != 409:
                raise

    def apply(self, database_id, update):
        wire = {key: update[key] for key in ("expectedRevision", "requestId", "members")}
        ack = self.share_call(lambda: self.node.databases(
            "set_share_config", database=database_id, config=wire))
        if (ack["databaseId"] != database_id or ack["requestId"] != update["requestId"]
                or ack["committed"] is not True
                or int(ack["revision"]) != int(update["expectedRevision"]) + 1):
            raise PeopleError("LastDB returned a mismatched sharing acknowledgement")
        return ack["revision"]

    def share_call(self, operation):
        """One share call. The node waits for enrollment. People does not."""
        try:
            return operation()
        except LastDBError as error:
            raise_share_refusal(error)
            raise

    def seal(self, database_id, node_key, contact_code, revision, publisher):
        response = self.share_call(lambda: self.node.databases(
            "invite", database=database_id, contactCode=contact_code, expectedNodeKey=node_key))
        if response["databaseId"] != database_id or not response.get("invitation"):
            raise PeopleError("LastDB returned a mismatched invitation")
        return {"databaseId": database_id, "revision": revision, "publisher": publisher,
                "invitation": response["invitation"]}

    def accept(self, receipt, publisher, name):
        if receipt.get("publisher") != publisher:
            raise PeopleError("invitation publisher differs from the trusted publisher")
        return self.share_call(lambda: self.node.databases(
            "accept", invitation=receipt["invitation"], publisher=publisher,
            database_id=receipt["databaseId"], name=name))["database"]


def read_invitation(stream):
    raw = stream.read(8193)
    if len(raw) > 8192:
        raise PeopleError("invitation receipt exceeds its size limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise PeopleError("expected a People invitation receipt")
    return value
