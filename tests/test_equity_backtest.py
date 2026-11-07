from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from thetahawk.domain import HistoricalPrice
from thetahawk.equity_backtest import (
    _leave_one_symbol_out,
    _per_symbol_contribution,
    _return_summary,
    walk_forward_overextended,
)


def _prices(*, event_jump_at: int | None = None) -> list[HistoricalPrice]:
    rows = []
    close = 100.0
    first_day = date(2025, 1, 1)
    observed = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index in range(125):
        daily_return = 0.0005 + ((index % 7) - 3) * 0.0004
        if event_jump_at == index:
            daily_return += 0.15
        close *= 1 + daily_return
        rows.append(HistoricalPrice(
            symbol="AAPL",
            session_date=first_day + timedelta(days=index),
            close=close,
            source="yahoo-fixture:auto-adjusted-daily",
            observed_at=observed,
            adjusted=True,
        ))
    return rows


def test_walk_forward_groups_repeated_threshold_days_into_one_episode() -> None:
    report = walk_forward_overextended(
        _prices(event_jump_at=80),
        as_of=date(2025, 7, 1),
    )

    assert report["strategy_backtest"] is False
    assert report["threshold_crossing_days"] > 1
    assert report["episode_count"] == 1
    episode = report["episodes"][0]
    assert episode["signal_date"] == (date(2025, 1, 1) + timedelta(days=80)).isoformat()
    assert report["summary"]["5"]["episode_returns"]["sample_size"] == 1
    assert not any(flag["flag"] == "extreme_20_session_move" for flag in report["review_flags"])


def test_walk_forward_excludes_session_on_exclusive_as_of_date() -> None:
    prices = _prices(event_jump_at=80)
    report = walk_forward_overextended(prices, as_of=prices[-1].session_date)

    assert all(
        row["signal_date"] < prices[-1].session_date.isoformat()
        for row in report["episodes"]
    )


def test_symbol_contributions_add_to_pooled_mean_and_leave_one_out_is_explicit() -> None:
    episodes = [
        {"symbol": "AAA", "forward_returns": {"5": 0.1},
         "direction_aligned_returns": {"5": 0.1}},
        {"symbol": "AAA", "forward_returns": {"5": 0.3},
         "direction_aligned_returns": {"5": 0.3}},
        {"symbol": "BBB", "forward_returns": {"5": -0.1},
         "direction_aligned_returns": {"5": -0.1}},
    ]

    contributions = _per_symbol_contribution(episodes, 5, ["AAA", "BBB"])
    sensitivity = _leave_one_symbol_out(episodes, 5, ["AAA", "BBB"])

    assert sum(row["contribution_to_pooled_mean_fraction"] for row in contributions) == 0.1
    assert contributions[0]["sample_size"] == 2
    assert contributions[0]["mean_return_fraction"] == 0.2
    assert contributions[1]["contribution_to_pooled_mean_fraction"] == -0.1 / 3
    assert sensitivity[0]["excluded_symbol"] == "AAA"
    assert sensitivity[0]["mean_return_fraction"] == -0.1
    assert sensitivity[0]["sample_size"] == 1
    assert sensitivity[1]["excluded_symbol"] == "BBB"
    assert sensitivity[1]["mean_return_fraction"] == 0.2
    assert sensitivity[1]["sample_size"] == 2


def test_robust_return_summaries_reduce_single_outlier_influence() -> None:
    rows = [
        {"forward_returns": {"5": value}, "direction_aligned_returns": {"5": value}}
        for value in [0.0] * 19 + [10.0]
    ]

    summary = _return_summary(rows, 5)

    assert summary["mean_return_fraction"] == 0.5
    assert summary["median_return_fraction"] == 0.0
    assert summary["median_absolute_deviation_fraction"] == 0.0
    assert summary["winsorized_mean_5pct_fraction"] == 0.0
    assert summary["trimmed_mean_10pct_fraction"] == 0.0
