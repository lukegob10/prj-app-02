# ADR 0002: Oracle connection boundary

- Status: Accepted
- Date: 2026-08-27
- Supersedes: PostgreSQL and Psycopg portions of ADR 0001

## Context

Agora must use Oracle locally and in managed corporate environments. Connection coordinates vary
by environment and are already encapsulated by a `treasury_analytics` package. Local development
uses its `PROD` profile; deployment may install the corporate implementation and select `SDLC`.
Database passwords must remain runtime secrets rather than source-controlled settings.

## Decision

Use Django's Oracle backend and `python-oracledb` behind a narrow custom backend adapter. The
adapter passes normalized `ENV` to `treasury_analytics.TAConnection` and does not accept or
duplicate a username, hostname, port, or service name. The package reads the corresponding
`TA_<ENV>_PASSWORD` from the process.

The repository provides an API-compatible local package through uv, which installs under the
active `.venv`. Managed images replace that development implementation with the corporate package
without changing Agora imports or settings. Local `.env` loading is optional and never overrides
container variables; deployment injects `ENV` and the password directly.

Port retained-history and ownership invariants to Oracle-native constraints, function-based
indexes, and PL/SQL triggers. Keep Python NFKC/casefold normalization as the authoritative
filename transformation because Oracle's built-in composition and case operations do not exactly
reproduce Python's full algorithm. Oracle still rejects non-composed/non-lowercase stored forms and
enforces the normalized comparison key's uniqueness.

Give every table owned by the Agora deployment a `TB_TA_AGORA_<CORE_TABLE>` name. The stable
prefix identifies the project and the suffix identifies the domain or framework table. Keep
`persistence` as the Django app label, not as part of the physical Oracle namespace. Apply the
same project prefix to the Django framework tables Agora installs, including migrations,
content types, permissions, groups, their join table, and sessions.

## Consequences

- Local setup no longer runs a PostgreSQL container.
- Tests and migrations require a reachable, non-production Oracle schema selected by the package.
- Agora tables are readily distinguishable from other applications in a shared Oracle schema;
  both Django runtime metadata and migration state must retain the project prefix.
- The test database configuration reuses that selected schema instead of creating an Oracle user;
  destructive migration verification must therefore use a disposable schema.
- CI requires a self-hosted runner that can reach the Oracle test profile and receive its password
  from the CI secret store.
- `CONN_MAX_AGE=0` keeps connection lifecycle ownership simple while the adapter returns direct
  package connections. Pooling can be introduced later through the same package boundary.

## Rejected alternatives

- **Expose host/user/service settings in Agora:** duplicates package-owned configuration and makes
  local and managed deployments diverge.
- **Keep PostgreSQL for local tests:** fails to exercise Oracle SQL, locking, LOB, trigger, and
  exception behavior that production uses.
- **Use SQLite for tests:** conceals even more of the required database semantics.
- **Copy the password into Django settings or deployment files:** expands secret exposure without
  improving the connection boundary.
