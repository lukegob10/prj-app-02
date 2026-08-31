"""Reconcile expired private artifact write reservations."""

from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from agora.core.services import cleanup_expired_reservations
from agora.core.storage import ArtifactStorageError, FilesystemArtifactStorage


class Command(BaseCommand):
    help = "Idempotently clean expired, unowned private artifact writes."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--limit", type=int, default=100, help="Maximum reservations to inspect."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        limit = int(options["limit"])
        try:
            storage = FilesystemArtifactStorage(Path(settings.AGORA_ARTIFACT_ROOT))
            result = cleanup_expired_reservations(storage, limit=limit)
        except (ArtifactStorageError, ValueError) as error:
            raise CommandError(str(error)) from error
        self.stdout.write(
            self.style.SUCCESS(
                f"Artifact cleanup complete: removed={result.removed} retained={result.retained}."
            )
        )
