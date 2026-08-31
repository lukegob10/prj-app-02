"""Django checks for keeping private artifacts outside served roots."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from django.apps import apps
from django.conf import settings
from django.core.checks import Error, Tags, register


def _overlaps(first: Path, second: Path) -> bool:
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)


@register(Tags.security)
def private_artifact_root_check(app_configs: Any, **kwargs: Any) -> list[Error]:
    del app_configs, kwargs
    artifact_root = Path(settings.AGORA_ARTIFACT_ROOT).resolve(strict=False)
    candidates: list[Path] = []
    static_root = getattr(settings, "STATIC_ROOT", None)
    media_root = getattr(settings, "MEDIA_ROOT", None)
    if static_root:
        candidates.append(Path(static_root).resolve(strict=False))
    if media_root:
        candidates.append(Path(media_root).resolve(strict=False))
    candidates.extend(
        Path(path).resolve(strict=False) for path in getattr(settings, "STATICFILES_DIRS", ())
    )
    candidates.extend(
        (Path(app_config.path) / "static").resolve(strict=False)
        for app_config in apps.get_app_configs()
        if (Path(app_config.path) / "static").is_dir()
    )
    if any(_overlaps(artifact_root, public_root) for public_root in candidates):
        return [
            Error(
                "AGORA_ARTIFACT_ROOT must not overlap a static or media root.",
                id="agora.E001",
            )
        ]
    return []
