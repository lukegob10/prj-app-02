"""Project-scoped sharing, request, and ownership stewardship routes."""

from django.urls import path

from agora.portal.stewardship import (
    project_access_request_approve,
    project_access_request_decline,
    project_access_requests,
    project_transfer,
    project_transfer_confirm,
)
from agora.portal.views import project_access, project_grant_revoke, project_view

urlpatterns = [
    path("projects/<uuid:project_id>/view/", project_view, name="project-view"),
    path("projects/<uuid:project_id>/access/", project_access, name="project-access"),
    path(
        "projects/<uuid:project_id>/access/requests/",
        project_access_requests,
        name="project-access-requests",
    ),
    path(
        "projects/<uuid:project_id>/access/requests/<int:request_id>/approve/",
        project_access_request_approve,
        name="project-access-request-approve",
    ),
    path(
        "projects/<uuid:project_id>/access/requests/<int:request_id>/decline/",
        project_access_request_decline,
        name="project-access-request-decline",
    ),
    path(
        "projects/<uuid:project_id>/access/<uuid:grant_id>/revoke/",
        project_grant_revoke,
        name="project-grant-revoke",
    ),
    path(
        "projects/<uuid:project_id>/transfer/confirm/",
        project_transfer_confirm,
        name="project-transfer-confirm",
    ),
    path(
        "projects/<uuid:project_id>/transfer/",
        project_transfer,
        name="project-transfer",
    ),
]
