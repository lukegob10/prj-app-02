"""Trusted portal routes. Uploaded artifact delivery must never be added here."""

from django.urls import include, path

from agora.portal.views import (
    login_view,
    logout_view,
    project_access,
    project_create,
    project_detail,
    project_grant_revoke,
    project_preview,
    project_upload,
    project_view,
    user_create,
    user_disable,
    user_enable,
    user_list,
    user_reset_password,
)

urlpatterns = [
    path("", include("agora.urls.discovery")),
    path("login/", login_view, name="login"),
    path("logout/", logout_view, name="logout"),
    path("projects/new/", project_create, name="project-create"),
    path("projects/<uuid:project_id>/", project_detail, name="project-detail"),
    path("projects/<uuid:project_id>/access/", project_access, name="project-access"),
    path(
        "projects/<uuid:project_id>/access/<uuid:grant_id>/revoke/",
        project_grant_revoke,
        name="project-grant-revoke",
    ),
    path("projects/<uuid:project_id>/upload/", project_upload, name="project-upload"),
    path("projects/<uuid:project_id>/view/", project_view, name="project-view"),
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
