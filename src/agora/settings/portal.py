"""Settings for the trusted portal UI and API origin."""

import os
import tempfile
from pathlib import Path

from agora.config import load_portal_static_root, load_service_secret
from agora.rendering.security import PORTAL_REFERRER_POLICY
from agora.settings.base import *

SECRET_KEY = load_service_secret(os.environ, "portal")
ALLOWED_HOSTS = [RUNTIME.portal_origin.hostname]
ROOT_URLCONF = "agora.urls.portal"
ASGI_APPLICATION = "agora.asgi.application"

INSTALLED_APPS = [
    "servestatic",
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "agora.core.apps.CoreConfig",
    "agora.portal",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "servestatic.middleware.ServeStaticMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "agora.middleware.PortalSecurityHeadersMiddleware",
]

_PORTAL_TEMPLATE_SOURCES = (
    "django.template.loaders.filesystem.Loader",
    "django.template.loaders.app_directories.Loader",
)


def _portal_template_loaders(*, cache: bool) -> list[object]:
    """Keep production templates cached while making browser refresh truthful in development."""
    sources: list[object] = list(_PORTAL_TEMPLATE_SOURCES)
    if cache:
        return [("django.template.loaders.cached.Loader", sources)]
    return sources


TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": False,
        "OPTIONS": {
            "loaders": _portal_template_loaders(cache=not DEBUG),
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.messages.context_processors.messages",
                "agora.portal.context_processors.portal_shell",
            ],
        },
    }
]

STATIC_URL = "/static/"
STATIC_ROOT = load_portal_static_root(
    os.environ,
    environment=RUNTIME.environment,
    development_default=Path(tempfile.gettempdir()) / "agora" / "static",
)
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "servestatic.storage.CompressedManifestStaticFilesStorage"
            if RUNTIME.is_production
            else "django.contrib.staticfiles.storage.StaticFilesStorage"
        ),
    },
}
SERVESTATIC_USE_FINDERS = not RUNTIME.is_production
SERVESTATIC_USE_MANIFEST = RUNTIME.is_production
SERVESTATIC_USE_STATIC_ROOT = RUNTIME.is_production
# One HTML entry point plus at most 50 supporting files. Django enforces this while parsing the
# multipart request, before the package validator independently counts and stages every part.
DATA_UPLOAD_MAX_NUMBER_FILES = 51
_SECURE_PORTAL_COOKIES = RUNTIME.portal_origin.scheme == "https"
_PORTAL_COOKIE_PREFIX = "__Host-" if _SECURE_PORTAL_COOKIES else ""
CSRF_COOKIE_NAME = f"{_PORTAL_COOKIE_PREFIX}agora_csrf"
CSRF_COOKIE_DOMAIN = None
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_PATH = "/"
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = _SECURE_PORTAL_COOKIES
CSRF_TRUSTED_ORIGINS = [RUNTIME.portal_origin.value]
SECURE_REFERRER_POLICY = PORTAL_REFERRER_POLICY
SESSION_COOKIE_AGE = 8 * 60 * 60
SESSION_COOKIE_NAME = f"{_PORTAL_COOKIE_PREFIX}agora_session"
SESSION_COOKIE_DOMAIN = None
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_PATH = "/"
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = _SECURE_PORTAL_COOKIES
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "login"
AUTHENTICATION_BACKENDS = ["django.contrib.auth.backends.ModelBackend"]
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
        "OPTIONS": {"user_attributes": ["soeid"]},
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
X_FRAME_OPTIONS = "DENY"
