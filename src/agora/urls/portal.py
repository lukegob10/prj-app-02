"""Trusted portal routes. Uploaded artifact delivery must never be added here."""

from django.urls import include, path

from agora.health import liveness, portal_readiness
from agora.portal.development import development_reload_version
from agora.portal.views import (
    login_view,
    logout_view,
    project_create,
    project_detail,
    project_preview,
    project_rename,
    project_upload,
    user_create,
    user_disable,
    user_enable,
    user_list,
    user_reset_password,
)

urlpatterns = [
    path("health/live/", liveness, name="health-live"),
    path("health/ready/", portal_readiness, name="health-ready"),
    path("__dev__/reload/", development_reload_version, name="development-reload-version"),
    path("", include("agora.urls.discovery")),
    path("login/", login_view, name="login"),
    path("logout/", logout_view, name="logout"),
    path("projects/new/", project_create, name="project-create"),
    path("", include("agora.urls.stewardship")),
    path("projects/<uuid:project_id>/", project_detail, name="project-detail"),
    path("projects/<uuid:project_id>/rename/", project_rename, name="project-rename"),
    path("projects/<uuid:project_id>/upload/", project_upload, name="project-upload"),
    path(
        "projects/<uuid:project_id>/revisions/<uuid:revision_id>/preview/",
        project_preview,
        name="project-preview",
    ),
    path("admin/users/", user_list, name="admin-user-list"),
    path("admin/users/create/", user_create, name="admin-user-create"),
    path("admin/users/<uuid:user_id>/disable/", user_disable, name="admin-user-disable"),
    path("admin/users/<uuid:user_id>/enable/", user_enable, name="admin-user-enable"),
    path(
        "admin/users/<uuid:user_id>/reset-password/",
        user_reset_password,
        name="admin-user-reset-password",
    ),
]
