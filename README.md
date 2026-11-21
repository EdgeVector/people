# People

People remembers verified contacts in the local personal database and manages
access to owned named LastDB databases set up for sharing. This app was called
Social before 2026-11-18. It is a command-line tool. Use Python 3.9 or later
and the LastDB client and daemon from the same build. The publisher needs a
connected ExeMem account.

## How People talks to LastDB

The LastDB launcher starts an installed People command. It sets
`LASTDB_SOCKET_PATH` to the app socket (`<home>/data/apps.sock`) and
`LASTDB_APP` to `people`. People sends that id as `X-LastDB-App`.

Private records use `POST /v1/read` and `POST /v1/write`. The installer places
the schemas in `lastdb-app.json`. People does not declare those while it runs.

Named databases and sharing use one call, `POST /v1/x/databases`. The manifest
lists the extension `databases`. The JSON body carries `action`:

| Action | What People uses it for |
| --- | --- |
| `list`, `create`, `accept` | List databases, create one, accept a People consent code |
| `schemas`, `declare_schema` | Read a database's schemas, place a setup schema |
| `grant`, `revoke`, `grants` | Append or fold publisher-signed chain grants (authority) |
| `heads`, `relays`, `bootstrap_relay` | Signed heads for comparison; relay URL bootstrap |
| `share_link` | Mint local open-link material that People wraps as a consent code (no ExeMem) |
| `submit`, `submission_status` | A content app submits one proposal and reads its state. People's own commands do not call these |

`declare_schema` sends `app_id` from the catalog on the app socket and on the
owner socket. The shared-albums catalog uses `app_id` `social` and the names
`social/Album`, `social/AlbumItem`, `social/Photo`, and `social/Comment`.
LastDB stores those names. They are not in `access.read` or `access.write`.
The read list and the write list are the nine `people/<Name>` schemas. The
installer stores each manifest schema under the app id, so local name `Person`
becomes `people/Person`. Placing a schema is the `databases` extension.

`people` runs `lastdb share contact` and `lastdb share verify-contact` to
check a contact file. Those commands read `--data-dir` and do not open a
socket, so the app socket does not affect them.

```sh
people --data-dir "$lastdb_home" COMMAND ...
```

`LASTDB_HOME` can stand in for `--data-dir`. A command prints a plain table on
a terminal and JSON when its output is piped or when you pass `--json`. An
error prints one `people:` line on standard error and exits non-zero.

| Command | What it does |
| --- | --- |
| `person add NAME --node-key KEY --contact FILE` | Verify a contact file and save the contact |
| `person list`, `person show PERSON` | The address book. One person, with what you share and what they share with you |
| `person rename PERSON NAME`, `person remove PERSON` | Rename a contact. Remove one that has no active grant and is in no circle |
| `circle list`, `circle show CIRCLE` | Circles, their members, and their linked databases |
| `circle set CIRCLE [--members A,B] [--link DB=SCOPE] [--unlink DB] [--rename NAME]` | Create a circle, or change the named parts of one |
| `circle remove CIRCLE` | Remove a circle and the access it grants |
| `db create NAME [--setup shared-albums]` | Create a whole-database share, optionally from a named setup |
| `db verify ID --setup NAME [--finish]` | Verify a database against a named setup |
| `db list`, `db show DB` | Databases. One database with its roster, circles, and pending change |
| `db accept [FILE] --publisher KEY [--name NAME]` | Accept a People consent code (file or stdin) |
| `db retry [DB]` | Resume unfinished sharing updates |
| `grant list [DB]`, `grant revoke DB PERSON` | Active grants. Revoke one person |
| `grant set DB PERSON --write SCOPE`, `grant restore DB PERSON` | Give one person their own grant on a whole-database share. Restore a revoked person |
| `invite DB PERSON [--out FILE]` | Mint a hand-off consent code after a chain grant |
| `invite DB --open [--out FILE]` | Mint a Deliver consent code after `grant#any` |
| `request submit`, `request status ID` | For apps: propose a sharing change, read its outcome |
| `request list`, `request show ID`, `request approve ID`, `request decline ID` | For the owner: review and decide app proposals |
| `pick-database --spec NAME --version N --intent read\|write` | List the databases an app can use, or select one |
| `check` | Report that the People command started |

`PERSON` is a saved contact's name, or a node key. `DB` is a database name or
ID. `CIRCLE` is a circle name or ID.

## Preview, then apply

`circle set`, `circle remove`, `grant set`, `grant restore`, and `grant revoke`
change who can reach a database. Without `--apply` they print what would change and change nothing.

```sh
people circle set Family --members Ana,Bea --link family=social/Comment
people circle set Family --members Ana,Bea --link family=social/Comment --apply
```

A preview includes a `token`. Pass `--apply --token TOKEN` to make the change
only if it still matches that preview.

When another publisher tool changed a grant that People manages, the command
stops and names each one. Decide each with `--resolve DB:PERSON=keep` to keep
the outside grant as a direct grant, or `--resolve DB:PERSON=people` to apply
People's grant. Unrelated grants and explicit revocations are preserved.
A circle edit can instead take `--apply --defer-conflicts`: the other databases
are updated, and the one with the outside grant stays pending for
`db retry DB --resolve ...`.

People applies each database independently and saves unfinished work. An
unfinished update only blocks new changes to the same database. `db retry`
resumes it.

## Contacts

`lastdb share contact --out PATH` exports a contact file and its public node
key. Check the key with the person through another channel, then save them:

```sh
people person add Ana --node-key "$ana_key" --contact ana.contact
```

## Whole-database sharing

`people db create NAME` creates an empty share for your app's own schemas.
Place each schema with `POST /v1/x/databases` and `"action": "declare_schema"`.
The body has `database`, `app_id`, `schema`, and optional `retention` and
`intent`. `schema.name` is the stored name, such as `social/Comment`.
`app_id` is the namespace of that name. The app declares the `databases`
extension. That name is not an `access.write` entry.
Put keys in `record_keys`. The route refuses a schema-level `key`.
`retention` sits beside `schema`, not inside it. Then link a circle to the
database.

A circle is one list of people with links to any number of databases. Each
link carries a write scope: `read`, `all`, or a comma-separated list of
canonical schema names such as `social/Comment`. Every member can read each
whole database. Write access combines across overlapping circles. `all` also
covers future schemas. An explicit revoke on a database stays in effect
across later circle edits.

```sh
people circle set Family --members Ana,Bea --link family=social/Comment --link trips=read --apply
people invite family Ana --out ana-consent.txt
```

For one person outside any circle, use a direct grant. It combines with the
person's circle grants. A revoked person comes back only through an explicit
`grant restore`.

```sh
people grant set family Cal --write social/Comment --apply
people grant revoke family Cal --apply
people grant restore family Cal --write read --apply
```

`invite` mints a People consent code after a signed chain grant. The JSON has
`databaseId`, `entryHash`, `writerSeq`, `publisher`, and `consent` (a
`people-consent-v1.` string). With `--out` it writes only the consent code to
a file only you can read. Copy and paste that code into the other People app.
ExeMem and the relay never see the invite; they only see signed grant entries.
The recipient accepts with the publisher's independently verified key:

```sh
people db accept ana-consent.txt --publisher "$trusted_publisher_key" --name family
```

`db accept` returns LastDB's received database metadata. The local LastDB
bootstraps the first relay URL from the publisher-signed `relays` entry and
replays the signed log. `db show` reports folded chain grants and signed
heads for comparison.

Compatible schema upgrades keep a grant in place. The publisher still checks
the specific current definition and the record rules for each write. On a
whole-database share, a receiver with a `social/Comment` write grant can
create comments and edit comments they created. The author is `created_by`
on the `POST /v1/read` reply: the lowercase hex node key of the writer.
LastDB sets it when the comment is created and keeps it on a replace.
`updated_by` is the node key of the writer of this revision. A write that
names `created_by` or `updated_by` is refused. The `author_id` field is not
the author. It is whatever the writer stored, or empty. A comment with no
`created_by` stays editable only by the publisher. The publisher can edit or
delete any comment. The shared-albums catalog schema is `social/Comment`,
so that rule applies to comments in the setup.

### Named setups

`people db create family --setup shared-albums` creates a whole-database share
and places `social/Album`, `social/AlbumItem`, `social/Photo`, and
`social/Comment` through the `declare_schema` action. The node owns the wait
for cloud enrollment, up to 30 seconds. People does not wait and does not
retry.

A node with no cloud account answers HTTP 409, `this node has no cloud
account`. `db create --setup` and `db verify --setup` do not fail. The
database exists and the schemas are placed. Run
`people db verify ID --setup shared-albums --finish` once the node is
connected if setup was interrupted.

HTTP 503 `the database is still enrolling with the cloud; retry`, and any
other HTTP 503, is an error. People exits non-zero and tells you to run the
same command again. A grant-chain confirmation failure is an error too: the
node is enrolled and connected, and the service answer could not be
confirmed.

People records the new database ID before placing those schemas, and resumes
setup by the same name. `people db verify ID --setup shared-albums` checks an
owned or received share by its stable ID and reports the current definition
IDs. Add `--finish` to install missing schemas in an owned database.
Additive schema updates can introduce more fields under the same name. When
replacing a record, read it first, retain the fields and keys you aren't
editing, and send the replacement against the record's current revision.

## Sharing proposals from apps

A local app lists circles with `people circle list` and submits a proposal as
JSON on standard input to `people request submit`. Reuse the same request UUID
when retrying the same declaration. The proposal is saved with
`awaitingApproval`. `people request status UUID` reports its outcome.

An app can request a circle binding, remove a binding, set a direct person
grant, revoke, or explicitly restore a person. It gives a list of installed
canonical schema names or one of the `viewer`, `commenter`, or
`album-organizer` presets.

The owner decides:

```sh
people request list
people request show UUID
people request approve UUID
people request decline UUID
```

`request show` prints the access the proposal would produce. After approval a
circle binding follows later changes to the circle. A decline is final, and
reusing a declined request's UUID returns the same outcome.

The owner socket trusts every program that runs as its OS user, so an app
running as the owner could run `request approve` itself. Run content apps
outside that trust boundary before treating owner approval as app isolation.

## How sharing works

An active member with role `submit` can propose a change. The receiver posts
`POST /v1/x/databases` with `"action": "submit"`, the database id, the
mutation document, and `idempotency_key` (one non-nil lowercase UUID). The
call answers 202 with `ok`, `status: submitted`, `mutation_id`, and
`submission_sequence`. It does not write a record on the receiver. An owned
database or an unknown id answers 404. A share that is not a whole database
answers 403. A member whose role is `read` is refused: HTTP 403, code
`permission_denied`, message `this member cannot submit this shared mutation`,
and no `Retry-After`. Do not retry. The publisher does not receive that
proposal. Only the publisher's LastDB can read pending changes. It
incorporates accepted proposals onto the signed history. Receivers follow
that log and can read the resulting record after it syncs, with
`POST /v1/read` and the `database` field. An app that wants to show the
sender's own change before the publisher accepts it uses
`"action": "submission_status"` and shows that pending state.
`POST /v1/write` to a received database is refused unless the member's chain
role is `write`.
[How sharing works](https://github.com/EdgeVector/lastdb/blob/main/docs/designs/cloud-api.md)

People turns Share, Deliver and Org into publisher-signed grant and revoke
chain entries through LastDB. People may carry signed heads for comparison
only and bootstraps the first relay URL from a publisher-signed `relays`
entry. A grant that exists only in People is never authoritative.
[Relay protocol](https://github.com/EdgeVector/lastdb/blob/main/docs/designs/relay-protocol.md)

## What members can change

There is no database-wide write map. Access is the per-member role on the
publisher-signed grant chain: `read`, `submit`, `write`, or `revoked`. People
reads that role from the grant (and from `selfAccess` once LastDB folds
`GrantState`). Deliver uses `grant#any`/`read`. Org uses `write`. Share uses
`submit` for proposals. A member with role `read` cannot submit. A circle
link or a preset still chooses `read` or `submit` (or `write` for Org).
People invites are hand-off consent codes. LastDB authorizes access from
signed grant entries on the chain; there is no ExeMem invite or share-config
path.

## Choosing a database for an app

`people pick-database --spec whole-database --version 1 --intent read` lists
the databases that match. Add `--database ID` to check one selection. The
command returns that database's id, name, and spec. The app names that id in
the `database` field of each `POST /v1/read` and `POST /v1/write` item. A
write to a received database is refused. A change to a received
whole-database share is the proposal described in How sharing works. LastDB
limits an app by the schemas it declared, not by database.

JavaScript and TypeScript apps use [`@lastdb/people-client`](client/README.md)
in `client/`. Its `peopleClient` runs these commands and checks their output.
It ships with People, so it always matches the installed commands.

## How People is checked

A pull request gets syntax, manifest and command-grammar checks
(`.github/workflows/ci-required.yml`). It never receives cloud credentials.

`.github/workflows/dev-sharing.yml` runs on `main` in four ways:

- Every push to `main`.
- An hourly poll at minute 41 UTC (`41 * * * *`). The poll selects the newest
  published LastDB nightly: a release of `EdgeVector/lastdb` whose tag matches
  `nightly-YYYYMMDD-<9 hex characters>` and is not a draft. If a sharing job
  on `main` has already started for that tag, the poll skips the sharing jobs
  and finishes green. "Already attempted" means started, not green. The
  sharing job checks out this commit, then saves an Actions cache entry
  `nightly-attempted-<tag>` before it downloads the nightly. A later poll
  treats an exact cache hit as attempted. A red
  nightly is not repeated every hour. A poll cancelled while it is still
  pending never starts sharing, so it does not save the entry and the next
  poll tries again. The daily run, a push, a manual dispatch, and a re-run
  of a poll ignore this rule and always start sharing.
- Every day at 09:23 UTC (`23 9 * * *`).
- A manual dispatch from `main`. Leave `lastdb_release` empty to use the
  newest published nightly, or set it to a tag. Or set `lastdb_source_sha` to
  a 40-hex LastDB commit that includes the People UDS grants (mutually
  exclusive with `lastdb_release`); the job builds `lastdb`/`lastdbd` from
  that public SHA (default on dispatch: the #1816 squash on main (drain outbox
  batches before pulls) `009d853b2e09ef02381a974c739b422ebb8203fb`). Set `relay_url` to the ExeMem
  DEV HTTP origin (`https://….execute-api….amazonaws.com`, no `/relay` path,
  no credentials). Empty `relay_url` uses `environments.dev.exemem_api` from
  the LastDB source under test.

Push runs and the daily run also use the newest published nightly and the
default DEV relay origin.

Database and stream/device ids are 64-character lowercase hex (Hex32), not
UUID. Sharing-request and circle ids remain UUIDs.

To re-run a red nightly by hand:

```
gh workflow run dev-sharing.yml -R EdgeVector/people --ref main -f mode=all -f lastdb_release=<tag>
```

To prove against a pinned LastDB SHA and the live DEV relay:

```
gh workflow run dev-sharing.yml -R EdgeVector/people --ref main \
  -f mode=all -f lastdb_source_sha=<40-hex> -f relay_url=https://….execute-api….amazonaws.com
```

A red run on `main` from a schedule or a push opens or updates one GitHub
issue in this repository. The issue is labeled `scheduled-failure`, assigned
to `shiba4life`, and titled `Scheduled failure: DEV sharing`. A later
schedule or push run whose sharing jobs succeed closes it. An hourly poll
that skips sharing does not close it. A manual dispatch does not open or
close it.

`people-direct` starts four fresh LastDB nodes. The
other modes start two, a publisher and a recipient. The nodes come from the
newest published LastDB nightly. The publisher connects to the DEV cloud.
The recipe drives this commit's `people` command through the launcher, on
the app socket, with `people` registered:

| Mode | What it proves |
| --- | --- |
| `people-share` | Save a contact, set up `shared-albums`, grant, invite, accept, read, revoke and restore on a whole-database share, with two scoped content clients |
| `people-circles` | Circles grant submit from the chain role, Comment proposals, app sharing requests, grants changed outside People, direct grant, revoke and restore |
| `people-prepare` | The manifest and schemas produce a valid unsigned registry declaration for this commit on a fresh node |
| `people-link` | Deliver via `grant#any` plus a People consent-code hand-off; LastDB refuses revoke of `any` |
| `people-direct` | Org `write` members append directly, converge on one key, a reader is refused, and reducing to `submit` restores proposals |

Where a recipe is the node owner, it keeps the owner socket. That is schema
placement, roster reads and writes, share settings, a snapshot, an open
link, a member's proposal, a direct member write, cloud backup, and node
health. People's own commands do not use those routes.

The recipes are in `ci/`, and the node helpers they use are in `ci/lib/`.
Two things come from `EdgeVector/lastdb`: the endpoint profile
`lastdb_node/environments.json` and the LastDB app SDK's data client, which
`ci/people_sdk.mjs` uses to read shared records. Both are taken at the
nightly's exact source commit. The People client the recipes exercise is this
repo's `client/`.
