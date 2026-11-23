import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

import { peopleClient } from '../client/index.mjs';

const [action, peoplePath, , dataDir, , lastdbPath] = process.argv.slice(2);
const people = peopleClient({ dataDir, peoplePath, lastdbPath });
const input = JSON.parse(readFileSync(0, 'utf8'));

// Content reads use the LastDB app SDK built from the source of the build under test.
async function connect(options) {
  const sdk = await import(pathToFileURL(join(process.env.LASTDB_APP_SDK, 'dist', 'index.js')).href);
  return sdk.connect(options);
}

try {
  let result;
  switch (action) {
    case 'create':
    case 'create-shared':
      result = await people.createSharedDatabase(input.name);
      break;
    case 'setup':
      result = await people.setupDatabase(input);
      break;
    case 'circles':
      result = await people.listCircles();
      break;
    case 'request-sharing':
      result = await people.requestSharing(input);
      break;
    case 'sharing-status':
      result = await people.getSharingStatus(input.requestId);
      break;
    case 'invite':
      result = await people.inviteReader(input.database, input.recipient);
      break;
    case 'seal':
      result = await people.sealInvitation(input.database, input.nodeKey);
      break;
    case 'accept':
      result = await people.acceptInvitation(input.receipt, input.publisher, input.name);
      break;
    case 'revoke':
      result = await people.revokeAccess(input.database, input.nodeKey);
      break;
    case 'read': {
      const root = await connect({ appId: input.appId ?? 'people-dev-sdk', socketPath: join(dataDir, 'data', 'folddb.sock') });
      const [page] = await root.read([{
        database: input.database,
        schema: input.schema,
        key: { hash: input.key },
      }]);
      if (!page || page.error || !Array.isArray(page.records) || page.records.length !== 1) {
        throw new Error('read failed');
      }
      const row = page.records[0];
      result = { fields: row.record, revision: row.revision, key: row.key };
      break;
    }
    case 'list-databases': {
      const root = await connect({ appId: 'people-dev-sdk', socketPath: join(dataDir, 'data', 'folddb.sock') });
      result = await root.listDatabases();
      break;
    }
    case 'change': {
      // The app asks for one change and the node answers applied or pending. A member that
      // may neither write nor propose is refused; any other failure stays a failure.
      const root = await connect({ appId: 'people-dev-sdk', socketPath: join(dataDir, 'data', 'folddb.sock') });
      try {
        result = await root.select(input.database).changeRecord(input.schema, input.op);
      } catch (error) {
        if (!error || error.name !== 'PermissionDeniedError') throw error;
        result = { refused: error.name };
      }
      break;
    }
    case 'submission-status': {
      const root = await connect({ appId: 'people-dev-sdk', socketPath: join(dataDir, 'data', 'folddb.sock') });
      const client = root.select(input.database);
      result = await Promise.all(input.ids.map(id => client.getShareSubmissionStatus(id)));
      break;
    }
    default:
      throw new Error('Unknown SDK operation');
  }
  process.stdout.write(JSON.stringify(result));
} catch (error) {
  const detail = error && typeof error.message === 'string' ? error.message : 'sdk operation failed';
  process.stderr.write(`${detail}\n`);
  process.exitCode = 1;
}
