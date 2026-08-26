# AG-006 — Implement HTML/CSV upload and immutable revisions

- Milestone: MVP
- Epic: Content lifecycle
- Priority: P0
- Size: XL
- Depends on: AG-002, AG-005

## Goal

Accept one self-contained HTML file with optional CSV attachments as an atomic, immutable dashboard revision.

## Scope

- Build an owner-only multipart upload flow with exactly one HTML file and a configurable number of CSV files.
- Validate extensions, declared and detected types, filename normalization and uniqueness, supported encoding, NUL/binary content, attachment count, per-file bytes, and total bytes.
- Stream uploads with application and edge limits instead of buffering unbounded content.
- Store unchanged validated bytes and create a complete revision only after all artifacts are durable.
- Order revisions deterministically under concurrency and clean up incomplete attempts.
- Show upload limits, progress, attachment summary, and file-specific validation errors.

## Acceptance criteria

- [ ] Zero or multiple CSV attachments work within configured limits; missing or multiple HTML files fail.
- [ ] Empty, duplicate, absolute, reserved, traversal, normalization-colliding, misleading-type, malformed multipart, and oversized inputs fail safely.
- [ ] Rejected uploads create no visible revision or completed orphan artifact.
- [ ] Successful uploads create exactly one immutable revision with creator, time, artifact metadata, and digests.
- [ ] Upload failure or concurrency never changes the previous latest or published revision.
- [ ] HTML and CSV bytes remain unchanged after validation.
- [ ] The owner UI maps errors to affected files and removing a staged CSV never alters an older revision.

## Out of scope

- ZIP/static asset packages, in-browser editing, HTML rewriting, and CSV transformation.
