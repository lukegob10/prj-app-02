"""Local stand-in for the environment-installed Treasury Analytics package."""

from typing import Final

from .ta_connection import TAConnection

AGORA_DEVELOPMENT_STAND_IN: Final = True

__all__ = ["AGORA_DEVELOPMENT_STAND_IN", "TAConnection"]
