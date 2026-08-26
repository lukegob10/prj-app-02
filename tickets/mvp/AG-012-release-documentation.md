# AG-012 — Package, document, and accept the MVP release

- Milestone: MVP
- Epic: Release
- Priority: P0
- Size: L
- Depends on: AG-001 through AG-011

## Goal

Produce a repeatable MVP release that pilot users and operators can install, use, diagnose, back up, and recover without ECS-specific infrastructure.

## Scope

- Add a production-mode build/start path and safe environment configuration for the selected stack.
- Document database migration, first-administrator bootstrap, storage, backup expectations, restore outline, logs, health, user disablement, and emergency unpublish.
- Write the author contract for self-contained HTML, blocked external dependencies, relative CSV paths, limits, and troubleshooting with a minimal safe example.
- Write owner and viewer guides covering upload, preview, SOEID grants, publish, revoke, unpublish, user-created content, and complete-CSV visibility.
- Execute the full MVP acceptance checklist and record known limitations and deferred roadmap items.

## Acceptance criteria

- [ ] A clean environment can install, configure, migrate, bootstrap, start, and pass health checks using documented steps.
- [ ] Portal and content origins are independently configurable in production mode.
- [ ] No runtime secret, example password, uploaded artifact, or production data is committed or baked into a release.
- [ ] Operator documentation covers logs, backup, recovery, account disablement, and emergency content removal.
- [ ] Author documentation is sufficient to build an HTML file that loads zero or more named CSV attachments under the enforced CSP.
- [ ] All P0 acceptance criteria and the full automated suite pass.
- [ ] The product owner signs off on the MVP outcome in `tickets/README.md`.

## Out of scope

- ECS definitions, cloud-specific infrastructure, enterprise SSO, and formal service-level objectives.
