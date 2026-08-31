# Deployment and operations

This is Agora's single supported production handoff: one immutable image, one independently
supervised portal service, one independently supervised content service, one trusted TLS/static
edge, Oracle metadata, and private artifact storage. Portal and content use different HTTPS
origins and the image's canonical `deploy/entrypoint.py` entry point. The WSGI callables are
compatibility/diagnostic interfaces, not an equivalent production proxy path.

## Build the release image

The release pipeline must stage the managed `treasury-analytics` wheel inside the ignored
`managed/` build-context directory and calculate its SHA-256 locally. The wheel and digest are
build inputs, not credentials; never pass a package-index credential, Oracle password, `.env`, or
TLS private key as a build argument. The release system must authenticate the private artifact's
source before staging it; the digest then binds the build to those exact bytes.

```sh
umask 077
install -d managed
cp -- "$PRIVATE_RELEASE_DIR/treasury_analytics-0.1.1-py3-none-any.whl" managed/
TA_WHEEL=managed/treasury_analytics-0.1.1-py3-none-any.whl
TA_SHA256="$(sha256sum -- "$TA_WHEEL" | cut -d ' ' -f 1)"

uv lock --check
docker build --pull \
  --build-arg "TREASURY_ANALYTICS_WHEEL=$TA_WHEEL" \
  --build-arg "TREASURY_ANALYTICS_WHEEL_SHA256=$TA_SHA256" \
  --tag "registry.example/agora:$VERSION" .
unset TA_SHA256
docker push "registry.example/agora:$VERSION"
```

`uv.lock` remains the only dependency authority. The throwaway builder copies the local
`packages/treasury-analytics/pyproject.toml` only because uv must read that path-source metadata
to validate the existing lock. It copies no local `treasury_analytics/` source. The builder then:

1. installs locked non-local, non-development dependencies;
2. builds and installs the Agora wheel without dependency re-resolution;
3. verifies the managed wheel digest and installs that wheel without dependency re-resolution;
4. rejects the development marker or stand-in metadata, verifies `TAConnection`, and runs
   `uv pip check` to enforce the managed package's version/dependency contract.

A missing wheel, missing or mismatched digest, development stand-in, incompatible version, missing
API, or inconsistent dependency set fails the build. The final stage receives the clean virtual
environment, `manage.py`, and `deploy/`; it receives neither `src/`, general `scripts/`, local
package metadata/source, nor the managed wheel file. It runs as numeric UID/GID 10001 and declares
only `/var/lib/agora/artifacts` as a volume.

## Runtime commands and configuration

The image `ENTRYPOINT` already invokes `deploy/entrypoint.py`. Configure workload arguments
exactly as follows; do not launch Uvicorn or Django management commands in parallel paths.

| Workload | Command arguments | Purpose |
|---|---|---|
| Release job | `portal prepare` | Production checks, one-shot migrations, then `collectstatic`. |
| Portal service | `portal serve` | Trusted portal ASGI service. |
| Content service | `content serve` | Isolated content ASGI service with request-path access logging disabled. |

Every serve startup runs Django checks before replacing the entrypoint process with Uvicorn.
Production warnings fail startup. Each origin is an independently supervised service with a
bounded configured worker count; it is not required to be a single OS process.

In addition to the application variables in [`configuration.md`](./configuration.md), the
container runtime contract is:

| Variable | Workload | Contract |
|---|---|---|
| `AGORA_STATIC_ROOT` | Portal prepare | Image default `/var/lib/agora/static`; absolute release static mount. |
| `AGORA_FORWARDED_ALLOW_IPS` | Both serve services | Required comma-separated numeric proxy IPs/CIDRs. Hostnames, `*`, and `/0` networks are rejected. |
| `AGORA_WORKERS` | Both serve services | Optional integer `1` through `32`; default `1`. Size against Oracle and memory capacity. |
| `AGORA_BIND_HOST` | Both serve services | Optional bind address; default `0.0.0.0`. |
| `PORT` | Both serve services | Optional port `1` through `65535`; default `8000`. |

Inject `AGORA_ENVIRONMENT=production`, `AGORA_DEBUG=false`, both normalized origins, `ENV`, the
matching `TA_<ENV>_PASSWORD`, and `AGORA_ARTIFACT_ROOT`. Give portal only
`AGORA_PORTAL_SECRET_KEY` and content only `AGORA_CONTENT_SECRET_KEY`. Inject values from the
platform's configuration/secret store; do not copy an env file into the image or put secrets on a
command line.

## Collected static ownership

Static files are a release asset, not application-private data and not content-origin data. Use a
new, explicitly named static volume for each release, for example `agora-static-$VERSION`:

- mount it at `/var/lib/agora/static` read/write only in the `portal prepare` job;
- after prepare succeeds, mount that same volume read-only in the trusted portal reverse
  proxy/static adapter and serve `/static/` only on the portal origin;
- do not mount it in the portal Uvicorn service; and
- never mount or expose it in the content service.

The Dockerfile deliberately does not declare a static `VOLUME`, because that would attach storage
to content when the shared image is used there. A fresh release must complete `portal prepare`
successfully before the edge switches traffic. The release-checkout smoke validator proves that
the edge serves `/static/portal/foundation.css`; a successful `collectstatic` job without the
shared named mount is therefore not deployable.

## TLS and proxy trust

Only the trusted edge is public. Keep both application ports unreachable from client networks.
Set `AGORA_FORWARDED_ALLOW_IPS` to the actual edge peer addresses or bounded networks. The edge
must remove every client-supplied `Forwarded`/`X-Forwarded-*` value, write its own forwarding
headers, and send `X-Forwarded-Proto: https` for TLS requests.

Production sets Django's `SECURE_PROXY_SSL_HEADER` and `SECURE_SSL_REDIRECT`. Uvicorn accepts proxy
headers only from the numeric allowlist and normalizes the trusted scheme into the ASGI scope;
Agora's ASGI wrapper then removes every raw `X-Forwarded-Proto` header before Django evaluates the
request. This prevents redirect loops behind the trusted edge without allowing a direct client to
spoof HTTPS. Do not use the unsanitized WSGI compatibility callable as a production proxy entry
point.

Use distinct origins such as `https://portal.agora.example` and
`https://content.agorausercontent.example`; never rely on ports or a shared cookie domain for the
hostile-content boundary. The edge must route each hostname only to its matching service.

## Health and predeploy smoke

Both services expose exact unauthenticated `GET`/`HEAD` routes `/health/live/` and
`/health/ready/`. They are the content origin's only new public exceptions. Restrict them at the
edge to the internal load-balancer/probe network. Liveness is dependency-free. Readiness checks
Oracle plus artifact access (portal read/write, content read-only), returns only `ready` or
`not ready`, and is never cached. A process-local single-flight guard rejects concurrent database
probes; the managed Treasury package and Oracle network profile must still enforce a finite
connect/ping timeout.

After both services and the portal static adapter are running, execute the checkout/CI tool from a
release checkout, never from inside the runtime image:

```sh
uv run --locked python scripts/smoke_deploy.py \
  --portal-origin "$AGORA_PORTAL_ORIGIN" \
  --content-origin "$AGORA_CONTENT_ORIGIN" \
  --timeout 5
```

The five checks cover both origins' liveness/readiness plus portal static delivery. The validator
requires canonical HTTPS origins with different hostnames, follows no redirects, sends no
credentials, bounds response reads, and requires exact health bodies. Do not switch user traffic
until all checks pass.

## Private artifact volume

Mount `/var/lib/agora/artifacts` explicitly at the matching absolute `AGORA_ARTIFACT_ROOT`: read/
write for both the portal prepare job and portal service, and read-only for content. Docker's
`VOLUME` declaration cannot express modes, so the platform manifest must. Never mount artifacts
into the proxy or expose them as static/media. Use the same numeric service identity or qualified
ACL arrangement while preserving requested directory/file modes `0700`/`0600`.

Qualify the filesystem for same-directory hard links, atomic no-clobber creation, and required
durability. Back up Oracle metadata and artifact bytes as one recovery set. The edge and platform
must redact full content render URIs because their paths contain short-lived bearer values; never
log authorization headers or bodies.

## Rollback

Retain the prior image digest, configuration revision, release-scoped static volume, and a
coordinated Oracle/artifact recovery point. Drain both new services, restore the previous edge
static mount, start both origins on the prior image/configuration, and rerun all five smoke checks.
Use the prior image only when its schema contract is forward-compatible. Do not automatically
reverse migrations; otherwise apply an approved forward fix or restore Oracle and artifacts
together. Previously streamed render bytes cannot be recalled, so revoke or rotate authorization
state under the incident procedure when credentials may have escaped.
