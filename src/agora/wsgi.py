"""WSGI entry point for the trusted portal."""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "agora.settings.portal")

application = get_wsgi_application()
