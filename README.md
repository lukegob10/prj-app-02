# Agora

Agora is a Django application for uploading, versioning, and sharing HTML dashboard packages.
The portal handles identity and project management; a separate content service serves authorized
dashboard files without receiving portal sessions.

## Repository

The maintained surface is deliberately small:

- `src/agora/` — application code, settings, templates, static assets, and migrations
- `tests/` — application, security, storage, and browser tests
- `database/oracle/schema.sql` — readable Oracle DDL reference
- `docs/` — product contract, architecture, and threat model
- `manage.py` and `pyproject.toml` — Django and Python project configuration

Build output, deployment files, generated reports, local logs, and historical ticket copies do
not belong in this repository.

## Local setup

The repository uses the existing `.venv`. On Windows:

```powershell
.\.venv\Scripts\Activate.ps1
python -c "import agora, treasury_analytics; print('environment ready')"
```

`treasury_analytics` is installed directly in `.venv`; its duplicate source tree is not tracked
here. If the environment is rebuilt, obtain that package from its normal managed source before
installing Agora.

Copy `.env.example` to `.env`, generate two independent secret keys, and replace the remaining
placeholders. Keep `AGORA_ARTIFACT_ROOT` outside the repository.

```powershell
Copy-Item .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(64))"
python manage.py migrate --noinput
python manage.py bootstrap_admin --soeid YOUR_SOEID
```

The Oracle profile is selected by `ENV`; its password is read from `TA_<ENV>_PASSWORD`.

## Run locally

Start each service in an activated terminal:

```powershell
python manage.py runserver 127.0.0.1:8000
```

```powershell
python -m django runserver 127.0.0.1:8001 --settings=agora.settings.content
```

Open `http://localhost:8000`. Django reloads Python changes automatically; refresh the browser for
template or static-file changes. The content root intentionally returns 404 because content URLs
are issued only for authorized previews and published views.

Local HTTP uses ordinary host-only cookies. HTTPS configurations use secure `__Host-` cookies,
and production configuration rejects HTTP origins.

## Checks

Run tools directly from `.venv`:

```powershell
ruff format --check .
ruff check .
mypy src tests
python -m compileall -q src tests
python manage.py check
```

Database-backed tests flush the configured Oracle schema. Run the full suite only against a
dedicated disposable schema:

```powershell
$env:AGORA_ENVIRONMENT = "test"
$env:AGORA_TEST_DATABASE_RESET_ALLOWED = "true"
pytest --browser chromium
```

Install Chromium once with `python -m playwright install chromium` if the browser suite needs it.

## References

- [Product contract](docs/product-contract.md)
- [Architecture](docs/architecture.md)
- [Threat model](docs/threat-model.md)
- [Oracle schema](database/README.md)
