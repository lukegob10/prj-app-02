"""Password lookup for the local installed-package stand-in."""

from __future__ import annotations

import os
from types import MappingProxyType

from .profiles import get_profile


class CredentialManager:
    """Resolve package-owned coordinates plus one runtime password."""

    def __init__(self, env: str = "PROD", save_password: bool = False) -> None:
        self.env = (env or "").strip().upper()
        self.save_password = save_password
        self.config: MappingProxyType[str, object] = get_profile(self.env)

    def _get_credentials(self) -> tuple[str, str]:
        password_variable = f"TA_{self.env}_PASSWORD"
        password = os.getenv(password_variable)
        if not password:
            raise RuntimeError(f"Set {password_variable}")
        return str(self.config["username"]), password
