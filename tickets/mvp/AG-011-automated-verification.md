# AG-011 — Add automated functional and security verification

- Milestone: MVP
- Epic: Quality
- Priority: P0
- Size: XL
- Depends on: AG-003 through AG-010

## Goal

Lock down the complete administrator-owner-viewer workflow, security boundaries, and failure semantics before release.

## Scope

- Unit-test SOEID normalization, policies, validation, storage keys, revisions, render authorization, publication, and redaction.
- Integration-test migrations, persistence transactions, authentication, upload rollback, grants, revocation, publication, and artifact delivery.
- Browser-test administrator provisioning, owner upload/preview/share/publish, viewer access, revoke, and unpublish across separate origins.
- Add malicious HTML attempts for portal access, external fetch/beacons, forms, popups, navigation, and cross-dashboard reads.
- Add malicious upload fixtures for traversal, normalization collisions, malformed multipart, misleading types, and size limits.
- Check primary keyboard paths and serious automated accessibility violations.

## Acceptance criteria

- [ ] Every cell in the authorization matrix has automated coverage.
- [ ] Failed upload/publication leaves the last valid draft and published revision unchanged.
- [ ] Owner, viewer, unrelated, disabled, expired-token, and unauthenticated cases are covered for HTML and CSV routes.
- [ ] The complete happy path passes in every supported browser engine.
- [ ] Malicious dashboard and upload fixtures cannot cross the portal, dashboard, storage, or external-network boundaries.
- [ ] The suite is deterministic in CI, uses no required external service, and does not leak test secrets or uploaded data.
- [ ] Security-critical modules meet the agreed branch-coverage threshold and critical accessibility checks pass.

## Out of scope

- Production-scale load tests, soak tests, and comprehensive visual regression.
