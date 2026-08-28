"""Django Oracle backend using the environment-installed TAConnection boundary."""

from __future__ import annotations

import inspect
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.db.backends.oracle.base import DatabaseWrapper as OracleDatabaseWrapper
from django.utils.asyncio import async_unsafe
from treasury_analytics import TAConnection

from agora.db.backends.treasury_oracle.operations import DatabaseOperations
from agora.db.table_names import select_migration_recorder_table


@lru_cache(maxsize=1)
def treasury_dependency_path() -> Path:
    """Require Treasury Analytics to be installed inside the active environment."""

    package_path = Path(inspect.getfile(TAConnection)).resolve()
    environment_path = Path(sys.prefix).resolve()
    if environment_path not in package_path.parents:
        raise ImproperlyConfigured(
            "treasury-analytics must be installed in Agora's active .venv; "
            "run `uv sync --locked --all-groups`."
        )
    return package_path


class DatabaseWrapper(OracleDatabaseWrapper):
    """Use Django's Oracle behavior with package-owned connection coordinates."""

    ops_class = DatabaseOperations

    def get_connection_params(self) -> dict[str, str]:
        environment = self.settings_dict["OPTIONS"].get("environment")
        if not isinstance(environment, str) or not environment.strip():
            raise ImproperlyConfigured("Oracle connection option 'environment' is required")
        return {"environment": environment.strip().upper()}

    @async_unsafe
    def get_new_connection(self, conn_params: dict[str, Any]) -> Any:
        treasury_dependency_path()
        connection = TAConnection(str(conn_params["environment"])).connect()
        select_migration_recorder_table(connection)
        return connection
