# People client for JavaScript apps

`@lastdb/people-client` lets a JavaScript or TypeScript app use People. It is a
thin wrapper: each method runs the installed `people` command, reads the JSON
it prints, checks its shape, and returns it. It has no dependencies and no
build step.

It ships inside People's source, so it always matches the People it sits next
to. With People installed from the app registry, the package is the `client`
folder of the installed source: `<apps root>/people/current/source/client`.
Import it from there, or copy the folder into your app.

People manages verified contacts, reusable circles, and consent-code invites. For
schemas, records, and files, use `connect` and `select` from `@lastdb/app-sdk`.
Point both at the same LastDB home. The publisher needs a connected ExeMem
account.

| Method | Command it runs |
| --- | --- |
| `listCircles()` | `people circle list` |
| `requestSharing(request)` | `people request submit`, with the request on standard input |
| `getSharingStatus(requestId)` | `people request status ID` |
| `createSharedDatabase(name)` | `people db create NAME` |
| `setupDatabase(request)` | `people db create --setup ...` or `people db verify ...` |
| `sealInvitation(databaseId, nodeKey)` | `people invite DATABASE PERSON` (consent code) |
| `acceptInvitation(consent, publisherKey, name?)` | `people db accept`, with the consent code on standard input |
| `revokeAccess(database, nodeKey)` | `people grant revoke --apply DATABASE PERSON` |
| `inviteReader(database, recipient)` | `people invite --name ... DATABASE PERSON` |

A failed command throws `PeopleError` with `code: 'command_failed'` and
People's own `people: ...` line as the message. Output of an unexpected shape
throws `PeopleError` with `code: 'invalid_response'`. A database result is the
row People prints, which is LastDB's own database row; `id` is a Hex32
(64 lowercase hex), never a UUID. Sharing-request ids remain UUIDs.

## Share an app's own database

Create an empty named whole-database share when your app owns the schemas.
Save the stable database ID. Place each schema with the `declare_schema`
action of `POST /v1/x/databases`. The schema name is `{app id}/{Name}`.
The app declares the `databases` extension. Name the same database id in the
`database` field of each LastDB read and write.
LastDB limits the app by the schemas it declared, not by database. For
standard album and comment schemas, use the setup below.

```ts
import { join } from 'node:path';
import { connect } from '@lastdb/app-sdk';
import { peopleClient } from '@lastdb/people-client';

const people = peopleClient({ dataDir: publisherHome });
const root = await connect({
  appId: 'photo-journal',
  socketPath: join(publisherHome, 'data', 'folddb.sock'),
});
const database = await people.createSharedDatabase('family-trips');
const trips = root.select(database.id);
await trips.declareSchema(tripSchema);
```

The owner runs People on the publisher's device to add verified contacts and
link a circle to the database, for example
`people circle set Family --members Ana --link family-trips=read`. The command
prints the resulting access. Adding `--apply` makes the change. Use
`people db show ID` to see the database's roster. After People confirms the
grant, the app can get a sealed receipt with
`await people.sealInvitation(database.id, anaVerifiedKey)`. The caller delivers
the receipt to Ana. Keep sealed receipts out of logs.

On Ana's device, use a People client pointed at her home. Pass the publisher
key verified independently of the receipt, and save the returned database ID.

```ts
const recipientPeople = peopleClient({ dataDir: recipientHome });
const received = await recipientPeople.acceptInvitation(receipt, verifiedPublisherKey, 'family-trips');
const recipient = await connect({
  appId: 'photo-journal',
  socketPath: join(recipientHome, 'data', 'folddb.sock'),
});

// Wait for this ID to be ready in listDatabases() before reading its content.
const [page] = await recipient.read([{
  database: received.id,
  schema: 'photo-journal/Trip',
  key: { hash: tripKey },
}]);
const trip = page.records?.[0];
```

A change to that received database is not a write. `@lastdb/app-sdk`'s
`submitProposal` posts `POST /v1/x/databases` with `"action": "submit"`.
The proposal id is `idempotency_key`. The call does not write a record
locally. Show the change as pending from `getShareSubmissionStatus` until
the publisher decides. The state is `submitted`, then `published` (accepted)
or `not_accepted` (final). `POST /v1/write` to a received database is refused
for every member, whatever its role.

Use the same id in the `database` field when another app reads this share on
Ana's device. The owner can manage access in People or revoke a recipient with
`await people.revokeAccess(database.id, anaVerifiedKey)`.

## Propose circle sharing

Choose a circle from `await people.listCircles()` and submit a declaration.
Use one stable UUID for the request and its retries.

```ts
import { randomUUID } from 'node:crypto';

const request = await people.requestSharing({
  requestId: randomUUID(),
  appId: 'photo-journal',
  change: {
    operation: 'setCircleBinding',
    databaseId: database.id,
    circleId: family.id,
    preset: 'commenter',
  },
});
```

The owner reviews the proposal with `people request show <requestId>` and
decides with `people request approve <requestId>` or
`people request decline <requestId>`, using the same LastDB home. The app can
fetch the result with `people.getSharingStatus(request.requestId)`. Status is
`awaitingApproval`, `pending`, `needsReview`, `confirmed`, or `declined`.
Retrying the same request UUID returns that outcome. When confirmed, use
`sealInvitation` for a new recipient. Use `writeSchemas` in place of a preset
to suggest the app's canonical schema names. A pending grant update blocks
further changes to its database. Distinct databases can proceed with their own
owner review.

The row `acceptInvitation` returns, and `listDatabases` in the app SDK, expose
the receiving node's `selfAccess` when available. After LastDB #1805 that object
is `{ role: "read" | "submit" | "write" }` (no `state` / `stale`). A revoked
key omits `selfAccess`. Use `role` to guide which proposals the app offers.
Every change from a member is a proposal. A `write` member may propose any
change. A `submit` member may create records and change its own. The
publisher decides each proposal and may sign `not_accepted`.

## Use a named database setup

People can provision **shared-albums** version 1 with the standard
`social/Album`, `social/AlbumItem`, `social/Photo`, and `social/Comment`
schemas. The owner chooses a local name for provisioning. Other apps open the
selected database by its stable ID and get the verified definitions. Record
reads and writes then name that id in `database`.

```ts
const created = await people.setupDatabase({
  setupName: 'shared-albums', requiredVersion: 1, localName: 'family',
});
const verified = await people.setupDatabase({
  setupName: 'shared-albums', requiredVersion: 1, databaseId: created.databaseId,
});
const albums = root.select(verified.databaseId);
albums.rememberDefinition('social/Album', verified.definitions['social/Album']!);
```

Access comes from publisher-signed grant entries on the LastDB chain. The
member role is `read`, `submit`, or `write` (plus `revoked`). There is no
database-wide write map.

Verification by ID also works on a received share once it has synced its
schemas. Setup version 1 requires the catalog's schema shapes and leaves
schema upgrades to an explicit owner operation. Use the same circle and
invitation flow as for an app-owned share.

A LastDB build with retryable whole-database creation resumes interrupted
provisioning by the original local name. An owner can choose an existing
incomplete setup by its ID and pass `finishExisting: true` to install missing
schemas after checking compatibility.

Grant access with a circle or `people grant set` before sealing. Then
`people.inviteReader(database, { name, nodeKey, contactPath })` or
`people.sealInvitation(databaseId, nodeKey)` returns a People consent code.
People reuses verified contacts, so later consent codes can omit `contactPath`.
