"""Shared settings that do not blur portal and content responsibilities."""

import os
from pathlib import Path

from dotenv import load_dotenv

from agora.config import RuntimeConfig

BASE_DIR = Path(__file__).resolve().parents[3]
load_dotenv(BASE_DIR / ".env", override=False)
RUNTIME = RuntimeConfig.from_environ(os.environ)

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
SECURE_REFERRER_POLICY = "no-referrer"
SECURE_SSL_REDIRECT = RUNTIME.is_production

AGORA_ENVIRONMENT = RUNTIME.environment
AGORA_PORTAL_ORIGIN = RUNTIME.portal_origin.value
AGORA_CONTENT_ORIGIN = RUNTIME.content_origin.value
AGORA_ARTIFACT_ROOT = RUNTIME.artifact_root
AGORA_RENDER_AUTH_TTL_SECONDS = 5 * 60
