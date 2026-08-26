# AG-007 — Build the isolated HTML renderer and CSV delivery boundary

- Milestone: MVP
- Epic: Secure rendering
- Priority: P0
- Size: XL
- Depends on: AG-001, AG-004, AG-006

## Goal

Run useful self-contained dashboard HTML without giving it access to portal identity, portal APIs, external exfiltration channels, or another dashboard's content.

## Scope

- Serve dashboard content only from the configured content origin; never return it from portal routes or insert it into the portal DOM.
- Issue short-lived render authorization scoped to viewer, dashboard, revision, audience, and content origin, with defined expiry and revocation.
- Render through a sandboxed iframe and enforce CSP plus defensive response headers.
- Block external scripts/connections, image beacons, forms, popups, top navigation, service workers, framing by unapproved origins, and unsupported capabilities.
- Serve CSV attachments through documented revision-scoped relative URLs resolved from metadata rather than filesystem paths.
- Provide a safe recovery path for failed, expired, or nonresponsive dashboard content.

## Acceptance criteria

- [ ] Portal cookies are absent from content-origin requests and content code cannot read portal DOM, storage, cookies, or authenticated APIs.
- [ ] Authorization for one viewer/revision cannot retrieve another revision, dashboard, CSV filename, or storage key.
- [ ] Expired, altered, wrong-audience, revoked, disabled-user, and unpublished authorizations fail closed.
- [ ] Inline behavior and same-revision CSV reads work in supported browsers under the documented sandbox.
- [ ] External fetch/beacon, form, popup, top-navigation, cross-dashboard, and MIME-sniffing test cases are blocked.
- [ ] Only the trusted portal origin can frame content, and the trusted shell identifies owner and user-created content.
- [ ] Viewers are explicitly informed that access includes complete CSV attachments.

## Out of scope

- Third-party CDN allowlists, public signed links, server-side filtering, and guaranteed prevention of browser CPU abuse.
