"""Shared settings that do not blur portal and content responsibilities."""

import os
from importlib.metadata import PackageNotFoundError, metadata
from pathlib import Path

import treasury_analytics
from dotenv import load_dotenv

from agora.config import RuntimeConfig, validate_treasury_package

BASE_DIR = Path(__file__).resolve().parents[3]
load_dotenv(BASE_DIR / ".env", override=False)
RUNTIME = RuntimeConfig.from_environ(os.environ)
try:
    _TREASURY_METADATA = metadata("treasury-analytics")
except PackageNotFoundError:
    _TREASURY_DISTRIBUTION_PRESENT = False
    _TREASURY_SUMMARY = ""
else:
    _TREASURY_DISTRIBUTION_PRESENT = True
    _TREASURY_SUMMARY = _TREASURY_METADATA.get("Summary", "")
validate_treasury_package(
    RUNTIME.environment,
    development_stand_in=bool(getattr(treasury_analytics, "AGORA_DEVELOPMENT_STAND_IN", False)),
    distribution_present=_TREASURY_DISTRIBUTION_PRESENT,
    distribution_summary=_TREASURY_SUMMARY,
)

DEBUG = RUNTIME.debug

DATABASES: dict[str, dict[str, object]] = {
    "default": {
        "ENGINE": "agora.db.backends.treasury_oracle",
        "NAME": RUNTIME.database.environment,
        "USER": RUNTIME.database.environment,
        "PASSWORD": "",
        "HOST": "",
        "PORT": "",
        "CONN_MAX_AGE": 0,
        "OPTIONS": {"environment": RUNTIME.database.environment},
        "TEST": {
            "CREATE_DB": False,
            "CREATE_USER": False,
        },
    }
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "persistence.User"

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
SECURE_HSTS_SECONDS = 365 * 24 * 60 * 60 if RUNTIME.is_production else 0
SECURE_REFERRER_POLICY = "no-referrer"
SECURE_SSL_REDIRECT = RUNTIME.is_production

# Agora owns the exact configured hosts, not every sibling under their registrable domains.
# Include-subdomains and preload therefore require a separate DNS-wide review and are not safe
# defaults for this application's exact configured hosts.
SILENCED_SYSTEM_CHECKS = ["security.W005", "security.W021"]

AGORA_ENVIRONMENT = RUNTIME.environment
AGORA_PORTAL_ORIGIN = RUNTIME.portal_origin.value
AGORA_CONTENT_ORIGIN = RUNTIME.content_origin.value
AGORA_ARTIFACT_ROOT = RUNTIME.artifact_root
AGORA_RENDER_AUTH_TTL_SECONDS = 5 * 60
