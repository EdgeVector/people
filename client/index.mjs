// People client for JavaScript apps: a thin wrapper that runs the installed
// `people` command and checks the JSON it prints. It has no dependencies and
// no build step. It ships with People, so it matches People's commands.

import { execFile } from 'node:child_process';

/** A failed People command (`command_failed`) or output of the wrong shape (`invalid_response`). */
export class PeopleError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'PeopleError';
    this.code = code;
  }
}

const invalid = (message) => new PeopleError('invalid_response', message);
const isObject = (value) => typeof value === 'object' && value !== null && !Array.isArray(value);
const STATUSES = ['awaitingApproval', 'pending', 'needsReview', 'confirmed', 'declined'];
/** Database and stream/device ids are Hex32 (64 lowercase hex), not UUID. */
const HEX32 = /^[0-9a-f]{64}$/;
const isHex32 = (value) => typeof value === 'string' && HEX32.test(value);

/** Share contacts and databases through People. Use `connect` from `@lastdb/app-sdk` for content. */
export function peopleClient({ dataDir, peoplePath = 'people', lastdbPath }) {
  const baseArgs = ['--data-dir', dataDir];
  if (lastdbPath !== undefined) baseArgs.push('--lastdb', lastdbPath);

  function run(command, args, input) {
    const action = command.join(' ');
    return new Promise((resolve, reject) => {
      const child = execFile(peoplePath, [...baseArgs, ...command, ...args], {
        timeout: 180_000,
        maxBuffer: 1024 * 1024,
      }, (error, stdout, stderr) => {
        if (error) {
          const detail = stderr.split('\n').find((line) => line.startsWith('people: '));
          reject(new PeopleError('command_failed',
            detail ?? `People ${action} failed (${error.code ?? 'interrupted'})`));
          return;
        }
        try {
          resolve(JSON.parse(stdout));
        } catch {
          reject(invalid(`People ${action} returned invalid JSON`));
        }
      });
      // A child can reject its arguments and close stdin before reading.
      child.stdin?.on('error', (error) => {
        if (error.code !== 'EPIPE') {
          reject(new PeopleError('command_failed', `Could not send input to People ${action}`));
        }
      });
      child.stdin?.end(input === undefined ? undefined : JSON.stringify(input));
    });
  }

  return {
    /** List the owner's reusable circle labels and stable IDs. */
    async listCircles() {
      const value = await run(['circle', 'list'], []);
      if (!Array.isArray(value) || !value.every((item) =>
        isObject(item) && typeof item.id === 'string' && typeof item.name === 'string')) {
        throw invalid('People returned an invalid circle list');
      }
      return value.map(({ id, name }) => ({ id, name }));
    },

    /** Save a declaration for the owner to approve with `people request approve`. Retry with the same UUID and change. */
    async requestSharing(request) {
      const receipt = parseSharingStatus(await run(['request', 'submit'], [], request), request.requestId);
      if (receipt.databaseId !== request.change.databaseId) {
        throw invalid('People returned sharing status for a different database');
      }
      return receipt;
    },

    /** Read the outcome using the UUID assigned when the app proposed the change. */
    async getSharingStatus(requestId) {
      return parseSharingStatus(await run(['request', 'status'], ['--', requestId]), requestId);
    },

    /** Create an empty named whole-database share for an app's own schemas. */
    async createSharedDatabase(name) {
      const database = parseDatabase(await run(['db', 'create'], ['--', name]));
      if (database.sharingMode !== 'whole_database') {
        throw invalid('People did not create a whole-database share');
      }
      return database;
    },

    /** Open a versioned database setup using People's catalog. */
    async setupDatabase(request) {
      const args = [`--setup=${request.setupName}`, `--version=${request.requiredVersion ?? 1}`];
      if (request.localName !== undefined) {
        return parseSetup(await run(['db', 'create'], [...args, '--', request.localName]), request);
      }
      if (request.finishExisting) args.push('--finish');
      return parseSetup(await run(['db', 'verify'], [...args, '--', request.databaseId]), request);
    },

    /** Mint a hand-off consent code. Grant access with a circle or `grant set` first. Omit contactPath when People already remembers the recipient. */
    async inviteReader(database, recipient) {
      const args = [`--name=${recipient.name}`];
      if (recipient.contactPath !== undefined) args.push(`--contact=${recipient.contactPath}`);
      return parseConsent(await run(['invite'], [...args, '--', database, recipient.nodeKey]));
    },

    /** Mint a consent code for a verified contact with an existing whole-database grant. */
    async sealInvitation(databaseId, nodeKey) {
      const result = parseConsent(await run(['invite'], ['--', databaseId, nodeKey]));
      if (result.databaseId !== databaseId) {
        throw invalid('People returned a consent code for a different database');
      }
      return result;
    },

    /** Accept a consent code using the independently verified publisher key. */
    async acceptInvitation(invitation, publisherKey, name) {
      const args = [`--publisher=${publisherKey}`];
      if (name !== undefined) args.push(`--name=${name}`);
      const code = typeof invitation === 'string' ? invitation : invitation.consent;
      const expectedId = typeof invitation === 'object' && invitation ? invitation.databaseId : undefined;
      const database = parseDatabase(await run(['db', 'accept'], args, code));
      if (expectedId !== undefined && database.id !== expectedId) {
        throw invalid('People accepted a different database');
      }
      return database;
    },

    /** Revoke a person's grant and return the signed chain entry hash. */
    async revokeAccess(database, nodeKey) {
      const result = await run(['grant', 'revoke'], ['--apply', '--', database, nodeKey]);
      if (!isObject(result) || typeof result.entryHash !== 'string' || result.state !== 'revoked'
        || result.nodeKey !== nodeKey || !isHex32(result.databaseId)) {
        throw invalid('People returned an invalid revoke receipt');
      }
      return result.entryHash;
    },
  };
}

/** The database row People prints. It is LastDB's own row, returned as given. */
function parseDatabase(value) {
  if (isObject(value) && isHex32(value.id)) return value;
  throw invalid('People returned an invalid database');
}

function parseSharingStatus(value, expectedId) {
  if (isObject(value)) {
    const { requestId, databaseId, status, error } = value;
    // Policy-only confirms (empty circle, no grant yet) have no chain tip.
    // Null tip fields are omitted the same way as missing ones.
    const entryHash = typeof value.entryHash === 'string' ? value.entryHash : undefined;
    const writerSeq = typeof value.writerSeq === 'number' ? value.writerSeq : undefined;
    if (requestId === expectedId && isHex32(databaseId) && STATUSES.includes(status)
      && (error === undefined || typeof error === 'string')) {
      return {
        requestId, databaseId, status,
        ...(entryHash === undefined ? {} : { entryHash }),
        ...(writerSeq === undefined ? {} : { writerSeq }),
        ...(error === undefined ? {} : { error }),
      };
    }
  }
  let raw;
  try {
    raw = JSON.stringify(value);
  } catch {
    raw = String(value);
  }
  throw invalid(`People returned invalid sharing status: ${raw}`);
}

function parseConsent(result) {
  if (isObject(result)) {
    const { databaseId, entryHash, writerSeq, publisher, consent } = result;
    if (isHex32(databaseId) && typeof entryHash === 'string'
      && typeof publisher === 'string' && typeof consent === 'string'
      && consent.startsWith('people-consent-v1.')
      && (writerSeq === undefined || typeof writerSeq === 'number')) {
      return {
        databaseId, entryHash, publisher, consent,
        ...(writerSeq === undefined ? {} : { writerSeq }),
      };
    }
  }
  throw invalid('People returned an invalid consent receipt');
}

function parseSetup(result, request) {
  if (isObject(result)) {
    const { databaseId, name, setupName, version, definitions } = result;
    if (isHex32(databaseId)
      && (request.databaseId === undefined || databaseId === request.databaseId)
      && (name === null || typeof name === 'string')
      && setupName === request.setupName && version === (request.requiredVersion ?? 1)
      && isObject(definitions)) {
      const ids = Object.values(definitions);
      if (ids.length > 0 && ids.every(isHex32)) {
        return { databaseId, name, setupName, version, definitions };
      }
    }
  }
  throw invalid('People returned an invalid database setup');
}
