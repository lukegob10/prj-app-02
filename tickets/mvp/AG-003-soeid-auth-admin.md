# AG-003 — Implement local SOEID authentication and user administration

- Milestone: MVP
- Epic: Identity
- Priority: P0
- Size: L
- Depends on: AG-001, AG-002

## Goal

Provide secure local accounts whose canonical application identity is SOEID and that can later be mapped to firm SSO.

## Scope

- Implement login, logout, secure sessions, CSRF protection, authentication throttling, and disabled-user handling.
- Normalize SOEIDs at one trusted boundary and expose a normalized principal to application policies.
- Disable self-registration and use secure framework-native password hashing.
- Add a safe first-administrator bootstrap process.
- Add administrator-only user creation, listing, disabling, re-enabling, and credential reset.
- Prevent disabling the last active administrator and prepare identity events for the audit stream.

## Acceptance criteria

- [ ] Active users can sign in and out; invalid credentials produce a generic response.
- [ ] Browser form values or identity headers cannot impersonate another SOEID.
- [ ] Portal session cookies are secure, HTTP-only, host-only, rotated appropriately, and absent from content-origin requests.
- [ ] Only administrators can provision or change users; duplicate SOEIDs are rejected.
- [ ] Disabled users lose authorized access within the documented session-revocation window.
- [ ] Bootstrap and reset credentials are never embedded in code, logged, or redisplayed.
- [ ] The last active administrator cannot be accidentally disabled.

## Out of scope

- Self-service registration, email reset, multifactor authentication, directory sync, and reverse-proxy SSO.
