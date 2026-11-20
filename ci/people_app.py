"""Run People's owner commands and selected-database reads in DEV."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import re
import subprocess

from node import Connection
from support import CREDENTIALS_FILE, OperationError, decode_json, require, retry, summary


ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "setups/shared-albums-v1"
SPEC = {"name": "whole-database", "version": 1}
GROUPS = ("person", "circle", "db", "grant", "request")
APP = "people"
RECEIPT = "lastdb-app-install.json"
HEX32 = re.compile(r"[0-9a-f]{64}\Z")


def lastdb_source():
    """The EdgeVector/lastdb checkout at the source commit of the build under test."""
    path = os.environ.get("LASTDB_SOURCE")
    require(bool(path) and Path(path, "lastdb_node/environments.json").is_file(), "lastdb_source_missing")
    return Path(path)


def apps_dir(node):
    return node.home.parent / "apps"


def prepare_launcher(node):
    """Lay out the launcher tree and register people on this node."""
    apps = apps_dir(node)
    current = apps / APP / "current"
    if not (current / RECEIPT).is_file():
        write_launcher_receipt(current)
    link = apps / "bin" / "people"
    if not link.is_symlink():
        link.parent.mkdir(parents=True, mode=0o700)
        link.symlink_to(node.programs.directory / "lastdb")
    if not getattr(node, "people_registered", False):
        register_people(node, apps)
        node.people_registered = True
    return link


def write_launcher_receipt(current):
    current.mkdir(parents=True, mode=0o700)
    source = current / "source"
    if not source.exists():
        source.symlink_to(ROOT)
    manifest = json.loads((ROOT / "lastdb-app.json").read_text(encoding="utf-8"))
    blank = "0" * 40
    listing = {
        "app_id": APP, "channel": "next", "app_version": manifest["version"],
        "source": manifest["source"], "sha": blank, "api_version": 1, "lastdb_build": "ci",
        "publisher_account": "0" * 32, "publisher_key": "ci", "publisher_sig": "ci",
        "status": "listed", "published_at": "2026-11-18T00:00:00Z",
        "access": {"read": "*", "write": "*", "extensions": ["databases"]},
    }
    receipt = {
        "app_id": APP, "sha": blank, "version": manifest["version"], "channel": "next",
        "index": "ci", "trust": "ci", "publisher": "ci", "consent_hash": "0" * 64,
        "inactive": False, "installed_at": "2026-11-18T00:00:00Z", "listing": listing,
    }
    (current / RECEIPT).write_text(json.dumps(receipt), encoding="utf-8")


def register_people(node, apps):
    manifest_path = apps / APP / "current" / "source" / "lastdb-app.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # One recipe puts both nodes under one apps directory. Register still
    # runs once per node. The directory is already there the second time.
    data = apps / APP / "data"
    data.mkdir(parents=True, mode=0o700, exist_ok=True)
    (data / "tmp").mkdir(mode=0o700, exist_ok=True)
    access = manifest["access"]
    reply = node.call("/api/apps/register", {
        "app": APP,
        "access": {"read": access["read"], "write": access["write"], "extensions": access["extensions"]},
        "data": str(data.resolve()),
    })
    require(reply.get("app") == APP, "people_app_register")


def command(node):
    """The `people` command, which is the launcher. It is not src/people itself.

    The launcher reads the node home from LASTDB_HOME before it sees --data-dir.
    A signed install keeps its own receipt. This still registers the app on the
    node that is about to run the command.
    """
    link = prepare_launcher(node)
    lastdb = str(node.programs.directory / "lastdb")
    return [str(link), "--data-dir", str(node.home), "--lastdb", lastdb]


def launcher_env(node):
    env = dict(node.programs.environment)
    env["LASTDB_APPS_ROOT"] = str(apps_dir(node))
    env["LASTDB_HOME"] = str(node.home)
    return env


def command_streams(result):
    """Decoded stderr and stdout. failure() redacts this pair and keeps the tail."""
    def text(value):
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode(errors="replace")
        return value

    return (text(result.stderr), text(result.stdout))


def sdk(node, action, **body):
    # The People client is this checkout's. LASTDB_APP_SDK holds the packed LastDB data client.
    require(Path(node.programs.environment.get("LASTDB_APP_SDK", ""), "dist/index.js").is_file(),
            "lastdb_app_sdk_missing")
    result = subprocess.run(
        ["node", str(ROOT / "ci/people_sdk.mjs"), action, *command(node)],
        input=json.dumps(body).encode(), capture_output=True, env=launcher_env(node), timeout=190,
    )
    if result.returncode != 0:
        raise OperationError(f"people_sdk_{action}", command_output=command_streams(result))
    return decode_json(result.stdout)


def install(publisher, root, revision):
    programs = publisher.programs
    require(len(revision) == 40 and all(char in "0123456789abcdef" for char in revision), "people_install_revision")
    home = root / "os-home"
    home.mkdir(mode=0o700)
    # The installer points the people command at this lastdb program.
    launcher = home / ".local/bin/lastdb"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    if not launcher.exists():
        launcher.symlink_to(programs.directory / "lastdb")
    extra = {"HOME": str(home), "LASTDB_APPS_ROOT": str(root / "apps"),
             "PATH": f"{programs.directory}:{os.environ['PATH']}"}
    catalog = programs.environment.get("PEOPLE_INSTALL_REGISTRY") or "dev"
    require(catalog in ("dev", "prod"), "people_install_registry")
    profile = decode_json((lastdb_source() / "lastdb_node/environments.json").read_bytes())
    index = profile["environments"][catalog]["registry_index"]
    receipt = programs.command(publisher.home, [
        "app", "install", "people", "--channel", "next", "--index", index, "--accept-install", "--json",
    ], extra=extra, timeout=150)
    require(isinstance(receipt, list) and len(receipt) == 1 and receipt[0]["sha"] == revision,
            "people_install_exact_source")
    # A restricted app's command link points at the lastdb program. Called as
    # `people`, that program is the launcher.
    link = home / ".local/bin/people"
    require(link.is_symlink() and os.readlink(link) == str(launcher), "people_install_command")
    summary(f"Installed People from the signed {catalog.upper()} registry at `{revision}`. "
            "Recipes start that install through the launcher.")


def people(node, *arguments, stdin=None, refusal=None, refusals=(), notices=None):
    """Run one People command and decode its JSON result."""
    label = "_".join(arguments[:2] if arguments[0] in GROUPS else arguments[:1]).replace("-", "_")
    try:
        if stdin is None:
            payload = None
        elif isinstance(stdin, (bytes, bytearray)):
            payload = bytes(stdin)
        elif isinstance(stdin, str):
            payload = stdin.encode()
        else:
            payload = json.dumps(stdin).encode()
        result = subprocess.run([*command(node), *arguments],
                                input=payload,
                                capture_output=True, env=launcher_env(node), timeout=180)
    except subprocess.TimeoutExpired as error:
        raise OperationError(f"people_{label}_deadline", command_output=command_streams(error)) from error
    output = command_streams(result)
    if refusal is not None:
        if not (result.returncode == 1 and not result.stdout and refusal in result.stderr):
            raise OperationError(f"people_{label}_expected_refusal", command_output=output)
        return None
    if result.returncode:
        # A known fragment selects the code. The same output is still the detail.
        for fragment, code in refusals:
            if fragment.encode() in result.stderr:
                raise OperationError(code, command_output=output)
        status = output[0].partition(" returned HTTP ")[2][:3]
        suffix = f"_http_{status}" if status.isascii() and status.isdigit() else ""
        raise OperationError(f"people_{label}_failed{suffix}", command_output=output)
    if notices is not None:
        notices.append(output[0])
    return decode_json(result.stdout)


def expect_setup_offline(node, name):
    """Create a setup on a node with no cloud account. Schemas land locally."""
    require(not (node.home / CREDENTIALS_FILE).exists(), "people_setup_offline_has_no_cloud_account")
    created = people(node, "db", "create", name, "--setup", "shared-albums")
    database = created["databaseId"]
    definitions = created["definitions"]
    require(HEX32.fullmatch(database) and created["name"] == name and created["setupName"] == "shared-albums"
            and created["version"] == 1 and "memberWrites" not in created
            and set(definitions) == {"social/Album", "social/AlbumItem", "social/Photo", "social/Comment"}
            and all(isinstance(item, str) and len(item) == 64 for item in definitions.values()),
            "people_setup_offline_schemas")
    shown = people(node, "db", "show", database)
    require(shown["database"]["id"] == database and shown["schemas"] == definitions,
            "people_setup_offline_shown")
    summary(f"Setup `{database}` was created with no cloud account; schemas applied locally.")
    return created


def pick(node, spec, intent, database):
    """Check one database selection. The result is its id, name and spec."""
    return people(node, "pick-database", "--spec", spec["name"], "--version", str(spec["version"]),
                  "--intent", intent, "--database", database)


def content(node, app, method, path, body=None, status=200):
    """One owner-socket call. The caller names a database in the body when the route takes one."""
    headers = {"X-LastDB-Client": app, "Content-Type": "application/json"}
    with closing(Connection(node.socket)) as connection:
        connection.request(method, path, None if body is None else json.dumps(body).encode(), headers)
        with connection.getresponse() as reply:
            require(reply.status == status, f"people_content_{app}_http_{reply.status}")
            return decode_json(reply.read(65537)) if status == 200 else None


def read_named(node, app, schema, key, database=None):
    """Read one record. A database id is a field of the read item."""
    item = {"schema": schema, "key": {"hash": key}}
    if database is not None:
        item["database"] = database
    reply = content(node, app, "POST", "/v1/read", {"reads": [item]})
    require(isinstance(reply.get("results"), list) and len(reply["results"]) == 1,
            f"people_content_{app}_read")
    return reply["results"][0]


def record_key(key):
    """A hash key, plus a range when the record has one."""
    if isinstance(key, str):
        return {"hash": key}
    body = {"hash": key["hash"]}
    if key.get("range"):
        body["range"] = key["range"]
    return body


def read_records(node, database, schema, keys):
    """Keyed reads of one schema. POST /v1/read names the database on each item."""
    reads = [{"database": database, "schema": schema, "key": record_key(key)} for key in keys]
    pages = node.call("/v1/read", {"reads": reads}).get("results")
    require(isinstance(pages, list) and len(pages) == len(keys), "record_read_pages")
    rows = []
    for page in pages:
        records = page.get("records") if isinstance(page, dict) and "error" not in page else None
        require(isinstance(records, list), "record_read_page")
        for record in records:
            fields = record.get("record") if isinstance(record, dict) else None
            revision = record.get("revision") if isinstance(record, dict) else None
            require(isinstance(fields, dict) and isinstance(revision, str) and revision, "record_read_row")
            row = {"key": record.get("key"), "revision": revision, "fields": fields,
                   "file": record.get("file")}
            # created_by and updated_by sit beside the record. The node omits a missing one.
            for name in ("created_by", "updated_by"):
                value = record.get(name)
                require(value is None or (isinstance(value, str) and len(value) > 0), "record_read_author")
                if value is not None:
                    row[name] = value
            rows.append(row)
    return rows


def write_owned(node, database, *updates):
    """Write records in a database this node owns.

    POST /v1/write takes op, key, record, and revision. It has no definition
    id, source file name, or metadata. A received database is not this route.
    A member proposal is the databases action submit. This helper is the owner write.
    """
    mutations = [owned_item(database, update) for update in updates]
    reply = node.call("/v1/write", {"mutations": mutations})
    revisions = reply.get("revisions")
    require(reply.get("written") == len(mutations) and isinstance(revisions, list)
            and len(revisions) == len(mutations)
            and all(isinstance(item, str) and item for item in revisions), "owned_write_receipt")
    return revisions


def owned_item(database, update):
    op = update["mutation_type"]
    item = {"database": database, "schema": update["schema"], "op": op,
            "key": record_key(update["key_value"])}
    if op != "delete":
        item["record"] = update["record"]
        item["alternates"] = update.get("alternates") or {}
    if op != "create":
        item["revision"] = update["expected"]["revision"]
    return item


def open_content_apps(recipient, database):
    choices = people(recipient, "pick-database", "--spec", SPEC["name"], "--version", str(SPEC["version"]),
                     "--intent", "read")
    require(any(row["databaseId"] == database for row in choices), "people_picker_received_database")
    selections = [(name, pick(recipient, SPEC, "read", database)) for name in ("album-viewer", "photo-journal")]
    require(all(selection["databaseId"] == database and selection["spec"] == SPEC
                for _, selection in selections), "people_picker_shared_database")
    refused = {"database": database, "schema": "social/Album", "op": "create",
               "key": {"hash": "album-refused"}, "record": {"title": "refused"}}
    for app, _selection in selections:
        # The personal database is the read that omits database. It is not this share.
        personal = read_named(recipient, app, "social/Album", "album-0")
        require("records" not in personal or personal["records"] == [], f"people_content_{app}_personal")
        # Grant roster is owner/chain derived. A received database is read-only on write.
        content(recipient, app, "POST", "/v1/write", {"mutations": [refused]}, status=403)
    return selections


def album_records(page):
    records = page.get("records") if isinstance(page, dict) else None
    return records if isinstance(records, list) else None


def read_album(node, selections, database, key, title=None):
    with ThreadPoolExecutor(max_workers=2) as pool:
        pages = list(pool.map(
            lambda item: read_named(node, item[0], "social/Album", key, database), selections))
    if title is None:
        return all(album_records(page) == [] for page in pages)
    return all(album_records(page) and len(album_records(page)) == 1
               and album_records(page)[0].get("record", {}).get("title") == title
               for page in pages)


def observe_access(node, database, state, tip=None, *, role="read"):
    """Wait until selfAccess matches #1805: `{role}` present, or omitted when revoked."""
    del tip

    def matches(result):
        access = (result.get("database") or {}).get("selfAccess")
        if state == "revoked":
            return access is None or access.get("role") in (None, "revoked")
        return isinstance(access, dict) and access.get("role") == role

    retry(f"people_recipient_access_{state}", lambda: people(node, "db", "show", database), matches)


def report_content_flow():
    message = ("Two content clients selected the same database, opened the same albums, and were refused "
               "on personal data, the roster, and reader writes. Both retained cached albums after revoke "
               "and caught up after regrant.")
    print(message, flush=True)
    summary(message)
