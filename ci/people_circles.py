"""Exercise a People circle's whole-database reads and scoped writes in DEV."""

from contextlib import closing
import json
import os
import sys
import uuid

from circle_comments import (
    exercise_comment_ownership, exercise_reader, exercise_upgrade, queue_and_restart, verify_results,
    verify_settled_retry,
)
from circle_report import (
    attach, cell, check, fail, mark, require_absent, saw, wait, wait_field,
)
from direct_nodes import clip_public
from node import Connection
from people_app import expect_setup_member_writes_pending, extend_member_writes, install, observe_access, read_records, sdk, people, write_owned
from people_declarations import configure_bindings, external_changes, independent_update, revoke_roster
from people_log_views import scope_for
from support import OperationError, decode_json, run_operation, summary

SCOPE = {"name": "whole-database", "version": 1}
ALBUM = "dev-albums/Album"
COMMENT = "dev-comments/Comment"
STANDARD_COMMENT = "social/Comment"
COMMENT_GRANT = {"names": [COMMENT, STANDARD_COMMENT]}
REFUSALS = (
    ("select writable schemas", "circle_invalid_schema_selection"),
    ("save verified contacts", "circle_contact_unavailable"),
    ("share configuration is not confirmed", "circle_lastdb_unconfirmed"),
    ("share_configuration_unconfirmed", "circle_lastdb_unconfirmed"),
    ("invalid_share_configuration", "circle_lastdb_rejected_config"),
    ("the share configuration is at another revision", "circle_revision_changed"),
    ("share_update_pending", "circle_update_pending"),
    ("mismatched sharing acknowledgement", "circle_ack_mismatch"),
    ("changed while you edited", "circle_revision_changed"),
    ("POST /v1/write", "circle_private_write_refused"),
    ("POST /v1/read", "circle_private_read_refused"),
)


def declare(node, database, schema, *, expanded=False):
    app_id, name = schema.split("/")
    fields = {"text": "Sample text."}
    if expanded:
        fields["caption"] = "Optional caption on the same record."
    return node.call(f"/api/databases/{database}/apps/declare-schema", {
        "app_id": app_id, "schema": {
            "name": name, "descriptive_name": name, "purpose_statement": f"Disposable DEV {name} record.",
            "schema_type": "Hash", "record_keys": {"primary": {"kind": "hash"}, "alternates": {}},
            "fields": list(fields), "field_types": dict.fromkeys(fields, "String"), "field_descriptions": fields,
        },
    })["definition_id"]


def mutation(schema, definition, key, text):
    return {"type": "mutation", "schema": schema, "definition_id": definition,
            "key_value": {"hash": key, "range": None}, "alternates": {},
            "mutation_type": "create", "expected": {"type": "absent"}, "record": {"text": text}}


def query(node, database, schema, key):
    return read_records(node, database, schema, [key])


def wait_text(name, node, database, schema, key, text, stage):
    wait_field(name, stage, lambda: query(node, database, schema, key), "text", text)


CANNOT_SUBMIT = "this member cannot submit this shared mutation"


def submit(node, database, update, *, mutation_id=None):
    """Append one proposal from a member who may submit. Only 202 succeeds.

    An append the node cannot confirm is 503 and carries Retry-After. This
    helper does not pause. A read-role member uses refuse_submission.
    """
    mutation_id = mutation_id or str(uuid.uuid4())
    path = f"/api/databases/{database}/submissions"
    with closing(Connection(node.socket)) as connection:
        connection.request("POST", path, json.dumps(update), {
            "Content-Type": "application/json", "Idempotency-Key": mutation_id,
        })
        with connection.getresponse() as reply:
            raw = reply.read(65537)
            if reply.status != 202:
                fail("circle_submission_http_%s_expected_202" % reply.status,
                     _refused(path, reply, raw, update))
            body = decode_json(raw)
    sequence = body.get("submission_sequence")
    check(body.get("ok") is True and body.get("mutation_id") == mutation_id
          and body.get("status") == "submitted" and type(sequence) is int and sequence > 0,
          "circle_submission_receipt",
          saw("receipt", ok=body.get("ok"), status=body.get("status"), id_match=body.get("mutation_id") == mutation_id,
              sequence=sequence if type(sequence) is int else None))
    return mutation_id


def refuse_submission(node, database, update):
    """A read-role proposal is a final 403. 202 and 503 both fail."""
    path = f"/api/databases/{database}/submissions"
    with closing(Connection(node.socket)) as connection:
        connection.request("POST", path, json.dumps(update), {
            "Content-Type": "application/json", "Idempotency-Key": str(uuid.uuid4()),
        })
        with connection.getresponse() as reply:
            raw = reply.read(65537)
            detail = _refused(path, reply, raw, update)
            if reply.status != 403:
                fail("circle_submission_http_%s_expected_403" % reply.status, detail)
            retry_after = reply.getheader("Retry-After")
    message = raw.decode("utf-8", "replace").strip()
    check(message == CANNOT_SUBMIT and not retry_after, "circle_reader_submission_refused", detail)


def _refused(path, reply, raw, update):
    retry_after = reply.getheader("Retry-After") or "-"
    return "POST %s status=%s retry_after=%s %s body=%s" % (
        path, reply.status, _mark(str(retry_after)), _proposal(update), _body(raw))


def _proposal(update):
    key = update.get("key_value") if isinstance(update, dict) else None
    ident = key.get("hash") if isinstance(key, dict) else None
    schema = update.get("schema") if isinstance(update, dict) else None
    kind = update.get("mutation_type") if isinstance(update, dict) else None
    return "schema=%s op=%s key=%s" % (_mark(schema), _mark(kind), _mark(ident))


def _mark(value):
    if isinstance(value, str) and value and len(value) <= 48 and all(char.isalnum() or char in "_./:-" for char in value):
        return value
    return "-"


def _body(raw):
    """The owner socket returns the sentence. A JSON error keeps its code and message."""
    try:
        parsed = decode_json(raw)
    except OperationError:
        parsed = None
    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict):
            return clip_public("%s %s" % (error.get("code") or "-", error.get("message") or "-"), 80)
        if isinstance(error, str):
            message = parsed.get("message")
            text = error if not isinstance(message, str) else "%s %s" % (error, message)
            return clip_public(text, 80)
    text = raw.decode("utf-8", "replace").split("\n", 1)[0]
    return clip_public(text, 80)


def transfer(programs, root, publisher, recipient, api_key):
    name = f"ci-circles-{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}"
    state = {"database": None}
    roster = [("publisher", publisher), ("Ana", recipient)]
    try:
        _transfer(programs, root, publisher, recipient, api_key, name, state)
    except OperationError as error:
        attach(error, roster, state["database"])
        raise


def _transfer(programs, root, publisher, recipient, api_key, name, state):
    if revision := os.environ.get("PEOPLE_INSTALL_REVISION"):
        install(publisher, root, revision)
    expect_setup_member_writes_pending(recipient, f"{name}-offline")
    created = sdk(publisher, "create-shared", name=name)
    check(created["sharingMode"] == "whole_database" and created["name"] == name, "circle_sdk_create",
          saw("create", mode=created.get("sharingMode"), name_match=created.get("name") == name))
    database = created["id"]
    state["database"] = database
    report(f"Owned DEV mutation log {database} for {name} through the packaged SDK.")
    second = create_database(publisher, f"{name}-second")
    definitions = {schema: declare(publisher, database, schema) for schema in (ALBUM, COMMENT)}
    write_owned(publisher, database, *(
        mutation(schema, definitions[schema], "seed", f"Initial {schema}") for schema in (ALBUM, COMMENT)))
    definition = declare(publisher, second, COMMENT)
    write_owned(publisher, second, mutation(COMMENT, definition, "seed", "Second database"))
    publisher.stop()
    connect(programs, publisher, api_key)
    scope = scope_for(publisher, api_key)
    publisher.keep_saved_credentials = True
    publisher.start()
    contact = root / "recipient.contact"
    reader = programs.command(recipient.home, ["share", "contact", "--out", str(contact)])["recipient_public_key"]
    sender = programs.command(publisher.home, ["share", "contact", "--out", str(root / "owner.contact")])["recipient_public_key"]
    check(reader != sender, "circle_two_identities", saw("keys", distinct=reader != sender))
    people(publisher, "person", "add", "Ana", "--node-key", reader, "--contact", str(contact), refusals=REFUSALS)
    wait_cloud("circle_cloud_ready", publisher, database)
    placed = people(publisher, "db", "verify", database, "--setup", "shared-albums", "--finish")
    check(placed["databaseId"] == database and placed["memberWrites"] != "pending", "circle_setup_finish",
          saw("setup", id_match=placed.get("databaseId") == database, pending=placed.get("memberWrites") == "pending"))
    definitions.update(placed["definitions"])
    member_writes = extend_member_writes(publisher, database, {COMMENT: "shared", ALBUM: "owner_only"})
    shown = set(people(publisher, "db", "show", database)["schemas"])
    check(set(definitions).issubset(shown), "circle_schema_catalog_missing",
          saw("schemas", missing=len(set(definitions) - shown)))
    wait_cloud("circle_second_cloud_ready", publisher, second)
    try:
        share_with_circle(publisher, recipient, database, second, definitions, scope, reader, sender, member_writes)
    finally:
        failed = sys.exc_info()[0] is not None
        cleanup_error = None
        for target in (database, second):
            try:
                cleanup(publisher, target)
            except OperationError as error:
                summary(f"The final DEV recipient revocation for {target} could not be confirmed.")
                cleanup_error = cleanup_error or error
        if cleanup_error and not failed:
            raise cleanup_error


def share_with_circle(publisher, recipient, database, second, definitions, scope, reader, sender, member_writes):
    created = edit_circle(publisher)
    circle = created["circle"]
    check(revisions(created) == {} and circle["name"] == "Family" and not circle["members"]
          and not circle["links"], "circle_starts_as_private_membership",
          saw("circle", revisions=len(revisions(created)), name=circle.get("name"),
              members=bool(circle.get("members")), links=bool(circle.get("links"))))
    configure_bindings(publisher, circle, database, second, COMMENT_GRANT["names"])
    same_revisions(edit_circle(publisher, "--members", reader), {database: "1", second: "1"}, "circle_grant_revisions")
    receipt = sdk(publisher, "seal", database=database, nodeKey=reader)
    check(receipt["publisher"] == sender and receipt["databaseId"] == database and receipt["revision"] == "1",
          "circle_sdk_sealed_invitation",
          saw("seal", publisher=receipt.get("publisher") == sender, database=receipt.get("databaseId") == database,
              revision=receipt.get("revision")))
    exercise(publisher, recipient, database, receipt, definitions, scope, member_writes)
    exercise_comment_ownership(publisher, recipient, database, definitions, scope, reader)
    second_receipt = people(publisher, "invite", second, reader)
    people(recipient, "db", "accept", "--publisher", sender, "--name", "second", stdin=second_receipt)
    wait_text("Ana", recipient, second, COMMENT, "seed", "Second database", "circle_second_database_read")
    observe_access(recipient, second, "active", 1)
    definitions[COMMENT] = exercise_upgrade(publisher, recipient, database, definitions[COMMENT], scope, member_writes)
    read_links = ("--link", f"{database}=read", "--link", f"{second}={COMMENT}")
    preview = people(publisher, "circle", "set", "Family", *read_links)
    people(publisher, "grant", "revoke", second, reader, "--apply")
    people(publisher, "circle", "set", "Family", *read_links, "--apply", "--token", preview["token"],
           refusal=b"sharing changed since the preview")
    same_revisions(edit_circle(publisher, *read_links), {database: "2", second: "2"}, "circle_reader_revision")
    observe_access(recipient, second, "revoked")
    exercise_reader(publisher, recipient, database, definitions, scope)
    external_changes(publisher, circle, database, reader)
    independent_update(publisher, circle["id"], database, second, reader, COMMENT)
    report("One reusable circle granted distinct access to two databases and preserved an explicit revoke after an edit.")

def create_database(owner, name):
    database = people(owner, "db", "create", name)["id"]
    report(f"Owned DEV mutation log {database} for {name}.")
    return database


def report(message):
    print(message, flush=True)
    summary(message)


def edit_circle(owner, *arguments):
    edit = ("circle", "set", "Family", *arguments)
    preview = people(owner, *edit, refusals=REFUSALS)
    errors = sum(1 for row in preview["databases"] if "error" in row)
    check(errors == 0, "circle_reusable_preview", saw("preview", errors=errors))
    response = people(owner, *edit, "--apply", "--token", preview["token"], refusals=REFUSALS)
    check(not response["pending"], "circle_reusable_apply", saw("apply", pending=bool(response.get("pending"))))
    return response


def revisions(response):
    return {row["databaseId"]: row["revision"] for row in response["databases"]}


def same_revisions(response, expected, code):
    found = revisions(response)
    values = ".".join(sorted(str(item) for item in found.values()))
    check(found == expected, code, saw("revisions", match=found == expected, values=values))


def wait_cloud(stage, node, database):
    wait(stage, lambda: people(node, "db", "show", database), lambda row: _sharing_revision(row) == "0",
         lambda row, reason: saw(reason, revision=_sharing_revision(row)))


def _sharing_revision(row):
    sharing = row.get("sharing") if isinstance(row, dict) else None
    return sharing.get("revision") if isinstance(sharing, dict) else None


def cleanup(publisher, database):
    roster = people(publisher, "db", "show", database, refusals=REFUSALS)["sharing"]
    if roster.get("status") == "pending":
        people(publisher, "db", "retry", database, refusals=REFUSALS)
        roster = people(publisher, "db", "show", database, refusals=REFUSALS)["sharing"]
    check(roster.get("members") is not None, "circle_cleanup_roster_unavailable",
          saw("roster", status=roster.get("status"), members=roster.get("members") is not None))
    revoke_roster(publisher, database)
    report("ExeMem confirmed the final recipient revocations.")


def connect(programs, publisher, api_key):
    receipt = programs.command(publisher.home, [
        "backup", "connect", "--existing", "--env", "dev", "--api-key-stdin",
        "--expected-user-hash", publisher.account, "--json",
    ], input_bytes=api_key, timeout=20)
    check(receipt["environment"] == "dev" and receipt["account_id"] == publisher.account
          and receipt["credentials_saved"] is True, "circle_dev_connection",
          saw("connect", env=receipt.get("environment"), account=receipt.get("account_id") == publisher.account,
              saved=receipt.get("credentials_saved")))


def exercise(publisher, recipient, database, receipt, definitions, scope, member_writes):
    accepted = sdk(recipient, "accept", receipt=receipt, publisher=receipt["publisher"], name="family")
    check(accepted["id"] == database and accepted["sharingMode"] == "whole_database", "circle_received_database",
          saw("accept", id_match=accepted.get("id") == database, mode=accepted.get("sharingMode")))
    for schema in (ALBUM, COMMENT):
        wait_text("Ana", recipient, database, schema, "seed", f"Initial {schema}", "circle_whole_database_read")
    album = sdk(recipient, "read", database=database, schema=ALBUM, key="seed")
    second_app = sdk(recipient, "read", database=database, schema=ALBUM, key="seed", appId="album-viewer")
    check(second_app == album, "independent_app_opens_same_published_album",
          "%s %s" % (cell("Ana", album, "text"), cell("viewer", second_app, "text")))
    check(album["fields"]["text"] == f"Initial {ALBUM}", "circle_sdk_read_shared_content", cell("Ana", album, "text"))
    report("The packaged SDK used People to create a whole-database share, seal a circle invitation and accept it. The recipient read shared data through the LastDB SDK.")
    observe_access(recipient, database, "active", 1, role="submit", member_writes=member_writes)
    sdk_access = next(row for row in sdk(recipient, "list-databases") if row["id"] == database)["selfAccess"]
    check(sdk_access["state"] == "active" and sdk_access["stale"] is False and sdk_access["role"] == "submit"
          and sdk_access.get("memberWrites") == member_writes, "circle_sdk_confirmed_grant",
          saw("grant", state=sdk_access.get("state"), role=sdk_access.get("role"), stale=sdk_access.get("stale"),
              writes=sdk_access.get("memberWrites") == member_writes))
    created = mutation(COMMENT, definitions[COMMENT], "allowed", "From Ana")
    created["metadata"] = {"share_author": "unverified-author"}
    updates = [
        mutation(ALBUM, definitions[ALBUM], "blocked", "Cannot write"),
        created,
        mutation(COMMENT, definitions[COMMENT], "seed", "A create cannot replace the seed"),
    ]
    queued = queue_and_restart(publisher, recipient, database, scope, updates,
                               check_status=True)
    verify_results(recipient, database, queued)
    verify_settled_retry(publisher, recipient, database, scope, updates[1], queued[1])
    for name, node in (("publisher", publisher), ("Ana", recipient)):
        wait_text(name, node, database, COMMENT, "allowed", "From Ana", "circle_authorized_write_applied")
        require_absent(name, query(node, database, ALBUM, "blocked"), "circle_forbidden_write_missing", "text")
        wait_text(name, node, database, COMMENT, "seed", f"Initial {COMMENT}", "circle_invalid_create_discarded")
    message = "The publisher kept Ana's Comment and discarded the Album write and the create that targeted the seed."
    print(message, flush=True)
    summary(message)


if __name__ == "__main__":
    run_operation(transfer)
