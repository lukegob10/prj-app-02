# AG-008 — Implement preview, publishing, and stable dashboard URLs

- Milestone: MVP
- Epic: Publication lifecycle
- Priority: P0
- Size: L
- Depends on: AG-005, AG-006, AG-007

## Goal

Let owners validate an exact revision in the production-equivalent sandbox and deliberately promote it to a stable access-controlled URL.

## Scope

- Add owner-only preview for complete revisions using the same content origin, sandbox, CSP, and CSV endpoints as viewers.
- Display revision number, upload time, HTML/CSV names, validation summary, and a strong Preview indicator.
- Add atomic owner-only publish and unpublish actions.
- Resolve a stable dashboard URL to the pinned published revision only after authorization.
- Keep later uploads private until explicitly published and define behavior for no grants, republish, archive, and delete.
- Add clear controls and confirmations to the owner dashboard details screen.

## Acceptance criteria

- [ ] Preview is owner-only, short-lived, non-shareable, and never changes publication state.
- [ ] Publishing cannot select an incomplete, foreign, or deleted revision and updates the published pointer atomically.
- [ ] A stable URL remains unchanged across publication changes.
- [ ] Uploading a new revision does not change the viewer experience until the owner republishes.
- [ ] Unpublish blocks new viewer HTML/CSV access while retaining revisions and grants.
- [ ] Publishing with no grants is allowed but visible only to the owner.
- [ ] Concurrent publish attempts leave one valid published revision and a complete audit trail.

## Out of scope

- Scheduled publication, approval workflows, revision diffing, and rollback UI.
