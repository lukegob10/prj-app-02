"""Compatibility/diagnostic WSGI entry point; production uses hardened ASGI."""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "agora.settings.content")

application = get_wsgi_application()
