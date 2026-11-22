"""Publish app-declared sharing through the owner's People commands in the signed DEV operation."""

import json
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from people_app import sdk, people
from share_node import chain_grants, owner_grant, owner_revoke
from support import OperationError, require, summary


def configure_bindings(publisher, circle, first, second, scopes):
    require(sdk(publisher, "circles") == [{"id": circle["id"], "name": circle["name"]}],
            "app_circle_discovery")
    decline_binding(publisher, circle["id"], first)
    requests = []
    for app, database, grant in (
        ("photo-journal", first, {"writeSchemas": scopes}),
        ("album-viewer", second, {"preset": "viewer"}),
    ):
        declaration = propose(publisher, app, {"operation": "setCircleBinding", "databaseId": database,
                                              "circleId": circle["id"], **grant})
        current = roster(publisher, database)
        require(not current["members"], "app_request_needs_owner_approval")
        request_id = declaration["requestId"]
        requests.append(request_id)
        preview = preview_request(publisher, request_id)
        require(preview["status"] == "awaitingApproval", "app_request_visible_on_reopen")
        approve(publisher, request_id, preview)
    for request_id in requests:
        status = sdk(publisher, "sharing-status", requestId=request_id)
        # Empty-circle binding confirms policy only; no grant tip exists yet.
        require(status["status"] == "confirmed", "app_request_confirmed_empty_circle")
    require(not people(publisher, "request", "list")["requests"], "app_confirmed_requests_leave_list")
    links = next(row["links"] for row in people(publisher, "circle", "list") if row["id"] == circle["id"])
    require(links == {first: sorted(scopes), second: []}, "app_declarations_saved")
    summary("Two app callers proposed independent bindings through the packaged SDK. The owner approved "
            "both and their confirmed status survived a new People process.")
    return links


def decline_binding(publisher, circle, database):
    change = {"operation": "setCircleBinding", "databaseId": database,
              "circleId": circle, "preset": "commenter"}
    declaration = propose(publisher, "photo-journal", change)
    request_id = declaration["requestId"]
    before = roster(publisher, database)
    pending = people(publisher, "request", "list")
    names = {row["requestId"]: (row["appId"], row["databaseId"]) for row in pending["requests"]}
    require(names == {request_id: ("photo-journal", database)} and pending["nextCursor"] is None,
            "app_request_found_on_regular_open")
    preview = preview_request(publisher, request_id)
    declined = people(publisher, "request", "decline", request_id)
    require(declined["status"] == "declined", "app_owner_declined")
    require(approve(publisher, request_id, preview) == declined, "app_decline_cannot_be_approved")
    require(not people(publisher, "request", "list")["requests"], "app_declined_request_leaves_list")
    require(people(publisher, "request", "decline", request_id) == declined,
            "app_decline_idempotent_after_reopen")
    status = sdk(publisher, "sharing-status", requestId=request_id)
    require(status["status"] == "declined" and sdk(publisher, "request-sharing", **declaration) == status
            and roster(publisher, database) == before, "app_decline_returned_through_sdk")
    summary("The owner found a proposal with `people request list` and declined it. It left the list, and its "
            "terminal status survived a repeated decline, a repeated request, and an old approval preview "
            "without changing the grants.")


def propose(publisher, app, change):
    request = {"requestId": str(uuid.uuid4()), "appId": app, "change": change}
    original = sdk(publisher, "request-sharing", **request)
    require(original["status"] == "awaitingApproval" and original["requestId"] == request["requestId"],
            "app_declaration_enrolled")
    require(sdk(publisher, "request-sharing", **request) == original, "app_declaration_durable_retry")
    return request


def resolutions(review):
    """Write `{database: {person: choice}}` as the --resolve flags of a People command."""
    return [flag for database, people in (review or {}).items() for person, choice in people.items()
            for flag in ("--resolve", f"{database}:{person}={choice}")]


def preview_request(owner, request_id, review=None):
    return people(owner, "request", "show", request_id, *resolutions(review))


def approve(owner, request_id, preview, review=None, refusal=None):
    return people(owner, "request", "approve", request_id, "--token", preview["token"], *resolutions(review),
                  refusal=refusal)


def roster(node, database):
    reply = chain_grants(node, database)
    members = []
    for row in reply.get("members") or []:
        members.append({"nodeKey": row["nodeKey"], "role": row["role"],
                        "state": "revoked" if row["role"] == "revoked" else "active"})
    return {"databaseId": database, "members": members,
            "entryHash": reply.get("entryHash"), "writerSeq": reply.get("writerSeq")}


def external_changes(publisher, circle, database, reader):
    person = Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    owner_grant(publisher, database, person, "read")
    change = {"operation": "setCircleBinding", "databaseId": database,
              "circleId": circle["id"], "preset": "commenter"}
    request_id = propose(publisher, "photo-journal", change)["requestId"]
    result = approve(publisher, request_id, preview_request(publisher, request_id))
    require(result["status"] == "confirmed", "unrelated_owner_grant_keeps_people_applicable")
    current = roster(publisher, database)
    require(any(row["nodeKey"] == person and row["state"] == "active" for row in current["members"]),
            "external_grant_preserved")
    outside = {"nodeKey": reader, "state": "active", "role": "read"}
    owner_grant(publisher, database, reader, "read")
    request_id = propose(publisher, "photo-journal", {**change, "preset": "viewer"})["requestId"]
    review = preview_request(publisher, request_id)
    conflict = review["databases"][0]["conflicts"]
    require(len(conflict) == 1 and conflict[0]["nodeKey"] == reader
            and conflict[0]["current"]["role"] == "read", "outside_scope_requires_review")
    approve(publisher, request_id, review, refusal=b"grants changed outside People")
    require(outside in roster(publisher, database)["members"], "outside_scope_preserved_before_review")
    choices = {database: {reader: "keep"}}
    preview = preview_request(publisher, request_id, choices)
    result = approve(publisher, request_id, preview, choices)
    require(result["status"] == "confirmed" and outside in roster(publisher, database)["members"],
            "outside_scope_retained_after_owner_review")
    summary("A separate publisher grant survived an app update. An overlapping grant required review "
            "and remained intact when the owner kept it as a direct grant.")
    person_requests(publisher, database, reader, person)
    return person


def person_requests(publisher, database, reader, separate):
    target = {"databaseId": database, "nodeKey": reader}
    # The roster is the signed chain: a revoked person's role is `revoked`.
    for operation, extra, state, role in (
        ("revokePerson", {}, "revoked", "revoked"),
        ("restorePerson", {"preset": "viewer"}, "active", "read"),
        ("setPersonGrant", {"preset": "viewer"}, "active", "read"),
    ):
        request = propose(publisher, "photo-journal", {**target, "operation": operation, **extra})
        request_id = request["requestId"]
        result = approve(publisher, request_id, preview_request(publisher, request_id))
        rows = roster(publisher, database)["members"]
        grant = next(row for row in rows if row["nodeKey"] == reader)
        kept = any(row["nodeKey"] == separate for row in rows)
        if not (result["status"] == "confirmed" and grant["state"] == state and grant["role"] == role and kept):
            raise OperationError("app_direct_grant_lifecycle", command_output=(
                "operation=%s result=%s state=%s role=%s want=%s.%s separate_kept=%s" % (
                    operation, result.get("status"), grant["state"], grant["role"], state, role,
                    "1" if kept else "0"), ""))
    summary("The app requested an explicit revoke and restore for one person, and the publisher kept "
            "the other independent grant in place.")


def independent_update(publisher, circle_id, first, second, reader, schema):
    owner_grant(publisher, first, reader, "submit")
    first_before = roster(publisher, first)
    edit = ("circle", "set", circle_id, "--link", f"{first}={schema}")
    preview = people(publisher, *edit)
    require(preview["databases"][0]["conflicts"][0]["nodeKey"] == reader,
            "app_overlapping_owner_edit_needs_review")
    people(publisher, *edit, "--apply", "--token", preview["token"], refusal=b"grants changed outside People")
    pending = people(publisher, *edit, "--apply", "--token", preview["token"], "--defer-conflicts")
    require(pending["pending"] == [first], "app_first_database_pending")
    overlap = propose(publisher, "photo-journal", {
        "operation": "setCircleBinding", "databaseId": first, "circleId": circle_id, "preset": "viewer",
    })
    people(publisher, "request", "show", overlap["requestId"], refusal=b"finish the pending sharing update")
    people(publisher, "request", "decline", overlap["requestId"])

    request_id = propose(publisher, "album-viewer", {
        "operation": "restorePerson", "databaseId": second, "nodeKey": reader, "preset": "viewer",
    })["requestId"]
    result = approve(publisher, request_id, preview_request(publisher, request_id))
    active = next(row for row in roster(publisher, second)["members"] if row["nodeKey"] == reader)
    require(result["status"] == "confirmed" and active["state"] == "active"
            and people(publisher, "circle", "show", circle_id)["pending"] == [first]
            and roster(publisher, first) == first_before, "app_second_database_confirmed_while_first_pending")

    result = people(publisher, "db", "retry", first, "--resolve", f"{first}:{reader}=keep")
    after = roster(publisher, first)
    require(not result["pending"] and member_rows(after) == member_rows(first_before)
            and sdk(publisher, "sharing-status", requestId=request_id)["status"] == "confirmed",
            "app_first_database_finished_without_disturbing_second")
    summary("A pending grant review blocked the same database while a second database confirmed its own "
            "owner-approved update. Resolving the first preserved the second outcome.")


def member_rows(config):
    return sorted((row["nodeKey"], row["role"], row["state"]) for row in config["members"])


def revoke_roster(node, database):
    current = roster(node, database)
    for row in current["members"]:
        if row["state"] == "active":
            owner_revoke(node, database, row["nodeKey"])
    require(all(row["state"] == "revoked" for row in roster(node, database)["members"]),
            "circle_cleanup_revoked")
