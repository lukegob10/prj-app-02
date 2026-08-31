# Foundation architecture and trust boundaries

Agora is one modular Django application with two least-privilege web entry points. It does not
launch a process or container per Dashboard. Portal and content may share an application
package, policy layer, Oracle metadata database, and private storage adapter, but they do
not share browser trust, URL configuration, or middleware.

## Boundary diagram

```mermaid
flowchart LR
  subgraph Browser[User browser]
    PDOM[Trusted portal DOM]
    IFRAME[Sandboxed hostile-content iframe]
  end

  subgraph PortalBoundary[Portal origin — identity and trusted UI]
    PORTAL[Portal entry point\nportal settings + URLs]
    IDENTITY[Identity/session boundary\ncanonical SOEID principal]
    POLICY[Shared authorization policy\ndefault deny]
  end

  subgraph DataBoundary[Metadata data boundary]
    DB[(Oracle\nmetadata only)]
  end

  subgraph ContentBoundary[Content origin — read-only, no portal session]
    CONTENT[Content entry point\ncontent settings + URLs]
  end

  subgraph StorageBoundary[Private artifact storage boundary]
    STORAGE[(Immutable hostile bytes\noutside public/static roots)]
  end

  PDOM -->|host-only portal session| PORTAL
  PORTAL --> IDENTITY
  IDENTITY --> POLICY
  POLICY --> DB
  PORTAL -->|short-lived render authorization| IFRAME
  IFRAME -->|content-scoped authorization only| CONTENT
  CONTENT --> POLICY
  CONTENT --> STORAGE
  IFRAME -. blocked by origin + sandbox .-> PDOM
  IFRAME -. common channels restricted by CSP .-> INTERNET[External network]
```

The rendered edge is implemented for exact owner previews and pinned published views. It never
returns uploaded bytes through the portal process.

## Local-development topology

| Surface | URL | Process | Trust |
|---|---|---|---|
| Portal | `https://localhost:8443` | `scripts/run_https.py` → `agora.settings.portal` | Trusted UI; host-only secure cookies. |
| Content | `https://127.0.0.1:8444` | `scripts/run_content_https.py` → `agora.settings.content` | Untrusted/read-only; exact health probes and authorized routes plus catch-all 404. |
| Oracle | package-managed `PROD` profile | `treasury_analytics.TAConnection` through the custom Django backend | Metadata boundary; connection coordinates stay inside the package. |
| Artifacts | absolute `AGORA_ARTIFACT_ROOT` | AG-002 private filesystem adapter | Opaque generated keys only; never a static/media root or raw URL. |

Both URLs terminate on loopback but use different browser hostnames deliberately. Ports alone do
not isolate cookies, and SameSite is not an origin boundary. Local TLS terminates for both entry
points using ignored development material. Production validation
requires distinct HTTPS origins, and operations must place them on different registrable sites
where firm DNS permits.

All tables owned by this deployment use `TB_TA_AGORA_<CORE_TABLE>`. The prefix identifies the
Agora project, while the suffix preserves the table's role: examples include
`TB_TA_AGORA_USER`, `TB_TA_AGORA_DASHBOARD`, `TB_TA_AGORA_DJANGO_MIGRATIONS`, and
`TB_TA_AGORA_AUTH_PERMISSION`. The `persistence` name remains an internal Django app label only;
it does not appear in the physical Oracle namespace.

## Trust-boundary rules

| Boundary | Trusted inputs | Hostile inputs | Non-negotiable rule |
|---|---|---|---|
| Portal | Validated configuration and authenticated principal | All browser input and uploaded bytes | Never return uploaded HTML, mark it safe, insert it into DOM/`srcdoc`, or create same-origin `blob:`/`data:` documents from it. |
| Identity | Administrator-provisioned canonical User record | Login fields, form SOEIDs, identity-like headers | Normalize once; application policies consume an immutable authenticated principal, never a request-selected identity. |
| Policy/data | Constrained internal identifiers and database constraints | Route IDs, filenames, publication/Grant claims | Authorize before resolving metadata or artifacts; opaque IDs add no authority. |
| Content | Exact portal origin and content-scoped render authorization | Uploaded dashboard-package bytes and every request parameter | GET/HEAD-only artifact gateway, no portal sessions, mutations, templates, administration, or general APIs. |
| Storage | Generated internal keys and adapter-owned absolute root | User filenames and artifact bytes | Storage keys never derive from filenames; artifacts remain outside public/static roots and are accessible only through metadata plus policy. |

Hostile Revisions must not share ambient credentials or durable browser storage. The AG-007
baseline therefore uses an opaque sandboxed origin. Any proposal to grant `allow-same-origin`
must first provide per-Dashboard/per-Revision origin isolation or an independently reviewed
equivalent. CSP is defense in depth, not a complete browser network firewall; AG-007 owns
supported-browser tests and any additional egress controls required by the deployment.

The source tree encodes the split with distinct settings, URLconfs, ASGI applications, and
middleware. The WSGI callables remain compatibility/diagnostic interfaces only; the supported
production runtime is the proxy-hardened ASGI path documented in [`deployment.md`](./deployment.md).
Both compositions load the shared core-domain models, but the content composition
has no sessions, templates, mutation routes, or portal middleware. Each composition exposes only
`/health/live/` and `/health/ready/` as its unauthenticated operational probes. Liveness is a
constant response with no Oracle or storage dependency; readiness single-flights the database
probe and returns only generic status text. The managed connection profile must bound Oracle
connect/ping time. The content composition has no other unauthenticated
non-artifact route: its exact renderer routes are GET/HEAD-only and every unmatched path returns
404 with a default-deny CSP. Health probes never read or return uploaded bytes. The portal host
allowlist excludes the content hostname.

Portal-issued render credentials are 256-bit random values stored only as SHA-256 digests. They
expire after five minutes and bind viewer, current authentication version, Dashboard, Revision,
audience, and current authorization state. The content process rechecks expiry, revocation,
active-user state, ownership or Viewer Grant, publication pointer, and artifact scope for every
package request. The local content launcher disables access logging because the short-lived
bearer appears in the iframe path; production proxies must apply equivalent path redaction.
Because `sandbox="allow-scripts"` gives uploaded HTML an opaque origin, only an already-authorized
Supporting Artifact response may opt into the exact `Access-Control-Allow-Origin: null` value. It
also varies on `Origin`, never enables credentials or a wildcard, and grants nothing to HTML, failures, or
non-null origins.

## Module ownership

```text
src/agora/config.py          typed shared startup contract
src/agora/db/                Django Oracle adapter for treasury_analytics.TAConnection
src/agora/settings/base.py   non-browser shared settings
src/agora/settings/portal.py trusted portal composition
src/agora/settings/content.py isolated content composition
src/agora/urls/portal.py     trusted UI/API routes only
src/agora/urls/content.py    exact health probes plus authorized package routes and catch-all 404
src/agora/health.py          dependency-safe liveness/readiness responses
src/agora/rendering/         render authorization, delivery, CSP, and sandbox policy
src/agora/portal/            trusted project, upload, preview, identity, and admin UI
src/agora/core/              constrained metadata, domain services, migrations, private storage
packages/treasury-analytics/ local development implementation of the managed connection API
```

Future domain code belongs behind a shared policy/service layer rather than in views. Portal
and content may call the same policy functions, but content must not import portal views,
session middleware, or templates.

## Project access and scale boundary

`Dashboard` is the authorization boundary. The owner has implicit management rights; every other
capability comes from an explicit retained `ViewerGrant` scoped to `(dashboard_id, viewer_id)`.
An unrevoked grant is sufficient only when its viewer is active and for the exact pinned Published
Revision.
Administrators do not receive implicit Dashboard content access. Grant and revoke mutations,
portal metadata, Shared with Me, render-token issuance, and content package delivery all call
the same default-deny project policy, with generic not-found behavior for unrelated projects.

Grant rows are retained epochs: revocation closes an epoch and a later regrant creates a new row.
The active-only uniqueness invariant allows at most one open epoch for a Dashboard/viewer pair
without destroying audit history. Active-grant checks and viewer-to-project discovery are
index-backed. Portal collections use signed, scope-bound keyset cursors and fetch at most 25 rows
plus one navigation sentinel; navigation does not issue full counts or deep offsets. Project
detail omits decorative Viewer counts, and artifact metadata is fetched only after a revision page
is bounded. The scale workload and release gates are recorded in
[`docs/scaling.md`](scaling.md), and remain unverified until an Oracle-backed staging run.

AG-002 coordinates Oracle and the filesystem through durable `StorageReservation` rows.
Artifact bytes are streamed, fsynced, read-back verified, and installed without clobbering before
a short outermost metadata transaction exposes a complete Revision. A known rollback deletes
only bytes proven to belong to that attempt; compensation locks the reservation before resolving
an ambiguous commit, and a crash leaves a reservation that the bounded cleanup command can
reconcile. Durable reservation states distinguish verified ownership, collision preservation, and
an unwitnessed write outcome; the last is retained rather than guessed. The database and filesystem
are not presented as one impossible distributed transaction.

Dashboard rows retain `first_published_at` so restore can deterministically distinguish never-
published Drafts from previously Published Dashboards. Model guards and Oracle triggers enforce
the lifecycle transition graph, terminal Deleted/read-only Archived behavior, active-owner
Revision creation, same-Dashboard publication/latest pointers, and complete immutable Revision
sets. Later tickets add the user-facing lifecycle and publication workflows; AG-002 provides their
durable boundary only.

## Enhancement domain and analytics boundaries

Migrations `0012` through `0014` are one linear forward chain. They add compact navigation and
workflow state, make ownership an explicit audited transfer, and establish the single
Authorized Open metric without changing the portal/content origin split:

- `DashboardTag`, `DashboardFavorite`, and `DashboardViewerState` support bounded navigation.
  New indicators compare the compact seen Publication Version with the Dashboard's current
  version; they never scan render or audit history.
- `AccessRequest` retains one lifecycle row per Dashboard/requester. Owner reads are scoped to one
  exact currently owned Dashboard and use a deterministic keyset index, avoiding a join-and-sort
  across every Dashboard an owner has.
- Publication metadata and `stale_after` live on Dashboard. Freshness is derived at read time;
  there is no clock-driven `is_stale` update.
- `DashboardOwnershipTransfer` is an append-only chain. Transfer, revision, and Grant mutations
  all lock Dashboard before User, while Oracle triggers authorize new writes against the current
  owner. Historical actor fields are never rewritten.
- A Published Viewer `RenderAuthorization` snapshots Publication Version and synchronously emits
  one idempotent raw `AuthorizedOpen`. Bounded background calls maintain daily, viewer, and
  Dashboard rollups and a monotonic checkpoint. Only aggregate query interfaces are available to
  portal/admin code; raw rows have a guarded 90-day retention boundary.

These are typed domain and query seams for later server-rendered pages, not a new service tier.
No SPA, cache, queue, or synchronous popularity counter is introduced.

This prerequisite lane intentionally does not retrofit the existing portal's numbered project,
Grant, Revision, or administrator lists. Their separately completed bounded-read change must be
integrated after this migration chain before the new bounded-work contract is treated as a release
claim.

## Runtime and stack

| Concern | Selected baseline | Support policy |
|---|---|---|
| Language | CPython 3.14.7 | Exactly pinned for development/CI; application supports latest `3.14.x`, through `<3.15`. Python 3.14 is in bugfix support through 2030-10. |
| Backend/UI | Django 5.2.17 LTS with server-rendered templates and committed CSS | `>=5.2.17,<5.3`, exact transitive resolution in `uv.lock`; security support through 2028-04. |
| Metadata DB | Oracle + `python-oracledb` 4 through `treasury_analytics.TAConnection` | The selected package profile owns coordinates; the process supplies `ENV` and `TA_<ENV>_PASSWORD`. No SQLite substitute. |
| Migrations | Django ORM migrations | Schema changes require checked-in forward migrations and explicit reversal/rollback consideration. |
| Dependencies | uv 0.12.6 universal lock | `uv.lock` is committed; CI uses `--locked` and `uv lock --check`. |
| Static quality | Ruff 0.16, mypy 2 + django-stubs | Format, lint, and strict type checks are blocking. Runtime tests compensate for stub/framework version skew. |
| Tests | pytest 9, pytest-django, pytest-cov | Branch coverage is blocking; Oracle connection and both service boundaries have smoke coverage. |
| CI | GitHub Actions | Read-only token, SHA-pinned actions, locked install, Oracle-capable self-hosted runner, full quality gate, bounded timeout. |

No Node runtime, SPA framework, bundler, task queue, cache, object store, or microservice split is
justified by AG-001. Browser test engines arrive with the renderer workflow rather than as idle
dependencies.

## Primary technical references

- [Python version status](https://devguide.python.org/versions/)
- [Python 3.14.7 release line](https://www.python.org/doc/versions/)
- [Django 5.2 LTS release notes](https://docs.djangoproject.com/en/5.2/releases/5.2/)
- [Django supported versions](https://www.djangoproject.com/download/)
- [Django database support](https://docs.djangoproject.com/en/5.2/ref/databases/)
- [Django migrations](https://docs.djangoproject.com/en/5.2/topics/migrations/)
- [python-oracledb documentation](https://python-oracledb.readthedocs.io/)
- [uv project locking](https://docs.astral.sh/uv/concepts/projects/sync/)
- [GitHub Actions secure use](https://docs.github.com/en/actions/security-guides/security-hardening-for-github-actions)
- [RFC 6454 web origin model](https://www.rfc-editor.org/rfc/rfc6454)
- [Django user-uploaded content guidance](https://docs.djangoproject.com/en/5.2/topics/security/#user-uploaded-content)

The database portion of ADR 0001 is superseded by ADR 0002. ECS topology, production high
availability, reverse-proxy SSO, and final production packaging remain explicitly out of scope.
