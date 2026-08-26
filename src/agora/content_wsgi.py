"""WSGI entry point for the isolated content service."""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "agora.settings.content")

application = get_wsgi_application()
