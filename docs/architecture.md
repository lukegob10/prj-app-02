# Architecture

Agora is one Django codebase with two deliberately separate HTTP surfaces.

## Services

The portal uses `agora.settings.portal`. It owns login, sessions, project management, uploads,
previews, administration, templates, and static assets.

The content service uses `agora.settings.content`. It has no session or authentication middleware
and serves only authorized, read-only dashboard-package requests. Uploaded HTML must never be
returned by a portal route.

Uvicorn is the only supported HTTP runtime. It loads two ASGI entry points from the same source:

- `agora.asgi:application` for `http://localhost:8000`
- `agora.content_asgi:application` for `http://127.0.0.1:8001`

The distinct hosts prevent portal cookies from being sent to the content service.
The portal process also serves its collected, content-hashed static assets through ASGI-native
ServeStatic middleware. The content process has no staticfiles application or middleware.

## Source layout

- `core/` contains models, domain services, queries, migrations, and storage.
- `portal/` contains trusted forms, views, templates, and static assets.
- `rendering/` authorizes and serves hostile dashboard-package content.
- `db/` adapts Django's Oracle backend to the installed `treasury_analytics` connection package.
- `settings/` and `urls/` compose the portal and content services.
- `config.py` and `middleware.py` hold shared runtime boundaries.

Keep dependencies flowing toward domain and boundary modules. Views should coordinate existing
services rather than duplicate authorization, persistence, or storage logic.

## Persistence

Oracle is the only supported relational database. Django migrations are the executable history;
`database/oracle/schema.sql` is the direct, readable DDL reference for the resulting tables,
keys, constraints, and indexes. Agora-owned table names use the `TB_TA_AGORA_` prefix.

Dashboard files live under the absolute private path configured by `AGORA_ARTIFACT_ROOT`.
Database rows store generated storage keys, never caller-supplied filesystem paths. Artifact
storage must remain outside static roots and outside the repository.

## Main flow

1. An administrator provisions a user.
2. An owner creates a project and uploads one HTML entry point with optional supporting files.
3. Validation reserves storage, writes immutable bytes, and commits the revision metadata.
4. The portal issues a short-lived authorization for an allowed preview or published view.
5. The browser loads the package from the separate content origin.
6. The content service rechecks authorization before every file response.

## Configuration

Configuration is read from process variables and the ignored `.env` file. Required values are
validated at startup. Portal and content secrets must differ, origins must be normalized and use
different hostnames, production origins must use HTTPS, and the artifact root must be absolute.
Origins describe browser-visible URLs; they are independent of Uvicorn's bind address.

When an external TLS proxy is present, Uvicorn accepts forwarding data only from the peer IPs or
CIDRs in `FORWARDED_ALLOW_IPS` and normalizes the ASGI scheme. Django does not trust raw proxy
headers or `X-Forwarded-Host`; it validates the preserved browser-visible Host normally.

The Oracle connection comes from the installed `treasury_analytics` package. Agora selects a
profile with `ENV` and supplies only the matching `TA_<ENV>_PASSWORD` secret.

See [Deployment](deployment.md) for image, proxy, migration, static, volume, and rollback
contracts.
