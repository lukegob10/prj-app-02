# AG-005 — Build dashboard ownership and management

- Milestone: MVP
- Epic: Owner experience
- Priority: P0
- Size: L
- Depends on: AG-002, AG-004

## Goal

Let an authenticated user create and manage multiple dashboards through a clear My Dashboards experience.

## Scope

- Implement owner-scoped create, list, retrieve, metadata update, archive, and delete behavior.
- Store name, description, owner, stable identifier, lifecycle state, latest revision, and published revision.
- Build My Dashboards and dashboard details with clear empty, loading, success, failure, and destructive-action states.
- Prevent mass assignment of owner, lifecycle, or publication fields.
- Display status, revision summary, viewer count, and last update.
- Meet baseline responsive, keyboard, focus, and form-label requirements.

## Acceptance criteria

- [ ] A user can own and distinguish multiple dashboards; new dashboards are private and unpublished.
- [ ] Stable identifiers are collision-resistant and do not unnecessarily expose sequential database IDs.
- [ ] Owners can update safe metadata but cannot use generic input to change protected fields.
- [ ] Users cannot retrieve or mutate another owner's dashboard through management routes.
- [ ] Archive/delete behavior handles grants, publication, revisions, and artifacts according to the approved product lifecycle.
- [ ] Destructive actions require explicit confirmation and explain viewer impact.
- [ ] Primary management workflows are keyboard accessible.

## Out of scope

- Ownership transfer, tags, folders, search, bulk actions, and dashboard duplication.
