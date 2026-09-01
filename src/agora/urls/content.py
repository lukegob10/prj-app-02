"""Narrow, read-only routes for authorized isolated dashboard artifacts."""

from django.http import HttpRequest, HttpResponse, HttpResponseNotFound
from django.urls import path, re_path

from agora.core.models import RenderAuthorization
from agora.rendering.views import render_artifact, render_html


def content_not_available(request: HttpRequest) -> HttpResponse:
    """Reject every path not matched by the exact authorized renderer surface."""
    return HttpResponseNotFound()


urlpatterns = [
    path(
        "render/preview/<str:token>/",
        render_html,
        {"audience": RenderAuthorization.Audience.PREVIEW},
        name="render-preview-html",
    ),
    path(
        "render/preview/<str:token>/<str:logical_name>",
        render_artifact,
        {"audience": RenderAuthorization.Audience.PREVIEW},
        name="render-preview-artifact",
    ),
    # Keep the established route name available to clients while the implementation is
    # generic for CSV, CSS, IMAGE, and FONT artifacts.
    path(
        "render/preview/<str:token>/<str:logical_name>",
        render_artifact,
        {"audience": RenderAuthorization.Audience.PREVIEW},
        name="render-preview-csv",
    ),
    path(
        "render/viewer/<str:token>/",
        render_html,
        {"audience": RenderAuthorization.Audience.VIEWER},
        name="render-viewer-html",
    ),
    path(
        "render/viewer/<str:token>/<str:logical_name>",
        render_artifact,
        {"audience": RenderAuthorization.Audience.VIEWER},
        name="render-viewer-artifact",
    ),
    # Compatibility alias for the former CSV-only route name.
    path(
        "render/viewer/<str:token>/<str:logical_name>",
        render_artifact,
        {"audience": RenderAuthorization.Audience.VIEWER},
        name="render-viewer-csv",
    ),
    re_path(r"^", content_not_available, name="content-not-available"),
]
