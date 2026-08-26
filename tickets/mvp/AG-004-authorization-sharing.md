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

- [ ] A documented and tested policy matrix covers owner, granted viewer, unrelated user, administrator, disabled user, and unauthenticated user.
- [ ] Viewers cannot access drafts, owner controls, or arbitrary revisions by changing identifiers.
- [ ] Owners can add and remove active viewers by canonical SOEID; invalid, unknown, disabled, duplicate, and self-grants are handled safely.
- [ ] A grant alone exposes no unpublished content.
- [ ] Revocation blocks subsequent HTML and CSV access within the documented maximum window.
- [ ] APIs and content handlers call shared policies instead of duplicating permission logic.
- [ ] Grant changes identify actor, target SOEID, dashboard, and time for auditing.

## Out of scope

- Editors, groups, pending invitations, firmwide access, and anonymous access.
