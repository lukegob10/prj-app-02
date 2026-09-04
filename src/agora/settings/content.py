"""Settings for the isolated, read-only content origin."""

import os

from agora.config import load_service_secret
from agora.settings.base import *

SECRET_KEY = load_service_secret(os.environ, "content")
ALLOWED_HOSTS = [RUNTIME.content_origin.hostname]
ROOT_URLCONF = "agora.urls.content"
ASGI_APPLICATION = "agora.content_asgi.application"

INSTALLED_APPS = ["agora.core.apps.CoreConfig"]
# The content service reads existing tables but never owns migration operations. Disabling its
# migration graph also lets Django's standard runserver start without installing portal-only
# framework apps solely to satisfy historical migration dependencies.
MIGRATION_MODULES = {"persistence": None}
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "agora.middleware.ContentSecurityHeadersMiddleware",
]
TEMPLATES: list[dict[str, object]] = []
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "redact_content_request_target": {
            "()": "agora.log_redaction.ContentRequestTargetFilter",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "filters": ["redact_content_request_target"],
        }
    },
    "loggers": {
        "django.request": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        }
    },
}

# These Django warnings describe protections that would violate the content composition:
# CSRF middleware is unnecessary for a GET/HEAD-only service, and X-Frame-Options would block
# the exact cross-origin portal frame already constrained by response-enforced CSP.
SILENCED_SYSTEM_CHECKS = [*SILENCED_SYSTEM_CHECKS, "security.W002", "security.W003"]
