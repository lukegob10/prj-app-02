"""Bounded, owner-scoped project query coverage."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from agora.persistence.models import Dashboard, Revision, User, ViewerGrant
from agora.persistence.projects import (
    manageable_project,
    owned_projects,
    project_active_grants,
    project_effective_viewer_count,
    project_grant_history,
    project_revisions,
    shared_projects,
    visible_project,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def owner() -> User:
    return User.objects.create_user("QUERY.OWNER")


@pytest.fixture
def second_owner() -> User:
    return User.objects.create_user("QUERY.OTHER")


@pytest.fixture
def viewer() -> User:
    return User.objects.create_user("QUERY.VIEWER")


@pytest.fixture
def disabled_viewer() -> User:
    user = User.objects.create_user("QUERY.DISABLED")
    user.is_active = False
    user.save(update_fields=("is_active",))
    return user


def dashboard(owner: User, name: str = "Query project") -> Dashboard:
    return Dashboard.objects.create(owner=owner, name=name)


def publish(project: Dashboard, owner: User, *, number: int = 1) -> Revision:
    revision = Revision.objects.create(dashboard=project, number=number, created_by=owner)
    project.latest_revision = revision
    project.published_revision = revision
    project.first_published_at = project.first_published_at or timezone.now()
    project.state = Dashboard.State.PUBLISHED
    project.save()
    return revision


def grant(
    project: Dashboard,
    owner: User,
    viewer: User,
    *,
    revoked_at: datetime | None = None,
    revoked_by: User | None = None,
) -> ViewerGrant:
    return ViewerGrant.objects.create(
        dashboard=project,
        viewer=viewer,
        created_by=owner,
        revoked_at=revoked_at,
        revoked_by=revoked_by,
    )


def test_detail_primitives_are_owner_scoped_and_bounded(
    owner: User,
    second_owner: User,
    viewer: User,
    disabled_viewer: User,
) -> None:
    project = dashboard(owner)
    other_project = dashboard(second_owner, "Other project")
    active = grant(project, owner, viewer)
    grant(project, owner, disabled_viewer)
    grant(
        project,
        owner,
        User.objects.create_user("QUERY.REVOKED"),
        revoked_at=timezone.now(),
        revoked_by=owner,
    )

    with CaptureQueriesContext(connection) as queries:
        resolved = manageable_project(project.id, owner.id)
    assert len(queries) == 1
    assert resolved is not None
    assert resolved.id == project.id
    assert not getattr(resolved, "_prefetched_objects_cache", {})

    with CaptureQueriesContext(connection) as queries:
        assert manageable_project(project.id, second_owner.id) is None
    assert len(queries) == 1
    assert manageable_project(other_project.id, owner.id) is None

    with CaptureQueriesContext(connection) as queries:
        count = project_effective_viewer_count(project.id, owner.id)
    assert len(queries) == 1
    assert count == 1
    assert active.viewer_id == viewer.id


def test_revision_query_is_lazy_newest_first_and_artifact_prefetch_is_caller_bounded(
    owner: User,
) -> None:
    project = dashboard(owner)
    first = publish(project, owner, number=1)
    second = publish(project, owner, number=2)

    revisions = project_revisions(project.id, owner.id)
    assert getattr(revisions, "_prefetch_related_lookups", ())
    with CaptureQueriesContext(connection) as queries:
        page = list(revisions[:1])
    assert len(queries) == 2
    assert [revision.id for revision in page] == [second.id]
    assert first.id != second.id

    with CaptureQueriesContext(connection) as queries:
        bounded_with_artifacts = list(revisions[1:2])
    assert len(queries) == 2
    assert [revision.number for revision in bounded_with_artifacts] == [1]


def test_active_and_history_queries_select_related_and_have_stable_epoch_order(
    owner: User,
    viewer: User,
) -> None:
    project = dashboard(owner)
    now = timezone.now()
    revoked_old = grant(
        project,
        owner,
        viewer,
        revoked_at=now - timedelta(days=1),
        revoked_by=owner,
    )
    active_new = grant(project, owner, viewer)
    another_viewer = User.objects.create_user("QUERY.ANOTHER")
    active_other = grant(project, owner, another_viewer)

    with CaptureQueriesContext(connection) as queries:
        active_rows = list(project_active_grants(project.id, owner.id))
        active_labels = [(row.viewer.soeid, row.created_by.soeid) for row in active_rows]
    expected_active = sorted(
        [active_new, active_other],
        key=lambda row: (row.created_at, row.id),
        reverse=True,
    )
    assert len(queries) == 1
    assert [row.id for row in active_rows] == [row.id for row in expected_active]
    assert active_labels == [(row.viewer.soeid, row.created_by.soeid) for row in expected_active]

    with CaptureQueriesContext(connection) as queries:
        history_rows = list(project_grant_history(project.id, owner.id))
        history_labels = []
        for row in history_rows:
            assert row.revoked_by is not None
            history_labels.append((row.viewer.soeid, row.created_by.soeid, row.revoked_by.soeid))
    assert len(queries) == 1
    assert [row.id for row in history_rows] == [revoked_old.id]
    assert history_labels == [("QUERY.VIEWER", "QUERY.OWNER", "QUERY.OWNER")]

    assert list(project_active_grants(project.id, viewer.id)) == []
    assert list(project_grant_history(project.id, viewer.id)) == []


def test_shared_with_me_is_duplicate_free_after_revoke_and_regrant(
    owner: User,
    viewer: User,
    disabled_viewer: User,
) -> None:
    project = dashboard(owner)
    publish(project, owner)
    old_epoch = grant(project, owner, viewer, revoked_at=timezone.now(), revoked_by=owner)
    new_epoch = grant(project, owner, viewer)
    grant(project, owner, disabled_viewer)

    with CaptureQueriesContext(connection) as queries:
        visible = list(shared_projects(viewer.id))
    assert len(queries) == 1
    assert [item.id for item in visible] == [project.id]
    assert visible_project(project.id, viewer.id) == (project, False)
    assert list(shared_projects(disabled_viewer.id)) == []
    assert old_epoch.id != new_epoch.id

    owner.is_active = False
    owner.save(update_fields=("is_active",))
    assert [item.id for item in shared_projects(viewer.id)] == [project.id]

    new_epoch.revoked_at = timezone.now()
    new_epoch.revoked_by = owner
    new_epoch.save(update_fields=("revoked_at", "revoked_by"))
    assert list(shared_projects(viewer.id)) == []


def test_owned_and_shared_project_lists_are_deterministic_and_scope_safe(
    owner: User,
    second_owner: User,
    viewer: User,
) -> None:
    first = dashboard(owner, "Same name")
    second = dashboard(owner, "Same name")
    shared = dashboard(second_owner, "Shared")
    publish(shared, second_owner)
    grant(shared, second_owner, viewer)

    # Make the tie-breakers explicit without changing the query shape.
    old = timezone.now() - timedelta(days=1)
    Dashboard.objects.filter(id=first.id).update(updated_at=old)
    Dashboard.objects.filter(id=second.id).update(updated_at=old)
    first.refresh_from_db()
    second.refresh_from_db()

    assert [item.id for item in owned_projects(owner.id)] == sorted(
        [first.id, second.id], key=lambda value: str(value)
    )
    assert [item.id for item in shared_projects(viewer.id)] == [shared.id]
    assert visible_project(shared.id, owner.id) is None
