"""Settings for the isolated, read-only content origin."""

import os

from agora.config import load_service_secret
from agora.settings.base import *

SECRET_KEY = load_service_secret(os.environ, "content")
ALLOWED_HOSTS = [RUNTIME.content_origin.hostname]
ROOT_URLCONF = "agora.urls.content"
WSGI_APPLICATION = "agora.content_wsgi.application"
ASGI_APPLICATION = "agora.content_asgi.application"

INSTALLED_APPS = ["agora.persistence"]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "agora.middleware.ContentSecurityHeadersMiddleware",
]
TEMPLATES: list[dict[str, object]] = []

# These deploy warnings describe protections that would violate the content composition:
# CSRF middleware is unnecessary for a GET/HEAD-only service, and X-Frame-Options would block
# the exact cross-origin portal frame already constrained by response-enforced CSP.
SILENCED_SYSTEM_CHECKS = [*SILENCED_SYSTEM_CHECKS, "security.W002", "security.W003"]
