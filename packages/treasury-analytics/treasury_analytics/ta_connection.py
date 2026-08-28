"""Synchronous Oracle connector with optional package-owned pooling."""

from __future__ import annotations

from typing import Any, ClassVar

import oracledb

from .credential_manager import CredentialManager


class TAConnection(CredentialManager):
    """Create direct or pooled Oracle connections for one managed environment."""

    __pool: ClassVar[Any | None] = None
    _pool_config: ClassVar[dict[str, Any]] = {
        "min": 1,
        "max": 3,
        "increment": 1,
        "timeout": 30,
        "getmode": oracledb.POOL_GETMODE_WAIT,
        "wait_timeout": 5000,
        "ping_interval": 60,
    }

    def __init__(
        self,
        env: str = "PROD",
        save_password: bool = False,
        pool_config: dict[str, Any] | None = None,
        use_pool: bool = False,
    ) -> None:
        super().__init__(env=env, save_password=save_password)
        self.use_pool = use_pool
        if pool_config:
            self._pool_config.update(pool_config)

    def connect(self) -> oracledb.Connection:
        """Acquire a pooled connection when requested, otherwise connect directly."""

        user, password = self._get_credentials()
        config = self.config
        dsn_tns = oracledb.makedsn(
            str(config["hostname"]),
            int(config["port"]),
            service_name=str(config["service_name"]),
        )

        if not self.use_pool:
            return oracledb.connect(user=user, password=password, dsn=dsn_tns)

        if TAConnection.__pool is None:
            TAConnection.__pool = oracledb.create_pool(
                user=user,
                password=password,
                dsn=dsn_tns,
                **self._pool_config,
            )
        return TAConnection.__pool.acquire()
