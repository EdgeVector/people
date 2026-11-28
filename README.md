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
| `change` | A content app changes one record. The node applies it (a write member under a lease) or queues it as a proposal. People's own commands do not call it |
| `lease`, `lease_length` | Read a database's lease length and newest lease. Set or clear the lease length of an owned database (Org) |

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
| `pick-database --spec NAME --version N --intent read\|write` | List the databases an app can use, or select one. `write` lists the received databases where the member may propose changes |
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
the specific current definition and the record rules for each proposal. On a
whole-database share, a receiver with a `social/Comment` grant can propose
comments and edits to comments they created. The publisher signs each accepted
change, or `not_accepted`. The author is `created_by`
on the `POST /v1/read` reply: the lowercase hex node key of the writer.
LastDB sets it when the comment is created and keeps it on a replace.
`updated_by` is the node key of the writer of this revision. A proposal that
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

Only the publisher's signature puts a record into a shared database, with one
exception: a member with role `write` whose node holds a covering write lease
signs its own change at once (see Org and write leases). Every other member
change is a proposal. An active member with role `write` or `submit` can propose
a change. The receiver posts
`POST /v1/x/databases` with `"action": "submit"`, the database id, the
mutation document, and `idempotency_key` (one non-nil lowercase UUID). The
call answers 202 with `ok`, `status: submitted`, `mutation_id`, and
`submission_sequence`. It does not write a record on the receiver, and the
local copy stays as it was until the publisher accepts the proposal and the
receiver pulls it. An owned
database or an unknown id answers 404. A share that is not a whole database
answers 403. A member whose role is `read` is refused: HTTP 403, code
`permission_denied`, message `this member cannot submit this shared mutation`,
and no `Retry-After`. Do not retry. The publisher does not receive that
proposal. Only the publisher's LastDB can read pending changes. It
incorporates accepted proposals onto the signed history. Receivers follow
that log and can read the resulting record after it syncs, with
`POST /v1/read` and the `database` field. An app that wants to show the
sender's own change before the publisher accepts it uses
`"action": "submission_status"` and shows that pending state. The status is
`submitted` (the publisher has not decided), `published` (accepted),
`not_accepted` (the publisher said no, and the answer is final) or `unknown`
(this node never submitted that id). The publisher accepts any create,
replace or delete from a `write` member. It accepts a create from a `submit`
member, and a replace or delete only of a record that member created. The
`expected` check still applies. When two members create the same key, the
first proposal the publisher processes is `published` and the other is
`not_accepted`. An accepted change carries the proposing member's key as
`created_by` and `updated_by`.
`POST /v1/write` to a received database is refused for every member, whatever
its role, except a `write` member under a covering lease: HTTP 403, `a received
database is read only`.
[How sharing works](https://github.com/EdgeVector/lastdb/blob/main/docs/designs/cloud-api.md)

People turns Share, Deliver and Org into publisher-signed grant and revoke
chain entries through LastDB. People may carry signed heads for comparison
only and bootstraps the first relay URL from a publisher-signed `relays`
entry. A grant that exists only in People is never authoritative.
[Relay protocol](https://github.com/EdgeVector/lastdb/blob/main/docs/designs/relay-protocol.md)

## Org and write leases

People has three presets of the signed roles. Deliver is `read` for anyone (`grant#any`).
Share is `submit`: members propose and the publisher decides. Org is `write`:
members change records directly while a write lease covers them.

A lease is one publisher-signed entry for the whole database: "everyone who is a
writer as of this entry may write until this time". The publisher's node signs a
new one at half life, and only for a database that has a lease length. Org is the
lease length. When a grant, a circle link or a request leaves a database with a
`write` member and no lease length, People sets
`ORG_LEASE_MS` in `src/org.py`, 3 days. A length the publisher already set,
for example with `lastdb share lease DB --length 24h`, stays. When the last writer
leaves (reduced to `submit` or `read`, or revoked), People clears the length. A
lease that is already signed runs to its end, so a person who left Org can write
directly until then. `grant set` and `grant revoke` say so when they preview or
apply such a change. A signed lease has no early end. A short length bounds how
long a removed writer keeps writing, and a long one lets the publisher stay away.
People does not offer another place to change the length.

`people db show DB` prints `writeAccess`. For a member it is "You can write
directly until <time>." while a lease covers the node, and "Your changes are
proposals. The publisher decides each one." otherwise. For the publisher it is the
lease length and when the current lease ends. A database with no lease length has no
`writeAccess` for the publisher.

People's commands do not change records of a received database. An app that does
calls the LastDB app SDK's `changeRecord(schema, op)` (LastDB 0.20.0 or later; the
`change` action of the `databases` extension). The node answers `applied` (the record is
in the member's database now) or `pending` (a proposal, `submissionSequence` set). The app
does not decide which. A reader is refused, and a change that carries a file is
refused. `pick-database --intent write` lists a received database for any
`submit` or `write` member, because the node decides how each change goes. An app shows an
`applied` change at once and a `pending` one as a proposal.

## What members can change

There is no database-wide write map. Access is the per-member role on the
publisher-signed grant chain: `read`, `submit`, `write`, or `revoked`. People
reads that role from the grant (and from `selfAccess` once LastDB folds
`GrantState`). Deliver uses `grant#any`/`read`. Org uses `write` and a lease. Share uses
`submit` for proposals. A member with role `read` cannot submit. A circle
link or a preset still chooses `read` or `submit` (or `write` for Org). A
`write` member proposes changes to any record. A `submit` member proposes
changes to its own records. The publisher decides each proposal.
People invites are hand-off consent codes. LastDB authorizes access from
signed grant entries on the chain; there is no ExeMem invite or share-config
path.

## Choosing a database for an app

`people pick-database --spec whole-database --version 1 --intent read` lists
the databases that match. Add `--database ID` to check one selection. The
command returns that database's id, name, and spec. The app names that id in
the `database` field of each `POST /v1/read` and `POST /v1/write` item. A
`/v1/write` to a received database is refused, except for a `write` member under a
lease. An app changes a received whole-database share with `changeRecord`
(see Org and write leases), which applies it or queues the proposal described in
How sharing works. LastDB
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
  that public SHA (default on dispatch: the #1839 squash on main (members propose, the
  publisher accepts or signs not accepted)
  `ea3d7f72b2b5a6d7966552978e7507420b291b9f`). Set `relay_url` to the ExeMem
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

`people-org` needs a LastDB build with write leases and `changeRecord` (the app SDK 0.20.0).

A red run on `main` from a schedule or a push opens or updates one GitHub
issue in this repository. The issue is labeled `scheduled-failure`, assigned
to `shiba4life`, and titled `Scheduled failure: DEV sharing`. A later
schedule or push run whose sharing jobs succeed closes it. An hourly poll
that skips sharing does not close it. A manual dispatch does not open or
close it.

`people-direct` starts four fresh LastDB nodes and `people-org` three. The
other modes start two, a publisher and a recipient. The nodes come from the
newest published LastDB nightly. Every node has a fresh identity made offline, and the
publisher enrolls its own with the DEV cloud (`lastdb backup connect --dev`). No
secret or shared account is used, so runs and legs overlap.
The recipe drives this commit's `people` command through the launcher, on
the app socket, with `people` registered:

| Mode | What it proves |
| --- | --- |
| `people-share` | Save a contact, set up `shared-albums`, grant, invite, accept, read, revoke and restore on a whole-database share, with two scoped content clients; after accept, a `social/Photo` with a full `file` and an `inline_file` thumbnail reaches the recipient byte-identical (needs LastDB shared file sync / API v48+) |
| `people-circles` | Circles grant submit from the chain role, Comment proposals, app sharing requests, grants changed outside People, direct grant, revoke and restore |
| `people-prepare` | The manifest and schemas produce a valid unsigned registry declaration for this commit on a fresh node |
| `people-link` | Deliver via `grant#any` plus a People consent-code hand-off; LastDB refuses revoke of `any` |
| `people-direct` | Share `submit` members propose, the publisher accepts them with the member as author, two members that propose the same key end with one `published` and one `not_accepted`, a reader is refused on the submissions route, `/v1/write` to a received database is refused with 403, and a member can change only its own record |
| `people-org` | Org on a database with no lease length sets the 3 day default and revoking its last writer clears it. With a 60 second lease a write member's `changeRecord` answers `applied`, is on its node at once and reaches the publisher and a reader with no accept step; a reader is refused; People shows the writer its end time and the publisher the length and expiry; after the writer is reduced to `submit` (the publisher leaves Org) the length clears, the next change answers `pending` and is published after the publisher accepts it, and no new lease is signed |

Where a recipe is the node owner, it keeps the owner socket. That is schema
placement, roster reads and writes, share settings, a snapshot, an open
link, a member's proposal and its decision, the refused direct write, cloud backup, and node
health. People's own commands do not use those routes.

The recipes are in `ci/`, and the node helpers they use are in `ci/lib/`.
Two things come from `EdgeVector/lastdb`: the endpoint profile
`lastdb_node/environments.json` and the LastDB app SDK's data client, which
`ci/people_sdk.mjs` uses to read shared records. Both are taken at the
nightly's exact source commit. The People client the recipes exercise is this
repo's `client/`.
