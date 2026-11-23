"""Prove Org against the DEV cloud: write members change records at once under a lease.

Org is the `write` role plus a lease length on the database. People sets the length
when the database gets its first writer and the publisher chose none, and clears it
when the last writer leaves. The node signs the leases. A write member whose node
holds a covering lease has a change applied at once and signed as a direct entry, and
no accept step follows. Any other member's change is a proposal. The app never decides
which: it calls the LastDB app SDK's `changeRecord` and gets `applied` or `pending`.

The recipe starts three nodes: the publisher, a writer (Ana) and a reader (Cam).
It uses a 60 second lease, as LastDB's own lease proof does. No clock is changed.
"""

import os
import sys
import time

from direct_report import attach, watch_rows
from people_app import ROOT, install, people, sdk, write_owned
from people_chain_views import self_access
from people_direct import create, enroll, fail, grant, handoff, revoke, wait_role
from share_node import (
    SCHEMA, await_decisions, await_empty_grants, connect, declare, ensure_relay, keyed,
    local_node, no_cloud_account, note, owner_databases, report, until,
)
from support import OperationError, run_operation, running

# The People constant that Org gives a database. It is read, not copied.
sys.path.append(str(ROOT / "src"))
from org import ORG_LEASE_MS, utc  # noqa: E402

LEASE_MS = 60_000
PROPOSALS = "Your changes are proposals. The publisher decides each one."


def now_ms():
    return int(time.time() * 1000)


def number(value):
    return str(value) if type(value) is int else "-"


def lease_state(node, database):
    """`{databaseId, role, leaseLengthMs?, lease}` of an owned database, from the owner socket."""
    reply = owner_databases(node, "lease", database=database)
    if reply.get("databaseId") != database:
        fail("org_lease_database", "id=0")
    return reply


def change(node, database, key, text):
    """One change through the LastDB app SDK. The node answers applied, pending, or refuses."""
    op = {"mutationType": "create", "key": {"hash": key, "range": None}, "alternates": {},
          "expected": {"type": "absent"}, "record": {"text": text}}
    return sdk(node, "change", database=database, schema=SCHEMA, op=op)


def seen(rows, text, author):
    return all(row and row["fields"].get("text") == text and row.get("created_by") == author
               and row.get("updated_by") == author for row in rows)


def default_length(publisher, name, writer_key):
    """Org gives a database with no lease length the People default. Leaving Org clears it."""
    database = create(publisher, name)
    await_empty_grants(publisher, database)
    if "leaseLengthMs" in lease_state(publisher, database):
        fail("org_default_start", "a new database already has a lease length")
    grant(publisher, database, writer_key, "write", [])
    length = lease_state(publisher, database).get("leaseLengthMs")
    if length != ORG_LEASE_MS:
        fail("org_default_length", "length=%s expected=%s" % (number(length), number(ORG_LEASE_MS)))
    text = people(publisher, "db", "show", database).get("writeAccess")
    if not (isinstance(text, str) and "Each lease lasts 3 days." in text):
        fail("org_default_text", "writeAccess=%s" % ("absent" if text is None else "differed"))
    result = people(publisher, "grant", "revoke", database, writer_key, "--apply")
    if result.get("applied") is not True:
        fail("org_default_revoke", "applied=%s" % result.get("applied"))
    if "leaseLengthMs" in lease_state(publisher, database):
        fail("org_default_clear", "the lease length stayed after the last writer was revoked")
    report("Org on a database with no lease length set the People default of 3 days, "
           "and revoking its last writer cleared it.")


def covered_writer(node, database):
    """Wait until the writer's node signs direct entries, with time left to use them."""
    def done(access):
        lease = (access or {}).get("lease") or {}
        return lease.get("directWrites") is True and lease.get("directWritesUntilMs", 0) - now_ms() > 20_000

    return until("org_lease_covers_writer", lambda: self_access(node, database), done)


def org_writer(publisher, database, keys, granted):
    """Org with a 60 second lease. People keeps a length the publisher already set."""
    reply = owner_databases(publisher, "lease_length", database=database, length_ms=LEASE_MS)
    if reply.get("leaseLengthMs") != LEASE_MS:
        fail("org_lease_length_set", "length=%s" % number(reply.get("leaseLengthMs")))
    grant(publisher, database, keys["Ana"], "write", granted)
    kept = lease_state(publisher, database).get("leaseLengthMs")
    if kept != LEASE_MS:
        fail("org_length_replaced", "length=%s" % number(kept))
    return kept


def writer_applied(ana, everyone, database, keys):
    covered_writer(ana, database)
    answer = change(ana, database, "org-1", "From the writer")
    if answer.get("status") != "applied":
        fail("org_writer_not_applied", "status=%s" % (answer.get("status") or answer.get("refused") or "-"))
    row = keyed(ana, database, ["org-1"])[0]
    if not seen([row], "From the writer", keys["Ana"]):
        fail("org_writer_not_at_once", "the writer's node does not hold the record the change applied")
    watch_rows(everyone, database, "org-1", "org_writer_visible",
               lambda rows: seen(rows, "From the writer", keys["Ana"]))
    report("The writer's change answered applied and was on its node at once. "
           "It reached the publisher and the reader with no accept step.")


def member_text(shown):
    lease = ((shown.get("database") or {}).get("selfAccess") or {}).get("lease") or {}
    return (lease.get("directWrites") is True
            and shown.get("writeAccess") == "You can write directly until %s." % utc(lease["directWritesUntilMs"]))


def publisher_text(shown):
    text = shown.get("writeAccess")
    return (isinstance(text, str) and "The current lease ends " in text and text.startswith(
        "Members with write access change records directly. Each lease lasts 1 minute."))


def shown_text(ana, publisher, database):
    """People's own view. A wait, because a renewal can land between two reads."""
    until("org_member_text", lambda: people(ana, "db", "show", database), member_text)
    until("org_publisher_text", lambda: people(publisher, "db", "show", database), publisher_text)
    report("People showed the writer its end time and the publisher the lease length and expiry.")


def reader_refused(cam, publisher, database):
    answer = change(cam, database, "org-reader", "From the reader")
    if answer != {"refused": "PermissionDeniedError"}:
        fail("org_reader_not_refused", "status=%s" % (answer.get("status") or answer.get("refused") or "-"))
    for node in (cam, publisher):
        if keyed(node, database, ["org-reader"])[0] is not None:
            fail("org_reader_record", "the reader's refused change left a record")
    report("The reader's change was refused (PermissionDeniedError) and left no record.")


def leave_org(publisher, ana, database, keys, everyone, granted):
    """Reduce the last writer to submit. The length clears, the node proposes, no lease follows."""
    grant(publisher, database, keys["Ana"], "all", granted)
    if "leaseLengthMs" in lease_state(publisher, database):
        fail("org_length_kept", "the lease length stayed after the last writer was reduced")
    wait_role(ana, database, "submit", None, "org_reduced", "Ana")
    access = self_access(ana, database) or {}
    if (access.get("lease") or {}).get("directWrites") is not False:
        fail("org_direct_after_reduction", "the reduced writer still signs direct entries")
    if people(ana, "db", "show", database).get("writeAccess") != PROPOSALS:
        fail("org_proposal_text", "writeAccess differed")
    answer = change(ana, database, "org-2", "After the reduction")
    sequence = answer.get("submissionSequence")
    if answer.get("status") != "pending" or type(sequence) is not int or sequence < 1:
        fail("org_reduced_not_pending", "status=%s" % (answer.get("status") or answer.get("refused") or "-"))
    if keyed(ana, database, ["org-2"])[0] is not None:
        fail("org_pending_applied", "the node holds a change it answered pending")
    found = await_decisions(ana, database, [answer["mutationId"]], "org_pending_decided")
    if found != ["published"]:
        fail("org_pending_outcome", "status=%s" % (found[0] if found else "-"))
    watch_rows(everyone, database, "org-2", "org_pending_visible",
               lambda rows: seen(rows, "After the reduction", keys["Ana"]))
    report("After the writer was reduced to submit, its change answered pending, was absent "
           "from its node, and was published after the publisher accepted it.")


def no_new_lease(publisher, database):
    """With the length cleared, the publisher signs no lease at the half-life it would have renewed."""
    last = lease_state(publisher, database)
    newest = last.get("lease") or {}
    if "leaseLengthMs" in last or type(newest.get("authoredAtMs")) is not int:
        fail("org_no_lease_start", "length=%s" % number(last.get("leaseLengthMs")))
    while now_ms() < newest["authoredAtMs"] + LEASE_MS // 2 + 15_000:
        time.sleep(2)
    later = lease_state(publisher, database)
    if "leaseLengthMs" in later or (later.get("lease") or {}).get("authoredAtMs") != newest["authoredAtMs"]:
        fail("org_lease_signed_after_org", "a lease was signed after the publisher left Org")
    if "writeAccess" in people(publisher, "db", "show", database):
        fail("org_publisher_text_kept", "the publisher view still shows a lease")
    report("After the publisher left Org no new lease was signed past the half-life it would have renewed at.")


def share(programs, root, publisher, ana, cam, database, name, granted):
    members = (("Ana", ana), ("Cam", cam))
    for node in (ana, cam):
        no_cloud_account(node)
    publisher_key, keys = enroll(programs, publisher, members, root)
    default_length(publisher, name + "-default", keys["Ana"])
    # The reader joins first, so no writer exists and the 60 second length is the only one.
    grant(publisher, database, keys["Cam"], "read", granted)
    handoff(publisher, cam, database, "Cam", publisher_key, "from-cam", "read", keys["Cam"])
    org_writer(publisher, database, keys, granted)
    handoff(publisher, ana, database, "Ana", publisher_key, "from-ana", "write", keys["Ana"])
    everyone = (("publisher", publisher), ("Ana", ana), ("Cam", cam))
    for person, node in members:
        watch_rows(((person, node),), database, "seed", "org_seed",
                   lambda rows: rows[0] and rows[0]["fields"].get("text") == "Seed")
    writer_applied(ana, everyone, database, keys)
    shown_text(ana, publisher, database)
    reader_refused(cam, publisher, database)
    leave_org(publisher, ana, database, keys, everyone, granted)
    no_new_lease(publisher, database)


def operation(programs, root, publisher, recipient):
    database = None
    roster = [("publisher", publisher), ("Ana", recipient)]
    try:
        if revision := os.environ.get("PEOPLE_INSTALL_REVISION"):
            install(publisher, root, revision)
        name = "ci-org-%s-%s" % (os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"])
        database = create(publisher, name)
        declare(publisher, database)
        write_owned(publisher, database, note("Seed", "seed"))
        connect(programs, publisher)
        await_empty_grants(publisher, database)
        ensure_relay(publisher, database)
        reader = local_node(programs, root, "cam")
    except OperationError as error:
        attach(error, roster, database, evidence=True)
        raise
    roster = [("publisher", publisher), ("Ana", recipient), ("Cam", reader)]
    granted, failed = [], False
    try:
        with running(reader):
            try:
                share(programs, root, publisher, recipient, reader, database, name, granted)
            except OperationError as error:
                attach(error, roster, database, evidence=True)
                raise
    except Exception:
        failed = True
        raise
    finally:
        try:
            revoke(publisher, database, list(dict.fromkeys(granted)), failed)
        except OperationError as error:
            attach(error, roster, database, evidence=True)
            raise


if __name__ == "__main__":
    # OwnedNode discards daemon stderr unless this name is set. The failure
    # detail reads that file from each node home.
    os.environ["PEOPLE_DIRECT_DAEMON_LOG"] = "daemon.log"
    run_operation(operation)
