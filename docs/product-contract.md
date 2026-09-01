# Product contract

Agora lets internal users publish versioned HTML dashboard packages without allowing uploaded
content to become part of the trusted application.

## Actors

- An administrator provisions, disables, enables, and resets users.
- An owner manages projects they currently own, uploads revisions, grants access, and previews
  their content.
- A viewer discovers and opens only projects for which they have an active grant and a published
  revision.
- Authentication alone does not grant access to project content.

Administrator status does not bypass project ownership or viewer grants.

## Projects and revisions

A project is the authorization boundary. Its name is visible only to users currently allowed to
discover it. External lookup failures must not reveal whether an unrelated project exists.

Each revision contains exactly one HTML entry point and up to 50 flat supporting files. Supported
files are HTML, CSV, CSS, PNG, JPEG, GIF, WebP, WOFF, and WOFF2. Files are validated for names,
types, counts, and size limits before the revision becomes visible.

Accepted revisions and their files are immutable. A change creates a new revision; it never
replaces bytes in an existing one. Publication pins one exact revision.

## Access

Owners may preview their own revisions. Viewers may open only the currently published revision
of a project with an active grant. Every content request rechecks the user, project, revision,
publication state, grant or ownership, credential scope, and expiry.

Revoking access, disabling a user, unpublishing a project, or transferring ownership takes effect
at the next server-side authorization check. Bytes already delivered to a browser cannot be
recalled.

A viewer authorized for a published revision may receive every file in that revision. Agora does
not promise row-level or column-level filtering, data masking, or download prevention.

## Identity and audit

SOEIDs are canonicalized before lookup. There is no self-registration. Password changes,
disablement, grant changes, ownership changes, uploads, publication actions, and successful
viewer opens must preserve the audit facts required by the domain model.

Disabling an owner removes that person's access but does not silently unpublish content for other
authorized viewers. Ownership changes must use the atomic transfer service and preserve immutable
historical attribution.

## Explicit limits

Agora does not currently promise public links, groups, editors, co-owners, self-registration,
enterprise SSO, multiple HTML entry points, mutable revisions, external asset allowlists,
scheduled publishing, malware scanning, or protection against every form of hostile browser CPU
exhaustion.
