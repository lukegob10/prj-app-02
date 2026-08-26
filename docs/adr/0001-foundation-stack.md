# ADR 0001: Modular Django foundation

- Status: Accepted
- Date: 2026-08-26
- Decision scope: AG-001 only

## Context

Agora needs server-side identity and authorization, transactional metadata, immutable artifact
coordination, secure multipart handling, and a hard browser boundary around active uploaded HTML.
The MVP is one shared application and does not justify a client SPA, per-Dashboard containers,
or independently deployed microservices.

## Decision

Use a modular Django 5.2 LTS monolith on CPython 3.14, PostgreSQL 18 through Psycopg 3, and
Django ORM migrations. Render the trusted UI with Django templates and committed CSS. Maintain
two explicit web compositions:

- portal settings/URLs/middleware/ASGI/WSGI for trusted UI, identity, and mutations;
- content settings/URLs/middleware/ASGI/WSGI for narrow read-only authorized artifacts.

Use uv with a committed universal lock, Ruff, strict mypy with django-stubs, pytest against
PostgreSQL, and a SHA-pinned GitHub Actions workflow. Pin the developer and CI interpreter to
Python 3.14.7 and constrain supported runtime to current 3.14 patch releases.

## Consequences

- Django supplies mature server-side security primitives, forms/templates, ORM, and migrations
  without adding a separate frontend runtime.
- PostgreSQL setup costs more than SQLite but tests the database semantics later acceptance
  criteria depend on.
- Two entry points add a second development process but make middleware and route ownership
  explicit; a host-switching router would make accidental trust crossover easier.
- Django 5.2 lacks the later built-in CSP API, but uploaded-content policy must be response- and
  route-specific regardless; LTS support through April 2028 is the stronger baseline.
- django-stubs versioning does not exactly match Django 5.2; strict static analysis is useful but
  runtime system/smoke tests remain authoritative.
- No Node/SPA toolchain means fewer dependencies and less client-state complexity. A later ticket
  may add a minimal browser dependency only when a proven workflow requires it.

## Rejected alternatives

- **Django 6.1:** newer features but a shorter support window than 5.2 LTS.
- **FastAPI plus a separate SPA:** duplicates routing, validation, authentication, CSRF, build,
  and deployment concerns without an MVP need.
- **SQLite for development/tests:** conceals PostgreSQL constraints, locking, and transaction
  behavior that future tickets explicitly require.
- **One process selected only by Host:** fewer commands but a larger chance that portal middleware
  or routes accidentally handle hostile content.
- **Per-Dashboard containers:** contradicts the product contract and creates needless operational
  scope.

Production hosting, ECS topology, SSO, HA, and storage-vendor selection are not decided here.
