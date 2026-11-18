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

    /** Create a named database on the filtered `social` spec, version 1. */
    async createDatabase(name) {
      return parseDatabase(await run(['db', 'create'], ['--filtered', '--', name]));
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

    /** Omit contactPath when People already remembers the verified recipient. */
    async inviteReader(database, recipient) {
      const args = [`--name=${recipient.name}`];
      if (recipient.contactPath !== undefined) args.push(`--contact=${recipient.contactPath}`);
      return parseInvitation(await run(['invite'], [...args, '--', database, recipient.nodeKey]));
    },

    /** Seal an invitation for a verified contact with an existing whole-database grant. */
    async sealInvitation(databaseId, nodeKey) {
      const result = parseInvitation(await run(['invite'], ['--', databaseId, nodeKey]));
      if (result.databaseId !== databaseId) {
        throw invalid('People returned an invitation for a different database');
      }
      return result;
    },

    /** Accept using the independently verified publisher key. */
    async acceptInvitation(invitation, publisherKey, name) {
      const args = [`--publisher=${publisherKey}`];
      if (name !== undefined) args.push(`--name=${name}`);
      const database = parseDatabase(await run(['db', 'accept'], args, invitation));
      if (database.id !== invitation.databaseId) {
        throw invalid('People accepted a different database');
      }
      return database;
    },

    /** Revoke a person's grant and return the applied revision. */
    async revokeAccess(database, nodeKey) {
      const result = await run(['grant', 'revoke'], ['--apply', '--', database, nodeKey]);
      if (!isObject(result) || typeof result.revision !== 'string' || result.state !== 'revoked'
        || result.nodeKey !== nodeKey || typeof result.databaseId !== 'string') {
        throw invalid('People returned an invalid revoke receipt');
      }
      return result.revision;
    },
  };
}

/** The database row People prints. It is LastDB's own row, returned as given. */
function parseDatabase(value) {
  if (isObject(value) && typeof value.id === 'string' && value.id.length > 0) return value;
  throw invalid('People returned an invalid database');
}

function parseSharingStatus(value, expectedId) {
  if (isObject(value)) {
    const { requestId, databaseId, status, revision, error } = value;
    if (requestId === expectedId && typeof databaseId === 'string' && STATUSES.includes(status)
      && (revision === undefined || typeof revision === 'string')
      && (error === undefined || typeof error === 'string')
      && (status !== 'confirmed' || typeof revision === 'string')) {
      return { requestId, databaseId, status, ...(revision === undefined ? {} : { revision }),
        ...(error === undefined ? {} : { error }) };
    }
  }
  throw invalid('People returned invalid sharing status');
}

function parseInvitation(result) {
  if (isObject(result)) {
    const { databaseId, revision, publisher, invitation } = result;
    if (typeof databaseId === 'string' && typeof revision === 'string'
      && typeof publisher === 'string' && typeof invitation === 'string' && invitation.length > 0) {
      return { databaseId, revision, publisher, invitation };
    }
  }
  throw invalid('People returned an invalid invitation receipt');
}

function parseSetup(result, request) {
  if (isObject(result)) {
    const { databaseId, name, setupName, version, definitions } = result;
    if (typeof databaseId === 'string' && databaseId.length > 0
      && (request.databaseId === undefined || databaseId === request.databaseId)
      && (name === null || typeof name === 'string')
      && setupName === request.setupName && version === (request.requiredVersion ?? 1)
      && isObject(definitions)) {
      const ids = Object.values(definitions);
      if (ids.length > 0 && ids.every((id) => typeof id === 'string' && /^[0-9a-f]{64}$/.test(id))) {
        return { databaseId, name, setupName, version, definitions };
      }
    }
  }
  throw invalid('People returned an invalid database setup');
}
