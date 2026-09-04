"""Aggregate authorized opens and enforce their bounded raw-event retention."""

from datetime import timedelta
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from agora.core.analytics import (
    AUTHORIZED_OPEN_RETENTION_DAYS,
    DEFAULT_ANALYTICS_BATCH_SIZE,
    MAX_ANALYTICS_BATCH_SIZE,
    aggregate_authorized_opens,
    purge_authorized_opens,
)

MAX_JOB_BATCHES = 100


class Command(BaseCommand):
    help = "Process bounded authorized-open rollup and retention batches."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--batch-size",
            type=int,
            default=DEFAULT_ANALYTICS_BATCH_SIZE,
            help="Rows per transaction (maximum 1000).",
        )
        parser.add_argument(
            "--max-batches",
            type=int,
            default=20,
            help=f"Maximum transactions per phase (maximum {MAX_JOB_BATCHES}).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        batch_size = options["batch_size"]
        max_batches = options["max_batches"]
        if isinstance(batch_size, bool) or not isinstance(batch_size, int):
            raise CommandError("batch-size must be an integer")
        if not 1 <= batch_size <= MAX_ANALYTICS_BATCH_SIZE:
            raise CommandError(f"batch-size must be between 1 and {MAX_ANALYTICS_BATCH_SIZE}")
        if isinstance(max_batches, bool) or not isinstance(max_batches, int):
            raise CommandError("max-batches must be an integer")
        if not 1 <= max_batches <= MAX_JOB_BATCHES:
            raise CommandError(f"max-batches must be between 1 and {MAX_JOB_BATCHES}")

        captured_at = timezone.now()
        processed = 0
        removed = 0
        aggregation_may_remain = False
        purge_may_remain = False
        try:
            for _ in range(max_batches):
                aggregation_result = aggregate_authorized_opens(
                    through=captured_at,
                    batch_size=batch_size,
                )
                processed += aggregation_result.processed
                aggregation_may_remain = aggregation_result.processed == batch_size
                if not aggregation_may_remain:
                    break

            retention_cutoff = captured_at - timedelta(days=AUTHORIZED_OPEN_RETENTION_DAYS)
            for _ in range(max_batches):
                purge_result = purge_authorized_opens(
                    before=retention_cutoff,
                    batch_size=batch_size,
                )
                removed += purge_result.removed
                purge_may_remain = purge_result.removed == batch_size
                if not purge_may_remain:
                    break
        except ValueError as error:
            raise CommandError(str(error)) from error

        self.stdout.write(
            self.style.SUCCESS(
                "Authorized-open analytics complete: "
                f"processed={processed} removed={removed} "
                f"aggregation_may_remain={str(aggregation_may_remain).lower()} "
                f"purge_may_remain={str(purge_may_remain).lower()}."
            )
        )
