"""ASGI entry point for the trusted portal."""

import os
from typing import cast

from asgiref.typing import ASGI3Application
from django.core.asgi import get_asgi_application

from agora.proxy import ForwardedProtoSanitizer

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "agora.settings.portal")

application = ForwardedProtoSanitizer(cast(ASGI3Application, get_asgi_application()))
