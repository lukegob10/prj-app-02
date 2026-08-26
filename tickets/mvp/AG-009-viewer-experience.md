# AG-009 — Build Shared with Me and the dashboard viewer experience

- Milestone: MVP
- Epic: Viewer experience
- Priority: P0
- Size: L
- Depends on: AG-004, AG-007, AG-008

## Goal

Let an authenticated viewer find and safely use exactly the published dashboards shared with their SOEID.

## Scope

- Build Shared with Me with dashboard name, description, owner, publication time, and stable open action.
- Build a trusted viewer shell around the isolated dashboard iframe.
- Handle loading, expired render authorization, revoked access, unpublish, archive, missing attachment, content failure, and unresponsive JavaScript.
- Keep unauthorized dashboards out of lists and protect direct URLs without revealing existence.
- Provide a safe stop/reload/back-to-portal path that does not lose the user's session.

## Acceptance criteria

- [ ] A viewer sees only currently published dashboards with an active SOEID grant.
- [ ] Grant removal, user disablement, or unpublish removes access from lists, stable URLs, HTML, and CSV routes.
- [ ] Identifier tampering reveals no unauthorized metadata or content.
- [ ] The trusted shell always identifies the owner and labels the dashboard as user-created content.
- [ ] Broken or looping dashboard content cannot break portal navigation or authentication.
- [ ] Viewer failures are actionable without revealing inaccessible dashboard existence.
- [ ] Loading, error, and navigation states are keyboard and assistive-technology accessible.

## Out of scope

- Favorites, comments, ratings, recommendations, and firmwide discovery.
