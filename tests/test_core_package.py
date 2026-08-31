from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import cast

import pytest
from django.apps import apps
from django.db.migrations.loader import MigrationLoader

from agora.core.models import User


def test_core_is_the_canonical_package_and_old_path_is_absent() -> None:
    core_package = import_module("agora.core")
    core_path = Path(cast(str, core_package.__file__))

    assert core_path.parent.name == "core"
    assert not core_path.parent.parent.joinpath("persistence").exists()
    with pytest.raises(ModuleNotFoundError, match=r"agora\.persistence"):
        import_module("agora.persistence")


def test_persistence_app_identity_is_stable_after_package_rename() -> None:
    config = apps.get_app_config("persistence")

    assert config.name == "agora.core"
    assert config.label == "persistence"
    assert config.module is not None
    assert config.module.__name__ == "agora.core"
    assert apps.get_model("persistence", "User") is User
    assert User._meta.app_label == "persistence"


def test_persistence_migration_graph_uses_existing_label_and_core_modules() -> None:
    loader = MigrationLoader(None, replace_migrations=False)

    assert loader.migrations_module("persistence") == ("agora.core.migrations", False)
    assert loader.graph.leaf_nodes("persistence") == [
        ("persistence", "0017_flat_dashboard_artifact_package")
    ]
    core_migrations = {
        key: migration
        for key, migration in loader.disk_migrations.items()
        if key[0] == "persistence"
    }
    assert len(core_migrations) == 17
    assert all(
        migration.__module__.startswith("agora.core.migrations.")
        for migration in core_migrations.values()
    )
