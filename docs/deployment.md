# Deployment

Agora has one supported runtime: one immutable image, two independently supervised Uvicorn
processes, and two different browser origins. Both processes run Django through ASGI. There is no
supported WSGI entry point and no bundled reverse proxy.

## The four addresses that must not be confused

| Concern | Example | Owner |
|---|---|---|
| Process bind | `0.0.0.0:8000` inside a container | Uvicorn |
| Browser portal origin | `https://portal.example.com` | `AGORA_PORTAL_ORIGIN` |
| Browser content origin | `https://content.exampleusercontent.com` | `AGORA_CONTENT_ORIGIN` |
| Trusted proxy peers | `10.20.0.0/24` | Uvicorn `FORWARDED_ALLOW_IPS` |

Only the browser origins belong in Django host, CSRF, cookie, CSP, and redirect decisions. A bind
address or Compose service name is not a browser origin. Agora rejects wildcard origins,
bind-all addresses, paths, trailing slashes, and explicit default ports.

Locally, use the exact example values: `http://localhost:8000` for the portal and
`http://127.0.0.1:8001` for content. The different hostnames keep portal cookies away from
untrusted dashboard code even though both resolve to the local machine.

The trusted portal uses `Referrer-Policy: same-origin` so native form submissions retain the
origin/referrer information required by Django's CSRF checks, without sending portal URLs to
other origins. Do not override portal responses with `no-referrer`: browsers then submit
ordinary forms with `Origin: null`, which Django correctly rejects. Uploaded content responses
and their iframe elements retain `no-referrer`; never add `null` to the portal's trusted origins
or disable CSRF checks to work around a header mismatch.

## Build contract

The repository lock contains the complete public dependency graph. The required corporate
`treasury-analytics` package has no repository-known private index, so it is a separate,
checksum-bound input during this transition.

The build requires:

1. an authenticated, approved source for `treasury_analytics-0.1.1-py3-none-any.whl`;
2. a directory containing exactly that one wheel;
3. its SHA-256 obtained independently from the artifact source; and
4. `TREASURY_ANALYTICS_CONTEXT` and `TREASURY_ANALYTICS_WHEEL_SHA256` in the build environment.

The Dockerfile verifies the checksum, version, production marker, distribution metadata, and
required `TAConnection` API. It installs locked public dependencies, collects versioned static
assets, and copies only the virtual environment, source package, management entry point, and
collected assets into the runtime stage. The final image runs as UID/GID 10001 with no Linux
capabilities and a read-only root filesystem under Compose.

Never put package-index credentials, Oracle credentials, `.env`, TLS keys, or the managed wheel
in the repository or a Docker build argument. Record the managed wheel SHA-256, both pinned base
image digests, source revision, and resulting image digest as release provenance.

The permanent dependency fix is an authenticated private PEP 503 package index. Once its real URL
and authentication contract exist, declare `treasury-analytics>=0.1.1,<0.2` in `pyproject.toml`,
bind it to an explicit uv index, regenerate `uv.lock`, and remove the transitional wheel mount.

## Local Compose lifecycle

Create `.env` from `.env.example`, replace every placeholder, then build as shown in the README.
Database migration is an explicit release operation rather than a side effect of every web
process start:

```powershell
docker compose run --rm portal python manage.py migrate --noinput
docker compose run --rm portal python manage.py bootstrap_admin --soeid YOUR_SOEID
docker compose up -d --wait
```

The Compose file has exactly two long-running services. Both use the same image:

- `portal` runs `agora.asgi:application` and exposes `127.0.0.1:8000`.
- `content` runs `agora.content_asgi:application` and exposes `127.0.0.1:8001` without access logs,
  because render paths contain short-lived bearer credentials.

The named artifact volume is read/write for portal and read-only for content. The health check is
TCP liveness only: it proves Uvicorn is listening, not that Oracle or artifact storage is ready.
Production orchestration needs a separately qualified readiness policy before routing traffic.

Useful operations are ordinary one-off portal commands, not extra Compose services:

```powershell
docker compose run --rm portal python manage.py check --deploy
docker compose run --rm portal python manage.py cleanup_artifact_reservations
docker compose run --rm portal python manage.py process_authorized_open_analytics
docker compose logs --tail 100 portal content
docker compose down
```

`docker compose down` preserves the named volume. Adding `--volumes` destroys it and is not a
normal shutdown operation.

Run exactly one scheduled analytics worker at least once per minute:

```powershell
docker compose run --rm portal python manage.py process_authorized_open_analytics --batch-size 500 --max-batches 20
```

Each transaction is bounded, each run has a hard batch ceiling, and retries are idempotent. Alert
when either `*_may_remain=true` repeats because that means the schedule is not keeping up with the
event backlog. Schedule `cleanup_artifact_reservations` separately and alert on retained cleanup
work. Agora does not embed a scheduler in either web process.

## Production edge contract

Production must use two distinct HTTPS hostnames. Set `AGORA_ENVIRONMENT=production`,
`AGORA_DEBUG=false`, the two exact HTTPS origins, an absolute artifact path, the Oracle profile,
and its matching password. Inject only `AGORA_PORTAL_SECRET_KEY` into portal and only
`AGORA_CONTENT_SECRET_KEY` into content.

Keep both Uvicorn ports private. The TLS edge must:

- route each hostname only to its matching service;
- preserve the original browser-visible `Host` header;
- remove client-supplied `Forwarded` and `X-Forwarded-*` headers;
- write its own `X-Forwarded-Proto: https` and `X-Forwarded-For`; and
- connect from an IP or bounded CIDR listed in `FORWARDED_ALLOW_IPS`; and
- disable request-target logging for the content virtual host, or redact the credential segment
  before any edge, APM, trace, or centralized-log ingestion.

The portal edge must reject bodies above a small framing allowance over Agora's 100 MiB aggregate
package limit, enforce request-header and body-read deadlines, and cap in-flight uploads. Do not
rely on Django's upload validation as the outer transport limit: ASGI may spool the request before
the view runs. Compose limits each container's `/tmp` to 256 MiB as defense in depth, not as the
primary admission policy.

Never set `FORWARDED_ALLOW_IPS=*`. Uvicorn, not Django, validates the direct proxy peer and
normalizes trusted forwarding data into the ASGI request scope. Django deliberately leaves
`SECURE_PROXY_SSL_HEADER` unset and never trusts `X-Forwarded-Host`. This prevents direct clients
from spoofing HTTPS while avoiding redirect loops behind a correctly configured edge.

Portal static files are collected during the image build and served by ASGI-native ServeStatic
middleware with content-hashed names and immutable caching. Uploaded dashboard artifacts are
private data and must never overlap the static root or be served by that middleware.

## Scaling, data, and rollback

The image defaults each Uvicorn worker to 32 concurrent connections. Set Uvicorn's standard
`WEB_CONCURRENCY` or raise `UVICORN_LIMIT_CONCURRENCY` only after measuring Oracle connection and
memory limits. Both services currently use `CONN_MAX_AGE=0`; more workers can increase connection
churn. Apply a stricter edge rate limit to random-token traffic on the content origin. A multi-node
deployment also requires storage whose atomic-write and durability behavior has been qualified for
the artifact workflow.

Back up Oracle metadata and artifact bytes as one recovery set. For rollback, retain the previous
image digest and configuration, drain both new services, start both old services, and verify both
origins and a static asset before restoring traffic. Do not automatically reverse database
migrations; use a forward fix unless a coordinated database-and-artifact restore has been
approved.
