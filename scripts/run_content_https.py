"""Run the isolated content service over local HTTPS with development autoreload."""

from __future__ import annotations

import os
from pathlib import Path

from django.conf import settings
from django.core.asgi import get_asgi_application

from agora.development import run_development_server

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TLS_ROOT = PROJECT_ROOT / ".local" / "tls"
CERTIFICATE_PATH = TLS_ROOT / "localhost.pem"
PRIVATE_KEY_PATH = TLS_ROOT / "localhost.key"
PORTAL_HTTPS_ORIGIN = "https://localhost:8443"
CONTENT_HTTPS_ORIGIN = "https://127.0.0.1:8444"

os.environ["AGORA_PORTAL_ORIGIN"] = PORTAL_HTTPS_ORIGIN
os.environ["AGORA_CONTENT_ORIGIN"] = CONTENT_HTTPS_ORIGIN
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "agora.settings.content")

application = get_asgi_application()


def main() -> None:
    """Start the cookie-free content service after validating local TLS material."""
    if settings.AGORA_ENVIRONMENT != "development":
        raise SystemExit("The local HTTPS content server is available only in development.")

    missing = [path.name for path in (CERTIFICATE_PATH, PRIVATE_KEY_PATH) if not path.is_file()]
    if missing:
        names = ", ".join(missing)
        raise SystemExit(f"Missing local TLS file(s): {names}. Bootstrap local TLS first.")

    run_development_server(
        "run_content_https:application",
        app_dir=PROJECT_ROOT / "scripts",
        host="127.0.0.1",
        port=8444,
        reload_dirs=[PROJECT_ROOT / "src", PROJECT_ROOT / "scripts"],
        ssl_certfile=CERTIFICATE_PATH,
        ssl_keyfile=PRIVATE_KEY_PATH,
        access_log=False,
    )


if __name__ == "__main__":
    main()
