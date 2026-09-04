# Agora

Agora is a Django application for uploading, versioning, and sharing HTML dashboard packages.
It keeps the trusted portal and untrusted dashboard content on separate browser origins.

Django remains the application framework. Uvicorn is the only supported HTTP process and runs
the two Django ASGI entry points. Removing Django would require rebuilding authentication,
sessions, CSRF protection, forms, templates, the ORM, migrations, and the admin workflows; it
would not make container networking simpler.

## Repository

The maintained root is intentionally small:

- `src/agora/` — application code, settings, templates, static assets, and migrations
- `tests/` — unit, security, integration, and browser tests
- `database/` — the reviewed Oracle schema reference
- `docs/` — product, architecture, deployment, and threat-model documentation
- `Dockerfile` and `compose.yaml` — one image and two long-running services
- `pyproject.toml` and `uv.lock` — Python metadata and the locked public dependency graph

Generated output, uploaded artifacts, local environments, secrets, and the managed database
package do not belong in the repository.

## Prerequisites

- uv `>=0.12.6,<0.13`
- Python 3.12, 3.13, or 3.14; `.python-version` selects the latest 3.14 patch
- access to the approved `treasury-analytics` 0.1.1 wheel
- an Oracle profile and its `TA_<ENV>_PASSWORD`
- Docker Compose 2.24 or newer when using containers

`treasury-analytics` is not available from a repository-known package index, so it cannot yet be
part of the portable lock. Install that approved wheel after every exact sync:

```powershell
uv sync --locked --all-groups
uv pip install --python .venv --no-deps C:\approved\treasury_analytics-0.1.1-py3-none-any.whl
uv pip check --python .venv
uv run --no-sync python -c "import agora, treasury_analytics; print('environment ready')"
```

The `--no-sync` flag is deliberate: an exact uv sync removes packages absent from `uv.lock`.
The permanent fix is to publish the managed package to an authenticated private Python index,
then declare and lock it normally.

## Configure

Copy the example, generate two different secret keys, replace every placeholder, and use a private
absolute artifact path outside the repository.

```powershell
Copy-Item .env.example .env
uv run --no-sync python -c "import secrets; print(secrets.token_urlsafe(64))"
```

The local origins are intentionally different hosts:

- portal: `http://localhost:8000`
- content: `http://127.0.0.1:8001`

An origin is the exact URL used by the browser. It is never `0.0.0.0` and never a Compose service
name. `0.0.0.0` is only a server bind address. Keeping those concepts separate makes Django's
host and CSRF validation deterministic.

## Run locally

Start one process per terminal:

```powershell
uv run --no-sync python -m uvicorn agora.asgi:application --host 127.0.0.1 --port 8000 --reload --limit-concurrency 32
```

```powershell
uv run --no-sync python -m uvicorn agora.content_asgi:application --host 127.0.0.1 --port 8001 --reload --limit-concurrency 32 --no-access-log
```

Open `http://localhost:8000`. The content root returning 404 is expected; authorized render URLs
are issued by the portal. Portal static files are served by ASGI-native middleware, so local and
container runs do not require an Nginx sidecar.

## Run with containers

The image build requires a directory containing exactly one approved wheel and its independently
obtained SHA-256:

```powershell
$wheel = Get-Item C:\approved\treasury_analytics-0.1.1-py3-none-any.whl
$env:TREASURY_ANALYTICS_CONTEXT = $wheel.Directory.FullName
$env:TREASURY_ANALYTICS_WHEEL_SHA256 = (Get-FileHash -Algorithm SHA256 $wheel).Hash.ToLowerInvariant()

docker compose build
docker compose run --rm portal python manage.py migrate --noinput
docker compose run --rm portal python manage.py bootstrap_admin --soeid YOUR_SOEID
docker compose up -d --wait
```

Compose publishes only loopback ports and mounts one named artifact volume read/write in the
portal and read-only in the content service. See [Deployment](docs/deployment.md) before changing
origins, TLS, proxy trust, storage, or production secrets.

## Checks

Run non-database checks without allowing uv to remove the managed wheel:

```powershell
uv lock --check
uv run --no-sync ruff format --check .
uv run --no-sync ruff check .
uv run --no-sync mypy src tests
uv run --no-sync python -m compileall -q src tests
uv run --no-sync python manage.py check
uv run --no-sync python -m django check --settings=agora.settings.content
uv run --no-sync pytest -m "not django_db and not browser" --no-cov
uv build --no-build-isolation
docker compose config --quiet
```

Trusted pushes and manually dispatched GitHub Actions runs repeat these gates on Python 3.12,
3.13, and 3.14 and build the runtime image. Pull requests receive a secretless lock, formatting,
lint, and compilation job; untrusted pull-request code is never given the managed-wheel secrets.
Configure repository secrets `TREASURY_ANALYTICS_WHEEL_URL` and
`TREASURY_ANALYTICS_WHEEL_SHA256`; trusted CI fails closed when the managed build input is
unavailable. A protected pre-merge environment still needs to run the full test and Oracle gates.

Database-backed tests flush the configured Oracle schema. Run them only against a dedicated,
disposable test profile after all three safeguards are explicit. The profile acknowledgement must
match `ENV`; known production aliases are refused even when acknowledged:

```powershell
$env:AGORA_ENVIRONMENT = "test"
$env:ENV = "AGORA_TEST"
$env:TA_AGORA_TEST_PASSWORD = "SET_FROM_YOUR_SECRET_STORE"
$env:AGORA_TEST_DATABASE_PROFILE = "AGORA_TEST"
$env:AGORA_TEST_DATABASE_RESET_ALLOWED = "true"
uv run --no-sync pytest --browser chromium
```

## References

- [Architecture](docs/architecture.md)
- [Deployment](docs/deployment.md)
- [Product contract](docs/product-contract.md)
- [Threat model](docs/threat-model.md)
- [Oracle schema](database/README.md)
