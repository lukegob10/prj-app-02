# AG-010 — Add auditability, observability, and security hardening

- Milestone: MVP
- Epic: Governance and operations
- Priority: P0
- Size: XL
- Depends on: AG-003 through AG-009

## Goal

Make the application supportable and defensible without logging sensitive uploaded content or weakening the render boundary.

## Scope

- Emit append-only audit events for authentication, user administration, dashboard lifecycle, revision upload, grants, preview issuance, publish, and unpublish.
- Add structured logs, correlation IDs, redacting exception handling, and liveness/readiness endpoints.
- Harden CSRF, session rotation, hosts/origins, request limits, rate limits, security headers, response filenames, storage paths, IDOR, and mass assignment.
- Add dependency and secret scanning to CI.
- Add owner-relevant activity history and an administrator operational view where practical.
- Complete an MVP security checklist and resolve all critical/high findings.

## Acceptance criteria

- [ ] Critical state changes record actor, target, type, time, request ID, and constrained metadata without raw HTML/CSV or credentials.
- [ ] Logs exclude passwords, cookies, auth headers, render tokens, HTML, CSV contents, and sensitive query strings.
- [ ] Readiness reflects required persistence; liveness is not coupled to optional dependencies.
- [ ] User-facing server errors expose only a correlation ID, never stack traces or filesystem paths.
- [ ] Oversized, cross-origin, traversal, normalization, token-tampering, IDOR, and rate-limit cases fail safely.
- [ ] Ordinary users cannot change audit records or view unrelated activity.
- [ ] CI identifies known vulnerable dependencies and committed secret patterns with no unresolved MVP-critical finding.

## Out of scope

- Vendor monitoring agents, SIEM export, formal penetration testing, and malware-scanning services.
