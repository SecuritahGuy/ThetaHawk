from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from thetahawk.domain import HistoricalPrice
from thetahawk.equity_backtest import walk_forward_overextended


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
