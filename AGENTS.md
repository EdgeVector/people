# People

People is the command-line address book and sharing settings for LastDB. It
moved out of `EdgeVector/lastdb` on 2026-11-16.

- `src/people` is the entrypoint, the file with no extension. `src/people.py`
  is the contacts module. Python imports the `.py` file. `src/cli.py` holds
  the command grammar, and `src/commands.py` maps each command to an
  operation. Database and sharing calls use `POST /v1/x/databases`. Record
  calls use `POST /v1/read` and `POST /v1/write`.
- `ci/` holds the DEV sharing recipes run by `dev-sharing.yml`.
- `lastdb-app.json` is the app manifest and the source of People's private
  schema definitions. Each schema object uses a local name such as `Person`.
  The installer stores that name as `people/<Name>` from the manifest
  `app_id`. People does not declare those schemas at run time. The `access`
  block lists the nine full names for read and for write.
- `setups/shared-albums-v1/` holds the schema definitions of the
  `shared-albums` version 1 setup. This is the only copy of the
  definitions. `EdgeVector/lastdb` keeps just the field names of the
  `social` version 1 spec in `database_policy/src/lib.rs`, which decides
  what a filtered share admits. A field added or renamed here needs the
  same change there before a filtered share accepts it.

## Rules

- No tests. Pull requests get syntax, manifest and `--help` checks. The
  sharing flow is proven on main by `.github/workflows/dev-sharing.yml`
  (`people-share`, `people-circles`, `people-scale`, `people-prepare`). It runs on every
  push to main, daily at 09:23 UTC after the LastDB nightly, and by manual
  dispatch, never on a pull request. The recipes
  are in `ci/`. They start two fresh LastDB nodes from a published LastDB
  nightly, connect them to the DEV cloud, and drive this commit's `people`
  command through the launcher on the app socket. A step that sets up the
  node keeps the owner socket. A change that alters command output or behaviour must update
  the recipes in the same PR.
- A registry listing is tied to one exact LastDB API version. `people-prepare`
  fails when the DEV `next` list has no People entry for the nightly's API
  version: with `people_dev_listing_mismatch` when the list command falls back
  to an entry for an older API version, and with
  `people_dev_listing_missing_for_api` when it finds none. The fix is to
  republish, not to change the recipe. `ci/publish_dev.py` does that by hand
  from a fresh node; its docstring has the command. It is not part of the
  workflow, and CI holds no publisher key.
- `ci/lib/` holds the node helpers the recipes need (`support`, `node`,
  `harness`, `node_auth`, the BIP39 word list). They were carried from
  LastDB's scripts and belong to this repo now: they use only the LastDB
  binaries' CLI and socket, so fix them here when a LastDB release changes
  that behaviour. Two things still come from `EdgeVector/lastdb`:
  `lastdb_node/environments.json` (the endpoint profile of the build under
  test) and the app SDK's data client, which `ci/people_sdk.mjs` uses to
  read shared records. Both are taken at the nightly's source commit.
- `client/` is `@lastdb/people-client`, the JavaScript wrapper around the
  `people` command: plain ESM, no build step, no dependencies, types in
  `index.d.ts`. It replaced the adapter that lived in
  `@lastdb/app-sdk`. When a command's name, flags or JSON output change,
  change `client/` in the same commit; `ci/people_sdk.mjs` exercises it.
  Keep `client/package.json`'s version equal to `lastdb-app.json`'s.
- Preserve whole-database sharing: two logs, the signed per-database roster,
  schema write grants, and the publisher approving each mutation. Read
  [the sharing design](https://github.com/EdgeVector/lastdb/blob/main/docs/designs/shared-database-mutation-logs.md#requirements-to-preserve)
  before changing that path.
- LastDB access is by key and batched. No scan, no loop of one call per record.
- No migration code and no readers for old formats.
- Source file: 400 lines. Function: 100 lines.
- No browser interface. Every owner action is a command.
