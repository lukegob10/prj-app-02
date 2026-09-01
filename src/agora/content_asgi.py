"""ASGI entry point for the isolated content service."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "agora.settings.content")

application = get_asgi_application()
