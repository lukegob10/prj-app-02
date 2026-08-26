"""Settings for the trusted portal UI and API origin."""

import os

from agora.config import load_service_secret
from agora.settings.base import *

SECRET_KEY = load_service_secret(os.environ, "portal")
ALLOWED_HOSTS = [RUNTIME.portal_origin.hostname]
ROOT_URLCONF = "agora.urls.portal"
WSGI_APPLICATION = "agora.wsgi.application"
ASGI_APPLICATION = "agora.asgi.application"

INSTALLED_APPS = [
    "django.contrib.staticfiles",
    "agora.persistence",
    "agora.portal",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "agora.middleware.PortalSecurityHeadersMiddleware",
]
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
            ],
        },
    }
]

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / ".local" / "static"

CSRF_COOKIE_DOMAIN = None
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SECURE = RUNTIME.is_production
CSRF_TRUSTED_ORIGINS = [RUNTIME.portal_origin.value]
SESSION_COOKIE_DOMAIN = None
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = RUNTIME.is_production
X_FRAME_OPTIONS = "DENY"
