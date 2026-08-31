# Configuration contract

Agora reads process environment variables after loading an optional repository-root `.env`
without overriding values already present in the process. Shared values below are required by
both services unless the table says otherwise; each service requires only its own Django secret.
The local bootstrap creates both secrets in one ignored development file. Startup reports all
missing or malformed names together, never their secret values.

| Variable | Secret | Validation |
|---|---:|---|
| `AGORA_ENVIRONMENT` | no | Exactly `development`, `test`, or `production`. |
| `AGORA_DEBUG` | no | Exactly `true` or `false`; production requires `false`. |
| `AGORA_PORTAL_SECRET_KEY` | yes | Portal process only. At least 50 characters; blank and known placeholders are rejected. Production must not inject it into the content process. |
| `AGORA_CONTENT_SECRET_KEY` | yes | Content process only. At least 50 characters, different from the portal key when both are present; blank and known placeholders are rejected. |
| `AGORA_PORTAL_ORIGIN` | no | Normalized `http[s]://host[:port]` only; no credentials, path, query, fragment, trailing slash, or whitespace. Production requires HTTPS. |
| `AGORA_CONTENT_ORIGIN` | no | Same format; hostname must differ from the portal hostname. Production requires HTTPS. |
| `ENV` | no | Oracle profile selected by `treasury_analytics`; uppercase letters, digits, `_`, and `-` only. Local development uses `PROD`; a managed deployment may use `SDLC`. |
| `TA_<ENV>_PASSWORD` | yes | Password expected by the selected profile, such as `TA_PROD_PASSWORD` or `TA_SDLC_PASSWORD`; blank and known placeholders are rejected. |
| `AGORA_ARTIFACT_ROOT` | no | Absolute private filesystem path. It must not overlap any Django static/media root and has no web URL. |
| `AGORA_STATIC_ROOT` | no | Portal settings only. Production requires an absolute path; the image defaults it to `/var/lib/agora/static`. Mount the release static volume read/write only for `portal prepare` and read-only only for the trusted portal static adapter. Neither Uvicorn service mounts it. Development/test default to `.local/static`. |

There are no embedded runtime defaults for required values. `.env.example` contains rejected
placeholders, not credentials. `.env`, `.local/`, databases, build artifacts, and uploaded
content are ignored by Git.

The development-only `scripts/run_local.py` launchers override the two browser origins in their
own processes to `https://localhost:8443` and `https://127.0.0.1:8444`. They do not rewrite
`.env`. Render authorizations use a reviewed five-minute application constant; changing that
window is a security-contract change, not an unchecked environment toggle.

## Safe local bootstrap

After installing locked dependencies, create `.env` once:

```powershell
uv run --locked python scripts/bootstrap_env.py
```

The script uses the operating system's cryptographic random source, prompts for the local Oracle
password without echoing it, refuses to overwrite an existing `.env`, and writes the artifact
root as an absolute path. It never displays generated or supplied secret values. On POSIX it
restricts the resulting file to the owner.

To rotate local values, stop the services, move the existing `.env` to a secure temporary
location or delete it intentionally, then run the bootstrap again. Rotate the Oracle password
through the database's approved credential process and update the matching `TA_<ENV>_PASSWORD`.

## Test-only destructive acknowledgement

`AGORA_TEST_DATABASE_RESET_ALLOWED` is not application runtime configuration and is deliberately
absent from `RuntimeConfig`. Before database setup, the test harness requires both a raw process
value of `AGORA_ENVIRONMENT=test` and the acknowledgement's exact `true` value for any
database-bearing pytest selection. The canonical `scripts/check.py` command applies the same
preflight before its first subprocess, so its migration step cannot write first. Pytest then
verifies the resolved Django environment again before flushing the reused Oracle validation
schema. The local bootstrap leaves the acknowledgement `false`; enable it only after confirming
that the package-selected profile resolves to a disposable, dedicated test schema. Pure
non-database test selections remain available without the acknowledgement.

CI's `ENV=PROD` is the existing `treasury_analytics` profile identifier, not Agora's application
environment and not permission to touch production data. The CI reset opt-in is valid only while
that runner-local profile resolves to a dedicated disposable validation schema.

## Failure behavior

Missing or invalid configuration stops Django before serving requests. A typical redacted error
looks like:

```text
Agora configuration is invalid:
- AGORA_PORTAL_SECRET_KEY is required
- portal and content origins must use different hostnames
```

Errors list variable names and rules only. They do not echo passwords, secret keys, full
connection strings, cookies, or tokens.

## Production invariants

Production configuration fails closed when:

- debug mode is enabled;
- either browser origin is not HTTPS;
- origins share a hostname;
- required values are blank or placeholders; or
- the artifact root is relative;
- the portal static root is missing or relative; or
- the repository's development-only `treasury-analytics` stand-in is installed.

Production must additionally use different registrable sites where possible, host-only Secure
cookies, external secret injection, a private storage mount, a supported Oracle service and
driver, and a reviewed TLS/proxy configuration. Those operator controls are verified in later
tickets.

The startup security check treats configured static/media roots and every installed application's
discovered `static` directory as public boundaries; the artifact root cannot equal, contain, or sit
inside any of them. The supported container contract fixes `AGORA_STATIC_ROOT` at
`/var/lib/agora/static`; it is a separate release asset, never an artifact, portal-service, or
content-service mount. See [`deployment.md`](./deployment.md) for the release-scoped
prepare/proxy ownership contract.

The selected artifact filesystem must support same-directory hard links with atomic no-clobber
creation. Operators must restrict the root to the Agora service identity and qualify durability
on the actual local or mounted filesystem; network/virtual filesystems are not assumed to honor
local-filesystem atomicity. The application computes Unicode filename display and comparison
forms, while Oracle enforces composed/lowercase storage, comparison-key uniqueness, and the rest
of the relational invariants. See [`storage.md`](./storage.md) for cleanup, backup, and restore
rules.

## Oracle connection boundary

Agora passes only the normalized `ENV` value to `treasury_analytics.TAConnection`. The package
owns all non-secret connection coordinates and reads `TA_<ENV>_PASSWORD` from the process. The
repository contains a development implementation with the `PROD` profile; managed images replace
it with the corporate package exposing the same import and constructor, including profiles such
as `SDLC`. The local implementation carries an explicit development marker, and production
settings reject it during startup. Container environments inject both variables directly and do
not copy the local `.env` into the image.

The production ASGI runtime also requires `AGORA_FORWARDED_ALLOW_IPS` to identify the trusted edge
with numeric addresses or bounded CIDRs. The edge strips inbound forwarding headers and writes
`X-Forwarded-Proto: https`. Uvicorn normalizes that header only for an allowlisted peer, and the
ASGI wrapper removes the raw value before Django applies `SECURE_PROXY_SSL_HEADER`. WSGI remains a
compatibility/diagnostic interface and is not the supported production proxy path.
