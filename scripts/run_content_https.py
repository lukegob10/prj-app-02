"""Run the isolated content service over local HTTPS with development autoreload."""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from django.conf import settings
from django.core.asgi import get_asgi_application

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

    uvicorn.run(
        "run_content_https:application",
        app_dir=str(PROJECT_ROOT / "scripts"),
        host="127.0.0.1",
        port=8444,
        reload=True,
        reload_dirs=[str(PROJECT_ROOT / "src"), str(PROJECT_ROOT / "scripts")],
        ssl_certfile=str(CERTIFICATE_PATH),
        ssl_keyfile=str(PRIVATE_KEY_PATH),
        access_log=False,
    )


if __name__ == "__main__":
    main()
