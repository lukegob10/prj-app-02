"""Foundation-only trusted portal views."""

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render


def home(request: HttpRequest) -> HttpResponse:
    """Render the runnable foundation without exposing later business features."""
    nav_items = ({"label": "Home", "url": "/", "current": True},)
    return render(
        request,
        "portal/home.html",
        {
            "content_origin": settings.AGORA_CONTENT_ORIGIN,
            "environment": settings.AGORA_ENVIRONMENT,
            "nav_items": nav_items,
        },
    )
