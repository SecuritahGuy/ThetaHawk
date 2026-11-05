"""Exploratory walk-forward validation for the overextended-equity screen.

This is a close-based screen study, not an options or executable strategy
backtest. Signals use only prior closes and the signal-day close; returns begin
at the following session's close to avoid assuming a fill at the signal close.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date, datetime
from math import isfinite
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .domain import HistoricalPrice


EASTERN = ZoneInfo("America/New_York")


def walk_forward_overextended(
    prices: Iterable[HistoricalPrice],
    *,
    as_of: date,
    window_sessions: int = 20,
    min_history_prices: int = 60,
    minimum_move_fraction: float = 0.05,
    minimum_robust_z_score: float = 3.5,
    episode_cooldown_sessions: int = 20,
    forward_sessions: tuple[int, ...] = (5, 20),
) -> dict[str, Any]:
    """Apply the current screen thresholds to prior-only closes, then measure later returns.

    Consecutive threshold crossings from one rolling move are grouped into one
    episode. The next-session close is used as the entry proxy and each horizon
    is measured from that close. No overlapping returns are counted twice
    within a symbol's episode cooldown.
    """
    if window_sessions < 2 or min_history_prices < window_sessions + 2:
        raise ValueError("history must contain at least window_sessions + 2 closes")
    if minimum_move_fraction < 0 or minimum_robust_z_score < 0:
        raise ValueError("screen thresholds cannot be negative")
    if episode_cooldown_sessions < 0:
        raise ValueError("episode_cooldown_sessions cannot be negative")
    horizons = tuple(sorted(set(forward_sessions)))
    if not horizons or any(horizon < 1 for horizon in horizons):
        raise ValueError("forward_sessions must contain positive horizons")

    grouped: dict[str, dict[date, HistoricalPrice]] = defaultdict(dict)
    for row in prices:
        if (
            not row.symbol.strip()
            or row.session_date >= as_of
            or row.observed_at.tzinfo is None
            or row.observed_at.utcoffset() is None
            or not row.adjusted
            or not isfinite(row.close)
            or row.close <= 0
        ):
            continue
        symbol = row.symbol.strip().upper()
        previous = grouped[symbol].get(row.session_date)
        if previous is None or row.observed_at > previous.observed_at:
            grouped[symbol][row.session_date] = row

    per_symbol: list[dict[str, Any]] = []
    all_eligible: list[dict[str, float]] = []
    episodes: list[dict[str, Any]] = []
    for symbol in sorted(grouped):
        daily = [grouped[symbol][day] for day in sorted(grouped[symbol])]
        if not daily:
            continue
        sources = sorted({row.source for row in daily})
        if len(sources) != 1:
            per_symbol.append({
                "symbol": symbol,
                "source": sources,
                "price_sessions": len(daily),
                "eligible_days": 0,
                "threshold_days": 0,
                "episodes": 0,
                "excluded_reason": "mixed_sources",
            })
            continue

        eligible: list[dict[str, Any]] = []
        threshold_days: list[dict[str, Any]] = []
        final_horizon = max(horizons)
        for index in range(min_history_prices, len(daily) - final_horizon - 1):
            prior = daily[:index]
            current_move = daily[index].close / daily[index - window_sessions].close - 1
            baseline = [
                prior[base_index].close / prior[base_index - window_sessions].close - 1
                for base_index in range(window_sessions, len(prior))
            ]
            baseline_median = median(baseline)
            mad = median(abs(value - baseline_median) for value in baseline)
            if mad <= 0 or not isfinite(mad):
                continue
            robust_z = 0.67448975 * (current_move - baseline_median) / mad
            outcomes = {
                horizon: (
                    daily[index + 1 + horizon].close / daily[index + 1].close - 1
                )
                for horizon in horizons
            }
            record: dict[str, Any] = {
                "symbol": symbol,
                "signal_date": daily[index].session_date.isoformat(),
                "source": sources[0],
                "current_window_move_fraction": current_move,
                "robust_z_score": robust_z,
                "forward_returns": {
                    str(horizon): outcomes[horizon] for horizon in horizons
                },
                "direction_aligned_returns": {
                    str(horizon): outcome * (1 if current_move >= 0 else -1)
                    for horizon, outcome in outcomes.items()
                },
            }
            eligible.append(record)
            all_eligible.append(record)
            if (
                abs(current_move) >= minimum_move_fraction
                and abs(robust_z) >= minimum_robust_z_score
            ):
                threshold_days.append(record)

        symbol_episodes: list[dict[str, Any]] = []
        last_episode_index = -10**9
        day_to_index = {row.session_date.isoformat(): index for index, row in enumerate(daily)}
        for record in threshold_days:
            signal_index = day_to_index[record["signal_date"]]
            if signal_index - last_episode_index <= episode_cooldown_sessions:
                continue
            last_episode_index = signal_index
            symbol_episodes.append(record)
            episodes.append(record)

        per_symbol.append({
            "symbol": symbol,
            "source": sources[0],
            "history_retrieved_at": max(row.observed_at for row in daily).isoformat(),
            "first_session": daily[0].session_date.isoformat(),
            "last_session": daily[-1].session_date.isoformat(),
            "price_sessions": len(daily),
            "eligible_days": len(eligible),
            "threshold_days": len(threshold_days),
            "episodes": len(symbol_episodes),
        })

    review_flags = [
        {
            "symbol": row["symbol"],
            "flag": "insufficient_history_for_forward_test",
            "price_sessions": row["price_sessions"],
            "note": "No eligible walk-forward observations after history and forward-window requirements.",
        }
        for row in per_symbol
        if row.get("eligible_days") == 0 and not row.get("excluded_reason")
    ]
    review_flags.extend({
        "symbol": row["symbol"],
        "signal_date": row["signal_date"],
        "flag": "extreme_20_session_move",
        "move_fraction": row["current_window_move_fraction"],
        "note": "Confirm the price series, corporate actions, halts, and event context independently.",
    } for row in episodes if abs(row["current_window_move_fraction"]) >= 1.0)

    return {
        "report_type": "exploratory_overextended_equity_walk_forward",
        "research_only": True,
        "strategy_backtest": False,
        "as_of_date_exclusive": as_of.isoformat(),
        "policy": {
            "window_sessions": window_sessions,
            "minimum_history_prices": min_history_prices,
            "minimum_move_fraction": minimum_move_fraction,
            "minimum_robust_z_score": minimum_robust_z_score,
            "episode_cooldown_sessions": episode_cooldown_sessions,
            "forward_sessions_after_next_session_close": list(horizons),
        },
        "eligible_daily_observations": len(all_eligible),
        "threshold_crossing_days": sum(row["threshold_days"] for row in per_symbol),
        "episode_count": len(episodes),
        "episodes": episodes,
        "review_flags": review_flags,
        "summary": {
            str(horizon): {
                "episode_returns": _return_summary(episodes, horizon),
                "all_eligible_days_reference": _return_summary(all_eligible, horizon),
            }
            for horizon in horizons
        },
        "symbols": per_symbol,
        "limitations": [
            "Daily adjusted closes are close-based proxies; option prices, spreads, and fills are not modeled.",
            "Entries use the next session close because daily data cannot measure an opening fill.",
            "Yahoo history is not point-in-time; adjusted closes and historical corrections may be revised.",
            "The configured universe contains current symbols only and has survivorship and selection bias.",
            "Signals and reference days overlap; observations are not independent and no significance claim is made.",
            "The screen is nondirectional; direction-aligned returns are descriptive diagnostics, not a trading rule.",
        ],
    }


def _return_summary(rows: list[dict[str, Any]], horizon: int) -> dict[str, Any]:
    key = str(horizon)
    returns = [row["forward_returns"][key] for row in rows]
    aligned = [row["direction_aligned_returns"][key] for row in rows]
    if not returns:
        return {"sample_size": 0, "mean_return_fraction": None,
                "median_return_fraction": None, "mean_absolute_return_fraction": None,
                "mean_direction_aligned_fraction": None,
                "direction_aligned_positive_fraction": None}
    return {
        "sample_size": len(returns),
        "mean_return_fraction": mean(returns),
        "median_return_fraction": median(returns),
        "mean_absolute_return_fraction": mean(abs(value) for value in returns),
        "mean_direction_aligned_fraction": mean(aligned),
        "direction_aligned_positive_fraction": sum(value > 0 for value in aligned) / len(aligned),
    }


def load_database_prices(database_path: str | Path, *, symbol: str | None = None) -> list[HistoricalPrice]:
    """Read adjusted daily closes from the local research database."""
    path = Path(database_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Research database does not exist: {path}")
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("DuckDB is required for this backtest") from exc
    connection = duckdb.connect(str(path), read_only=True)
    try:
        tables = {row[0] for row in connection.execute("SHOW TABLES").fetchall()}
        if "historical_prices" not in tables:
            return []
        query = "SELECT payload FROM historical_prices"
        parameters: list[str] = []
        if symbol:
            query += " WHERE symbol = ?"
            parameters.append(symbol.strip().upper())
        rows = connection.execute(query, parameters).fetchall()
    finally:
        connection.close()
    prices = []
    for (payload,) in rows:
        value = json.loads(payload) if isinstance(payload, str) else payload
        observed_at = _datetime(value.get("observed_at"))
        session_date = date.fromisoformat(str(value["session_date"])[:10])
        if observed_at is None:
            continue
        try:
            close = float(value["close"])
        except (KeyError, TypeError, ValueError):
            continue
        prices.append(HistoricalPrice(
            symbol=str(value.get("symbol") or "").upper(),
            session_date=session_date,
            close=close,
            source=str(value.get("source") or "unknown"),
            observed_at=observed_at,
            adjusted=bool(value.get("adjusted", False)),
        ))
    return prices


def _datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def main() -> int:
    from .settings import Settings

    parser = argparse.ArgumentParser(
        description="Run an exploratory walk-forward study of the overextended-equity screen"
    )
    parser.add_argument("--database", type=Path, default=Settings.from_env().database_path)
    parser.add_argument("--symbol", help="Limit the study to one symbol")
    parser.add_argument("--as-of", type=date.fromisoformat,
                        default=datetime.now(EASTERN).date(),
                        help="Exclusive final session date (default: today in New York)")
    args = parser.parse_args()
    try:
        prices = load_database_prices(args.database, symbol=args.symbol)
        report = walk_forward_overextended(prices, as_of=args.as_of)
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
