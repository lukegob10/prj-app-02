"""Trusted portal routes. Uploaded artifact delivery must never be added here."""

from django.urls import path

from agora.portal.views import home

urlpatterns = [path("", home, name="home")]
