"""ASGI entry point for the trusted portal."""

import os

from django.core.asgi import get_asgi_application

from agora.service_entrypoint import configure_django_settings

configure_django_settings(os.environ, "agora.settings.portal")

application = get_asgi_application()
