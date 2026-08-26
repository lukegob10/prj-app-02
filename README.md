# Agora

Agora is an internal application for securely uploading, publishing, and sharing
self-contained HTML dashboards with optional CSV attachments. This repository currently
implements the AG-001 foundation and AG-002 metadata/private-storage boundary; later MVP
behavior remains in the preserved [`tickets`](./tickets/README.md) backlog.

## Foundation decisions

- CPython 3.14.7 and Django 5.2 LTS
- PostgreSQL 18 with Django ORM migrations and Psycopg 3
- Server-rendered Django templates and committed CSS; no Node runtime or SPA framework
- Separate portal and content Django entry points, hosts, middleware, and URL configurations
- Constrained PostgreSQL metadata, immutable revision history, and private no-clobber filesystem
  storage with durable cleanup reservations
- uv universal dependency lock, Ruff formatting/linting, mypy, pytest, and GitHub Actions CI

The portal is trusted. Uploaded HTML is always hostile and must never be returned from a
portal route, inserted into the portal DOM, placed in `srcdoc`, or converted to a same-origin
`blob:`/`data:` document. The content service currently has no routes and returns 404 until
the authorized renderer is implemented by AG-007.

Read the normative [product contract](./docs/product-contract.md),
[architecture](./docs/architecture.md), and [threat model](./docs/threat-model.md) before
adding features.

Portal page structure and reusable component conventions are recorded in
[docs/ui-conventions.md](./docs/ui-conventions.md).

## Prerequisites

- [uv 0.12.6](https://docs.astral.sh/uv/getting-started/installation/) (uv installs the pinned
  Python runtime automatically)
- Docker Engine with Compose for the local PostgreSQL service
- Permission to add two loopback names to the local hosts file

Add these development-only mappings:

```text
127.0.0.1 portal.agora.test
127.0.0.1 content.agorausercontent.test
```

On Windows, edit `C:\Windows\System32\drivers\etc\hosts` from an elevated editor. On macOS
or Linux, edit `/etc/hosts` with appropriate administrative privileges. The two names use
different sites intentionally; different ports on one hostname are not a sufficient cookie
or SameSite boundary.

## First run

```powershell
uv sync --locked --all-groups
uv run python scripts/bootstrap_env.py
docker compose up -d postgres
uv run python manage.py migrate
uv run python manage.py runserver 127.0.0.1:8000
```

Open `http://portal.agora.test:8000`. Local HTTP is loopback-only and development-only; do
not use real credentials or data. Production configuration rejects HTTP origins. TLS-backed
local browser testing becomes mandatory before authentication and uploaded content are
enabled in AG-003/AG-007.

To prove that the isolated content process starts fail-closed, use a second terminal:

```powershell
uv run python -m django runserver 127.0.0.1:8001 --settings=agora.settings.content
```

`http://content.agorausercontent.test:8001/` intentionally returns 404. No content or portal
cookie route exists at this stage.

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

AG-001 locks contracts, tooling, boundaries, and a runnable skeleton. AG-002 adds canonical User,
Dashboard, immutable complete Revision/Artifact, Viewer Grant, append-only Audit Event, and
storage-reservation persistence plus a private filesystem adapter. It does **not** add login,
account administration workflows, dashboard/upload/share/publish UI, artifact HTTP delivery,
rendering, object storage, ECS topology, or production hosting. Those remain separate backlog
tickets so security-sensitive behavior is not smuggled across ticket boundaries.
