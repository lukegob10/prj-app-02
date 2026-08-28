"""Oracle naming behavior for the explicit Agora table namespace."""

from __future__ import annotations

import re

from django.db.backends.oracle.operations import DatabaseOperations as OracleDatabaseOperations

_PROJECT_IDENTIFIER = re.compile(r"TB_TA_AGORA_[A-Z0-9_]+", flags=re.ASCII)


class DatabaseOperations(OracleDatabaseOperations):
    """Preserve modern Oracle identifiers in Agora's project-owned namespace."""

    def quote_name(self, name: str) -> str:
        normalized = name.upper()
        if _PROJECT_IDENTIFIER.fullmatch(normalized):
            return f'"{normalized}"'
        return super().quote_name(name)
