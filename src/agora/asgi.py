"""ASGI entry point for the trusted portal."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "agora.settings.portal")

application = get_asgi_application()
