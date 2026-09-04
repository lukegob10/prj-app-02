from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from agora.core.analytics import AnalyticsAggregationResult, AnalyticsPurgeResult


def test_analytics_command_drains_bounded_batches_with_one_cutoff(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    command = import_module("agora.core.management.commands.process_authorized_open_analytics")
    captured_at = datetime(2026, 9, 3, 12, tzinfo=UTC)
    aggregate_calls: list[dict[str, Any]] = []
    purge_calls: list[dict[str, Any]] = []
    aggregate_results = iter(
        (
            AnalyticsAggregationResult(processed=2, last_completed_open_id=2),
            AnalyticsAggregationResult(processed=1, last_completed_open_id=3),
        )
    )
    purge_results = iter((AnalyticsPurgeResult(removed=2), AnalyticsPurgeResult(removed=0)))

    def aggregate(**kwargs: Any) -> AnalyticsAggregationResult:
        aggregate_calls.append(kwargs)
        return next(aggregate_results)

    def purge(**kwargs: Any) -> AnalyticsPurgeResult:
        purge_calls.append(kwargs)
        return next(purge_results)

    monkeypatch.setattr(command.timezone, "now", lambda: captured_at)
    monkeypatch.setattr(command, "aggregate_authorized_opens", aggregate)
    monkeypatch.setattr(command, "purge_authorized_opens", purge)

    call_command("process_authorized_open_analytics", batch_size=2, max_batches=3)

    assert aggregate_calls == [
        {"through": captured_at, "batch_size": 2},
        {"through": captured_at, "batch_size": 2},
    ]
    assert purge_calls == [
        {"before": captured_at - timedelta(days=90), "batch_size": 2},
        {"before": captured_at - timedelta(days=90), "batch_size": 2},
    ]
    assert (
        "processed=3 removed=2 aggregation_may_remain=false purge_may_remain=false"
        in capsys.readouterr().out
    )


@pytest.mark.parametrize("max_batches", [0, 101])
def test_analytics_command_rejects_unbounded_transaction_counts(max_batches: int) -> None:
    with pytest.raises(CommandError, match="max-batches must be between 1 and 100"):
        call_command(
            "process_authorized_open_analytics",
            batch_size=1,
            max_batches=max_batches,
        )


def test_analytics_command_reports_batch_size_validation_as_command_error() -> None:
    with pytest.raises(CommandError, match="batch-size must be between 1 and 1000"):
        call_command(
            "process_authorized_open_analytics",
            batch_size=0,
            max_batches=1,
        )
