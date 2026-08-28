# AG-004 — Implement authorization and viewer sharing by SOEID

- Milestone: MVP
- Epic: Access control
- Priority: P0
- Size: L
- Depends on: AG-002, AG-003

## Goal

Centralize ownership and viewer permissions so every metadata, HTML, and CSV request follows one server-side policy.

## Scope

- Define policy functions for dashboard management, upload, preview, grant management, publication, viewing, archive, and delete.
- Restrict management to the owner and viewing to the owner or an active SOEID grant against a published revision.
- Add owner-only grant listing, creation, and revocation for locally provisioned active users.
- Normalize grant targets through the identity service and prevent duplicates or owner self-grants.
- Define not-found versus forbidden behavior that limits dashboard enumeration.
- Make revocation and user disablement effective across metadata, HTML, CSV, and render authorization.

## Acceptance criteria

- [x] A documented and tested policy matrix covers owner, granted viewer, unrelated user, administrator, disabled user, and unauthenticated user.
- [x] Viewers cannot access drafts, owner controls, or arbitrary revisions by changing identifiers.
- [x] Owners can add and remove active viewers by canonical SOEID; invalid, unknown, disabled, duplicate, and self-grants are handled safely.
- [x] A grant alone exposes no unpublished content.
- [x] Revocation blocks subsequent HTML and CSV access within the documented maximum window.
- [x] APIs and content handlers call shared policies instead of duplicating permission logic.
- [x] Grant changes identify actor, target SOEID, dashboard, and time for auditing.

## Implementation and scale notes

The owner access page reports a database-computed effective viewer count and bounded current and
retained grant epochs. Project, revision, and grant queries are owner-scoped, deterministic, lazy,
and index-friendly; Shared with Me uses an existence check so retained revoked epochs cannot create
duplicate rows. The active-only grant invariant permits a revoked viewer to be regranted without
destroying audit history. Query-count regressions and the complete policy matrix are covered by the
AG-004 tests and the integrated renderer/portal suites.

The next scale gate is an Oracle-backed staging workload, not a unit-test result. See
[`docs/acceptance/AG-004.md`](../../docs/acceptance/AG-004.md) for the acceptance evidence and
[`docs/scaling.md`](../../docs/scaling.md) for concurrent-session, authorization, Shared with Me,
mutation, render, HTML/CSV, latency, error, Oracle pool/query, and revocation-propagation gates.

## Out of scope

- Editors, groups, pending invitations, firmwide access, and anonymous access.
