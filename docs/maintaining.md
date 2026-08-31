# Maintainer handoff

This page is the short operational map for Agora. The [README](../README.md) is the canonical
local setup and user-facing run guide; this page explains where a maintainer should look and
which path is canonical for each recurring workflow. The [product contract](product-contract.md),
[architecture](architecture.md), and [threat model](threat-model.md) remain normative. Do not
infer a feature from a ticket or a core-domain seam unless a route and test prove that it is
implemented.

## Five-minute map

| Need | Start here | Boundary to keep in mind |
|---|---|---|
| Install or refresh development dependencies | `uv sync --locked --all-groups` | [`pyproject.toml`](../pyproject.toml) and committed [`uv.lock`](../uv.lock) are the dependency/build contract. |
| Install the pinned browser engine | `uv run --locked python -m playwright install chromium` | Run once after syncing; the Chromium version is locked with the project dependencies. |
| Configure a local environment | `uv run --locked python scripts/bootstrap_env.py` | It creates an ignored `.env`, prompts for the local Oracle password, and refuses to overwrite an existing file. |
| Provision the first local administrator | `uv run --locked python manage.py bootstrap_admin --soeid ASSIGNED_SOEID` | One-time protected-terminal action after migrations; the command prompts for the password. |
| Run the application locally | `uv run --locked python scripts/run_local.py` | This starts the trusted portal and isolated content services together over development HTTPS. |
| Validate the repository | `uv run --locked python scripts/check.py` | This is the one full quality gate used by CI; it needs Chromium, a disposable Oracle validation schema, and the explicit reset acknowledgement described below. |
| Apply an existing schema | `uv run --locked python manage.py migrate --noinput` | Django migrations own the schema lifecycle; use an Agora validation schema, never production data. |
| Verify generated Oracle DDL | `uv run --locked python scripts/generate_oracle_schema.py --check` | Migrations remain authoritative; this checks that `database/oracle/schema.sql` is their current generated deployment artifact. |
| Reconcile expired storage reservations | `uv run --locked python manage.py cleanup_artifact_reservations --limit 100` | This is a bounded operational repair, not an arbitrary filesystem cleanup. |

The complete first-run sequence, certificate setup, local URLs, and authentication caveats are
kept in [README — First run](../README.md#first-run). The local Oracle implementation in
`packages/treasury-analytics/` is a development substitute for the managed package boundary; it
does not make SQLite a supported database.

## Canonical workflows

### Setup and local development

Use the commands in the five-minute map for a clean checkout, then repeat `migrate` when the
checked-in schema advances. On Windows, trust and export the development certificate before the
first local run, as described in the README. Keep the certificate and key under the ignored
`.local/tls/` directory. Then use the combined launcher:

```powershell
uv run --locked python scripts/run_local.py
```

Open `https://localhost:8443`. The launcher starts the portal on `localhost:8443` and the
cookie-free content service on `127.0.0.1:8444`; both are development-only and reload source
changes. `scripts/run_preview.py` is an optional unauthenticated HTTP convenience preview.
`run_https.py` and `run_content_https.py` are optional individual diagnostics when the combined
launcher is not suitable. They are not deployment entry points.

### Checks and build

The canonical local and CI gate is:

```powershell
uv run --locked python scripts/check.py
```

`scripts/check.py` runs, in order, the lock check, Ruff format and lint, strict mypy, bytecode
compilation, portal and content Django checks, static collection, migration-drift detection,
migration application, the Chromium-enabled pytest suite, and `uv build`. It stops at the first
failure. Before launching even the lock check, it requires the two disposable-schema opt-ins below;
this prevents its earlier migration step from writing before pytest's collection guard. CI uses
the same command from `.github/workflows/ci.yml` on a self-hosted Linux runner with an
Oracle-capable `PROD` profile.

Run an individual gate only to diagnose a failure; these commands do not replace the full gate:

```powershell
uv lock --check
uv run --locked ruff format --check .
uv run --locked ruff check .
uv run --locked mypy src tests scripts
uv run --locked pytest --browser chromium
uv run --locked pytest tests/browser --browser chromium --no-cov
uv build
```

The focused browser command is useful for renderer/security diagnosis. Keep the coverage-bearing
full gate as the release signal. The browser suite is documented in
[`docs/browser-security.md`](browser-security.md); it does not replace Oracle-backed integration
validation.

Database-bearing pytest selections intentionally flush the reused Oracle validation schema once
at session start and continue to use Django's destructive transactional-test cleanup. Before any
database setup, they fail closed unless the raw process values include `AGORA_ENVIRONMENT=test`
and `AGORA_TEST_DATABASE_RESET_ALLOWED=true`; the resolved Django environment is checked again
before the flush. The bootstrap writes this test-only acknowledgement as `false`; set it to `true` only
after verifying that the selected `treasury_analytics` profile resolves to a disposable,
dedicated schema containing no production or shared data. Pure non-database diagnostics do not
need the acknowledgement. Prefer process-local overrides so the same `.env` remains safe for the
development launcher:

```powershell
$env:AGORA_ENVIRONMENT = "test"
$env:AGORA_TEST_DATABASE_RESET_ALLOWED = "true"
uv run --locked python scripts/check.py
```

### Schema lifecycle

The checked-in Django migration graph under `src/agora/core/migrations/` is the sole schema
authoring authority. Runtime models under `src/agora/core/` must remain aligned with it, while
Oracle-specific constraints and triggers stay versioned in those migrations. `src/agora/db/`
owns the Django Oracle backend and the
`treasury_analytics.TAConnection` boundary; `src/agora/db/table_names.py` owns the physical
namespace mapping. The `agora.core` package owns metadata, audit, durable storage reservations,
and domain invariants. The private artifact bytes remain outside the database under
`AGORA_ARTIFACT_ROOT`; see [storage operations](storage.md). Django's historical `persistence`
app label remains a schema/migration compatibility boundary; it is not the canonical Python
source path.

For a schema change, keep the model, forward migration, reversal consideration, focused tests,
and documentation aligned. Before applying it, inspect drift with the same check used by CI:

```powershell
uv run --locked python manage.py makemigrations --check --dry-run
```

Apply only reviewed, checked-in migrations with `migrate`. Never edit an already-applied migration
to change history or bypass the migration graph with an unreviewed database script. The
deployment-facing [`database/oracle/schema.sql`](../database/oracle/schema.sql) is generated from
that migration graph; it is not a second schema-authoring source. Follow
[`database/README.md`](../database/README.md), never hand-edit the generated SQL, and diagnose
drift with:

```powershell
uv run --locked python scripts/generate_oracle_schema.py --check
```

Migration changes are a protected boundary; coordinate them with the database owner and run the
Oracle-backed checks before integration.

### Packaging and release boundary

[`pyproject.toml`](../pyproject.toml) defines the Agora package metadata, supported CPython range, dependency groups,
Ruff/mypy/pytest policy, and Hatch wheel/sdist contents. `uv.lock` is committed and CI rejects
drift. `uv build` is part of the canonical quality gate and is the package smoke check; the
repository does not publish a registry artifact or define a release upload command.

The local `treasury-analytics` package is selected through `[tool.uv.sources]`. A managed
deployment supplies its corporate implementation with the same `treasury_analytics.TAConnection`
interface and profile-selected `ENV`/`TA_<ENV>_PASSWORD` contract. Do not duplicate connection
coordinates in Agora configuration. See [configuration](configuration.md) and
[ADR 0002](adr/0002-oracle-connection-boundary.md).

## Deployment interface

[`Dockerfile`](../Dockerfile), [`deploy/entrypoint.py`](../deploy/entrypoint.py), and the
[`deployment runbook`](deployment.md) define the production image and process contract.
Deployment infrastructure still owns TLS termination, DNS, private artifact storage, Oracle
credentials, supervision, and rollout/rollback. The underlying application callables remain
explicit:

| Service | ASGI callable (selected baseline) | WSGI callable (supported interface) | Settings composition |
|---|---|---|---|
| Trusted portal | `agora.asgi:application` | `agora.wsgi:application` | `agora.settings.portal` |
| Isolated content | `agora.content_asgi:application` | `agora.content_wsgi:application` | `agora.settings.content` |

Run the two compositions as independently managed services. Production configuration requires
HTTPS for both `AGORA_PORTAL_ORIGIN` and `AGORA_CONTENT_ORIGIN`, different hostnames, separate
service secrets, and a private absolute `AGORA_ARTIFACT_ROOT`. The portal owns authentication,
sessions, CSRF, templates, mutations, and metadata UI. The content service is read-only and
cookie-free: it serves only exact authorized HTML/supporting-artifact routes and returns 404 for
everything else. Uploaded HTML is hostile and must stay outside the trusted portal DOM and
origin. The deployment exposes `/health/live/` for process liveness and `/health/ready/` for
dependency readiness; use the semantics and rollout guidance in the deployment runbook rather
than treating either route as a general application API. The
[architecture boundary table](architecture.md#local-development-topology) and
[configuration contract](configuration.md) define the remaining requirements.

The development launchers override origins and enable local-only behavior. Do not use
`scripts/run_local.py`, `scripts/run_https.py`, or `scripts/run_content_https.py` as a production
start command. Use the checked-in production entrypoint and deployment runbook, which bind the
appropriate callable without collapsing the portal/content trust boundary.

## Ownership boundaries

Ownership here means the subsystem responsible for a concern, not a person or team assignment.
Keep changes in the narrowest applicable boundary and update the linked source of truth when a
contract changes.

| Subsystem | Primary paths | Change here for |
|---|---|---|
| Shared startup/configuration | `src/agora/config.py`, `src/agora/settings/base.py` | Environment validation, shared middleware/database settings, and non-browser defaults. |
| Trusted portal composition | `src/agora/settings/portal.py`, `src/agora/urls/portal.py`, `src/agora/portal/` | Login, admin, project/discovery/stewardship UI, forms, templates, and portal-only behavior. |
| Isolated content composition | `src/agora/settings/content.py`, `src/agora/urls/content.py`, `src/agora/rendering/` | Render authorization, exact HTML/CSV/supporting-artifact delivery, CSP, and content response headers. Never import portal views or session behavior here. |
| Core domain | `src/agora/core/` (`agora.core`) | Models, policies, services, queries, audit, migrations, and storage coordination. Keep templates and HTTP concerns out of this boundary. |
| Oracle adapter | `src/agora/db/`, `src/agora/db/table_names.py`, `packages/treasury-analytics/` | Django backend integration and the managed connection API. Do not duplicate package-owned connection coordinates. |
| Generated Oracle artifact | `database/`, `scripts/generate_oracle_schema.py` | Deployment-facing SQL generated from the authoritative Django migration graph. Never hand-edit `schema.sql` or treat it as a second schema source. |
| Artifact bytes | `src/agora/core/storage.py`, `src/agora/core/uploads.py`, `src/agora/uploads.py` | Private key generation, safe writes, upload validation, and cleanup ownership. Artifacts are not static/media files or raw URLs. |
| Deployment | `Dockerfile`, `deploy/`, `docs/deployment.md` | Production image, process entrypoint, health semantics, and rollout/rollback interface. Preserve the independent portal/content compositions. |
| Developer/operator workflows | `scripts/check.py`, `scripts/bootstrap_env.py`, `scripts/run_*.py`, `scripts/load/` | Canonical checks, local launchers, environment bootstrap, bounded cleanup, and opt-in capacity diagnostics. Add a wrapper only when it removes a real repeated workflow. |
| Contract and validation evidence | `docs/`, `tickets/`, `tests/` | Normative requirements, recorded decisions/backlog, acceptance/security coverage, and regression checks. Tickets and ADRs are preserved history, not substitutes for runtime wiring. |

The enhancement query seams (`src/agora/core/enhancements.py`, `analytics.py`, and
`enhancement_queries.py`) support later workflows described by the product contract; their
existence does not mean that a corresponding UI or operations job is available. Likewise, the
capacity harness is opt-in evidence gathering, not a production capacity claim.

## Safe maintainer loop

1. Read the relevant contract and architecture section before touching an active boundary.
2. Search current call sites, URL wiring, scripts, CI, and tests before moving or removing code.
3. Make the smallest change that preserves the portal/content trust split, auth semantics, storage
   ownership, and migration history.
4. Run the focused diagnostic for the changed boundary, then the canonical full gate.
5. Record unresolved operational evidence rather than weakening a gate or claiming an unverified
   scale/deployment property.

High-signal references:

- [Product contract](product-contract.md) — user-visible and security contracts.
- [Architecture](architecture.md) — trust boundaries, module ownership, and runtime stack.
- [Configuration](configuration.md) — environment variables and fail-closed startup rules.
- [Authentication](authentication.md) — local SOEID, sessions, and administrator controls.
- [Storage](storage.md) — private artifact durability, cleanup, backup, and restore boundary.
- [Browser security](browser-security.md) — isolated renderer test guarantees and residual risk.
- [Scaling runbook](scaling.md) — explicitly unverified workload targets and blockers.
- [UI conventions](ui-conventions.md) — server-rendered accessibility and component contracts.
- [ADR 0001](adr/0001-foundation-stack.md) and [ADR 0002](adr/0002-oracle-connection-boundary.md) — recorded stack and database-boundary decisions.
- [Acceptance evidence](acceptance/AG-001.md) through [AG-004](acceptance/AG-004.md) — current acceptance slices.
- [MVP backlog](../tickets/README.md) — preserved delivery roadmap and deferred scope.
