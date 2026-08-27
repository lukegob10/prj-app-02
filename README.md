# Agora

Agora is an internal application for securely uploading, publishing, and sharing
self-contained HTML dashboards with optional CSV attachments. This repository currently
implements the secure foundation, metadata/private-storage boundary, local SOEID authentication,
and a runnable owner workspace for creating projects, uploading revisions, and previewing exact
HTML/CSV dashboards. The preserved [`tickets`](./tickets/README.md) backlog remains the authority
for the complete publishing, sharing, and operations roadmap.

## Foundation decisions

- CPython 3.14.7, Django 5.2 LTS, and the Uvicorn ASGI server
- PostgreSQL 18 with Django ORM migrations and Psycopg 3
- Server-rendered Django templates and committed CSS; no Node runtime or SPA framework
- Separate portal and content Django entry points, hosts, middleware, and URL configurations
- Constrained PostgreSQL metadata, immutable revision history, and private no-clobber filesystem
  storage with durable cleanup reservations
- uv universal dependency lock, Ruff formatting/linting, mypy, pytest, and GitHub Actions CI

The portal is trusted. Uploaded HTML is always hostile and must never be returned from a
portal route, inserted into the portal DOM, placed in `srcdoc`, or converted to a same-origin
`blob:`/`data:` document. The content service exposes only short-lived, database-authorized
GET/HEAD routes for one exact revision and its CSV attachments; every other path returns 404.

Read the normative [product contract](./docs/product-contract.md),
[architecture](./docs/architecture.md), and [threat model](./docs/threat-model.md) before
adding features.

Portal page structure and reusable component conventions are recorded in
[docs/ui-conventions.md](./docs/ui-conventions.md).

## Prerequisites

- [uv 0.12.6](https://docs.astral.sh/uv/getting-started/installation/) (uv installs the pinned
  Python runtime automatically)
- Docker Engine with Compose for the local PostgreSQL service
- Permission to trust a development-only localhost certificate for authenticated local use

The legacy HTTP topology can optionally use these development-only mappings:

```text
127.0.0.1 portal.agora.test
127.0.0.1 content.agorausercontent.test
```

The recommended HTTPS launcher below does not require these mappings. It uses `localhost` for
the trusted portal and `127.0.0.1` for isolated content so portal cookies cannot be sent to the
content host.

## First run

```powershell
uv sync --locked --all-groups
uv run --locked python -m playwright install chromium
uv run python scripts/bootstrap_env.py
docker compose up -d postgres
uv run python manage.py migrate
uv run python manage.py bootstrap_admin --soeid ASSIGNED_SOEID
```

The bootstrap command prompts for the password without echoing it; see
[local authentication and user administration](./docs/authentication.md). Browser authentication
requires the TLS setup below, and production configuration rejects HTTP origins.

For authenticated local use on Windows, the already-installed .NET SDK can be used once as a
certificate-management helper. It is not an Agora runtime or deployment dependency. Trust and
export its development certificate, then start the TLS-backed Python portal:

```powershell
dotnet dev-certs https --trust
dotnet dev-certs https --export-path .local/tls/localhost.pem --format Pem --no-password
uv run python scripts/run_local.py
```

Open `https://localhost:8443`. The single launcher starts the Uvicorn-backed portal there and the
cookie-free isolated content service at `https://127.0.0.1:8444`; both automatically reload Python
changes. In development, the portal reads templates without Django's production cache, so a normal
browser refresh also loads current HTML and committed CSS. The certificate and private key live
only under ignored `.local/tls/`; never copy them into source control or use them outside local
development.

This certificate workflow is local-development-only. A deployed environment terminates HTTPS
with an organization-issued certificate at its load balancer or reverse proxy and runs the Agora
Python application behind it; .NET is not required on the server.

Django and Uvicorn serve different layers: Django is the application framework that owns routes,
authentication, sessions, CSRF, templates, and persistence; Uvicorn is the network server that
runs Django's ASGI application. The TLS launcher uses both and automatically reloads Python source
changes.

For a read-only UI preview when the system hosts file cannot be edited, run:

```powershell
uv run python scripts/run_preview.py
```

Open `http://127.0.0.1:8002`. Django automatically restarts this preview when Python files
change; refresh the browser to see the latest templates, styles, and backend behavior. Because
secure authentication cookies deliberately require HTTPS, use the canonical TLS-backed portal
origin—not this convenience preview—for login or authenticated testing.

To run either entry point separately for diagnostics, use:

```powershell
uv run python scripts/run_https.py
uv run python scripts/run_content_https.py
```

The content root intentionally returns 404. Only an iframe URL minted by an authorized portal
preview or published-view shell can resolve HTML/CSV bytes, and the content composition has no
session, login, mutation, administration, or template middleware.

## Checks

Start PostgreSQL, then run the same complete quality gate used by CI:

```powershell
uv run python scripts/check.py
```

Focused commands are also available:

```powershell
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests scripts
uv run pytest
uv build
```

The canonical test command includes the deterministic Chromium browser-security suite. The
browser binary is pinned by the Playwright version in `uv.lock`; install it once after syncing:

```powershell
uv run --locked python -m playwright install chromium
uv run --locked pytest tests/browser --browser chromium --no-cov
```

The browser suite uses three loopback-resolved fixture origins and never serves production
artifact routes or render credentials. Its proven guarantees and residual network/CPU risks are
documented in [`docs/browser-security.md`](./docs/browser-security.md).

The test suite deliberately connects to PostgreSQL; SQLite is not a supported substitute.
Configuration keys and safe local setup are documented in
[docs/configuration.md](./docs/configuration.md).

Expired storage reservations can be reconciled idempotently without scanning arbitrary paths:

```powershell
uv run python manage.py cleanup_artifact_reservations --limit 100
```

The private filesystem contract, durability assumptions, and backup boundary are documented in
[docs/storage.md](./docs/storage.md).

## Scope

The current vertical slice includes Home, My Projects, Shared with Me, Create Project, Project
Detail, bounded HTML/CSV upload, and owner/published viewing through the isolated renderer. It
does not yet include owner-facing grant management, publish/unpublish controls, archive/delete,
object storage, ECS topology, reverse-proxy SSO, or production hosting. Those remain explicit
backlog work rather than hidden behavior.
