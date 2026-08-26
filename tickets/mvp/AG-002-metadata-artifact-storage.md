# AG-002 — Implement metadata persistence and private artifact storage

- Milestone: MVP
- Epic: Persistence
- Priority: P0
- Size: L
- Depends on: AG-001

## Goal

Create the durable model and storage boundary for users, dashboards, immutable revisions, HTML, CSV attachments, viewer grants, publication, and audit events.

## Scope

- Model users keyed by canonical SOEID and an immutable internal identifier.
- Model dashboards with one owner, stable identifier, lifecycle state, and published revision pointer.
- Model immutable revisions and HTML/CSV artifact metadata including logical name, private storage key, size, media type, and digest.
- Model viewer grants and append-only audit events with database constraints and indexes.
- Implement a private MVP filesystem adapter behind an interface that can later support object storage.
- Generate storage keys independently of user filenames; use atomic writes and cleanup for incomplete operations.

## Acceptance criteria

- [ ] Schema constraints prevent duplicate SOEIDs, artifact names, grants, invalid ownership, and foreign published revisions.
- [ ] Complete revisions and their artifacts cannot be mutated.
- [ ] Artifacts live outside the public web root and cannot be fetched through a raw storage URL.
- [ ] User filenames cannot control directories or storage keys.
- [ ] Writes are atomic, digest-verified, and leave no completed orphan after failure.
- [ ] Delete/cleanup behavior is idempotent and cannot escape the configured storage root.
- [ ] Migrations work from an empty database and tests cover collisions, traversal, Unicode names, and partial writes.

## Out of scope

- Cloud object storage, CDN, quotas, and automated retention.
