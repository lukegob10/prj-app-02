# Configuration contract

Agora reads process environment variables after loading an optional repository-root `.env`
without overriding values already present in the process. Shared values below are required by
both services; each service requires only its own Django secret. The local bootstrap creates
both secrets in one ignored development file. Startup reports all missing or malformed names
together, never their secret values.

| Variable | Secret | Validation |
|---|---:|---|
| `AGORA_ENVIRONMENT` | no | Exactly `development`, `test`, or `production`. |
| `AGORA_DEBUG` | no | Exactly `true` or `false`; production requires `false`. |
| `AGORA_PORTAL_SECRET_KEY` | yes | Portal process only. At least 50 characters; blank and known placeholders are rejected. Production must not inject it into the content process. |
| `AGORA_CONTENT_SECRET_KEY` | yes | Content process only. At least 50 characters, different from the portal key when both are present; blank and known placeholders are rejected. |
| `AGORA_PORTAL_ORIGIN` | no | Normalized `http[s]://host[:port]` only; no credentials, path, query, fragment, trailing slash, or whitespace. Production requires HTTPS. |
| `AGORA_CONTENT_ORIGIN` | no | Same format; hostname must differ from the portal hostname. Production requires HTTPS. |
| `AGORA_DB_NAME` | no | Non-empty PostgreSQL database name. |
| `AGORA_DB_USER` | no | Non-empty PostgreSQL role. |
| `AGORA_DB_PASSWORD` | yes | Non-empty; blank and known placeholders are rejected. |
| `AGORA_DB_HOST` | no | Non-empty database hostname/address. |
| `AGORA_DB_PORT` | no | Integer from 1 through 65535. |
| `AGORA_ARTIFACT_ROOT` | no | Absolute private filesystem path. It must not overlap any Django static/media root and has no web URL. |

There are no embedded runtime defaults for required values. `.env.example` contains rejected
placeholders, not credentials. `.env`, `.local/`, databases, build artifacts, and uploaded
content are ignored by Git.

## Safe local bootstrap

After installing locked dependencies, create `.env` once:

```powershell
uv run python scripts/bootstrap_env.py
```

The script uses the operating system's cryptographic random source, refuses to overwrite an
existing `.env`, and writes the artifact root as an absolute path. It never displays generated
values. On POSIX it restricts the resulting file to the owner.

To rotate local values, stop the services, move the existing `.env` to a secure temporary
location or delete it intentionally, then run the bootstrap again. Database credentials must
be rotated consistently with the existing local volume; the simplest disposable-development
path is to remove that Compose volume explicitly and recreate it.

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

Production deployment is not part of AG-001, but configuration already fails closed when:

- debug mode is enabled;
- either browser origin is not HTTPS;
- origins share a hostname;
- required values are blank or placeholders; or
- the artifact root is relative.

Production must additionally use different registrable sites where possible, host-only Secure
cookies, external secret injection, a private storage mount, current PostgreSQL minor updates,
and a reviewed TLS/proxy configuration. Those operator controls are verified in later tickets.

The startup security check treats configured static/media roots and every installed application's
discovered `static` directory as public boundaries; the artifact root cannot equal, contain, or sit
inside any of them.

The selected artifact filesystem must support same-directory hard links with atomic no-clobber
creation. Operators must restrict the root to the Agora service identity and qualify durability
on the actual local or mounted filesystem; network/virtual filesystems are not assumed to honor
local-filesystem atomicity. PostgreSQL must include ICU and its standard `und-x-icu` collation for
canonical artifact-name constraints. See [`storage.md`](./storage.md) for cleanup, backup, and
restore rules.
