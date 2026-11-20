/** Use the same LastDB home for People and the content client. */
export interface PeopleClientOptions {
  dataDir: string;
  /** Installed People command or explicit executable path. Defaults to `people`. */
  peoplePath?: string;
  /** LastDB executable to use when it is outside the caller's PATH. */
  lastdbPath?: string;
}

/** A failed People command or output of the wrong shape. */
export class PeopleError extends Error {
  constructor(code: PeopleError['code'], message: string);
  code: 'command_failed' | 'invalid_response';
}

/**
 * The database row People prints: LastDB's own row, returned as given.
 * Name `id` as `database` on each LastDB read and write item. LastDB limits
 * the app by the schemas it declared, not by database.
 */
export interface PeopleDatabase {
  /** Stable database id for the `database` field of `POST /v1/read` and `POST /v1/write`. */
  id: string;
  name?: string | null;
  kind?: string | null;
  sharingMode?: 'whole_database';
  spec?: { name: string; version: number } | null;
  state?: string;
  [field: string]: unknown;
}

/** A sealed invitation for the caller to hand to the verified recipient. */
export interface PeopleInvitation {
  databaseId: string;
  /** Tip of the publisher grant chain when the invitation was sealed. */
  entryHash: string;
  writerSeq?: number;
  publisher: string;
  invitation: string;
}

/** Provision by a new local name or verify an explicitly selected database. */
export type PeopleSetupRequest = {
  setupName: string;
  requiredVersion?: number;
} & (
  { localName: string; databaseId?: never; finishExisting?: never }
  | { databaseId: string; localName?: never; finishExisting?: true }
);

export interface PeopleDatabaseSetup {
  databaseId: string;
  name: string | null;
  setupName: string;
  version: number;
  /** Verified installed definition IDs keyed by canonical schema name. */
  definitions: Record<string, string>;
}

type PeopleGrantScope =
  { writeSchemas: string[] | 'all' | 'write'; preset?: never }
  | { preset: 'viewer' | 'commenter' | 'album-organizer'; writeSchemas?: never };

export type PeopleSharingChange =
  | ({ operation: 'setCircleBinding'; databaseId: string; circleId: string } & PeopleGrantScope)
  | { operation: 'removeCircleBinding'; databaseId: string; circleId: string }
  | ({ operation: 'setPersonGrant' | 'restorePerson'; databaseId: string; nodeKey: string } & PeopleGrantScope)
  | { operation: 'revokePerson'; databaseId: string; nodeKey: string };

export interface PeopleSharingRequest {
  requestId: string;
  /** Display label for the app that proposed the change. */
  appId: string;
  change: PeopleSharingChange;
}

export interface PeopleSharingStatus {
  requestId: string;
  databaseId: string;
  status: 'awaitingApproval' | 'pending' | 'confirmed' | 'needsReview' | 'declined';
  entryHash?: string;
  writerSeq?: number;
  error?: string;
}

export interface PeopleClient {
  /** List the owner's reusable circle labels and stable IDs. */
  listCircles(): Promise<{ id: string; name: string }[]>;
  /** Save a declaration for the owner to approve with `people request approve`. Retry with the same UUID and change. */
  requestSharing(request: PeopleSharingRequest): Promise<PeopleSharingStatus>;
  /** Read the outcome using the UUID assigned when the app proposed the change. */
  getSharingStatus(requestId: string): Promise<PeopleSharingStatus>;
  /** Create an empty named whole-database share for an app's own schemas. */
  createSharedDatabase(name: string): Promise<PeopleDatabase>;
  /** Open a versioned database setup using People's catalog. */
  setupDatabase(request: PeopleSetupRequest): Promise<PeopleDatabaseSetup>;
  /** Seal an invitation. Grant access with a circle or `grant set` first. Omit contactPath when People already remembers the recipient. */
  inviteReader(database: string, recipient: {
    name: string;
    nodeKey: string;
    contactPath?: string;
  }): Promise<PeopleInvitation>;
  /** Seal an invitation for a verified contact with an existing whole-database grant. */
  sealInvitation(databaseId: string, nodeKey: string): Promise<PeopleInvitation>;
  /** Accept using the independently verified publisher key. */
  acceptInvitation(invitation: PeopleInvitation, publisherKey: string, name?: string): Promise<PeopleDatabase>;
  /** Revoke a person's grant and return the signed chain entry hash. */
  revokeAccess(database: string, nodeKey: string): Promise<string>;
}

/** Share contacts and databases through People. Use `connect` from `@lastdb/app-sdk` for content. */
export function peopleClient(options: PeopleClientOptions): PeopleClient;
