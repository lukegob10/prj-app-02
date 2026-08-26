"""Fail-closed content routes; authorized artifact routes arrive in AG-007."""

from django.http import HttpRequest, HttpResponse, HttpResponseNotFound
from django.urls import re_path


def content_not_available(request: HttpRequest) -> HttpResponse:
    """Reject every content path until an authorized AG-007 route replaces this guard."""
    return HttpResponseNotFound()


urlpatterns = [re_path(r"^", content_not_available, name="content-not-available")]
