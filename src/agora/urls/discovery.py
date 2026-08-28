"""Dashboard discovery routes kept separate from the legacy portal URL surface."""

from django.urls import path

from agora.portal.discovery_views import project_favorite, project_tags
from agora.portal.views import home, project_list

urlpatterns = [
    path("", home, name="home"),
    path("projects/", project_list, name="project-list"),
    path("projects/<uuid:project_id>/tags/", project_tags, name="project-tags"),
    path(
        "projects/<uuid:project_id>/favorite/",
        project_favorite,
        name="project-favorite",
    ),
]
