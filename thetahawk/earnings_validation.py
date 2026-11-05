"""Descriptive validation of stored earnings-implied moves against later outcomes.

This compares an option-implied magnitude recorded before an event with a
later realized move record. It is not a strategy backtest, calibration claim,
or estimate of executable P&L.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from math import isfinite
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable, Mapping


def validate_records(
    candidates: Iterable[Any],
    realized_moves: Iterable[Any],
    *,
    symbol: str | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Compare pre-event implied moves with uniquely matched later outcomes.

    A candidate is eligible only when its scan date is strictly before the
    event date and its `implied_move_fraction` is finite and nonnegative. An
    outcome must match symbol and event date, have a timezone-aware
    `observed_at` strictly after the event date, and be available by `as_of`.
    Multiple qualifying outcome rows for one event are reported as ambiguous
    and excluded rather than choosing a source or timestamp arbitrarily.
    """
    cutoff = as_of or datetime.now(timezone.utc)
    if not _aware(cutoff):
        raise ValueError("as_of must be timezone-aware")
    wanted = symbol.strip().upper() if symbol else None
    candidate_rows = [_as_mapping(row) for row in candidates]
    outcome_rows = [_as_mapping(row) for row in realized_moves]
    moves_by_event: dict[tuple[str, date], list[dict[str, Any]]] = {}
    excluded_outcomes: dict[str, int] = {}

    for move in outcome_rows:
        key = _move_key(move)
        if key is None:
            _increment(excluded_outcomes, "invalid_symbol_or_event_date")
            continue
        move_symbol, event_date = key
        if wanted and move_symbol != wanted:
            continue
        measured_on = _date(move.get("post_event_date"))
        if measured_on is None:
            _increment(excluded_outcomes, "missing_or_invalid_post_event_date")
            continue
        if measured_on <= event_date:
            _increment(excluded_outcomes, "measurement_not_after_event_date")
            continue
        observed = _datetime(move.get("observed_at"))
        if observed is None:
            _increment(excluded_outcomes, "missing_or_naive_observed_at")
            continue
        if observed.date() <= event_date:
            _increment(excluded_outcomes, "outcome_not_observed_after_event_date")
            continue
        if observed > cutoff:
            _increment(excluded_outcomes, "outcome_observed_after_report_cutoff")
            continue
        if observed.date() < measured_on:
            _increment(excluded_outcomes, "outcome_observed_before_measurement_date")
            continue
        magnitude = _outcome_magnitude(move)
        if magnitude is None:
            _increment(excluded_outcomes, "invalid_outcome_prices_or_move")
            continue
        moves_by_event.setdefault(key, []).append({"payload": move, "magnitude": magnitude})

    matched: list[dict[str, Any]] = []
    excluded_candidates: dict[str, int] = {}
    ambiguous_events: list[str] = []
    candidate_events: set[tuple[str, date]] = set()
    for candidate in candidate_rows:
        if candidate.get("scanner") != "earnings_implied_move":
            continue
        candidate_symbol = str(candidate.get("symbol", "")).strip().upper()
        event_date = _candidate_event_date(candidate)
        if not candidate_symbol or event_date is None:
            _increment(excluded_candidates, "missing_symbol_or_event_date")
            continue
        if wanted and candidate_symbol != wanted:
            continue
        key = (candidate_symbol, event_date)
        candidate_events.add(key)
        created = _datetime(candidate.get("created_at"))
        if created is None:
            _increment(excluded_candidates, "missing_or_naive_created_at")
            continue
        # Same-day timestamps cannot prove the scan preceded an earnings event
        # because this candidate payload does not store the event's exact time.
        if created.date() >= event_date:
            _increment(excluded_candidates, "candidate_not_proven_pre_event")
            continue
        implied = _finite_number((candidate.get("metrics") or {}).get("implied_move_fraction"))
        if implied is None or implied < 0:
            _increment(excluded_candidates, "missing_or_invalid_implied_move")
            continue

        outcomes = [row for row in moves_by_event.get(key, [])
                    if _datetime(row["payload"].get("observed_at")) > created]
        if not outcomes:
            _increment(excluded_candidates, "no_later_post_event_outcome")
            continue
        if len(outcomes) != 1:
            ambiguous_events.append(f"{candidate_symbol}:{event_date.isoformat()}")
            _increment(excluded_candidates, "multiple_outcome_rows_for_event")
            continue
        outcome = outcomes[0]
        realized = outcome["magnitude"]
        signed_error = implied - realized
        matched.append({
            "symbol": candidate_symbol,
            "event_date": event_date.isoformat(),
            "post_event_date": measured_on.isoformat(),
            "measurement_horizon_calendar_days": (measured_on - event_date).days,
            "candidate_id": candidate.get("candidate_id"),
            "candidate_created_at": created.isoformat(),
            "candidate_status": candidate.get("status"),
            "implied_move_fraction": implied,
            "realized_move_magnitude": realized,
            "signed_error_fraction": signed_error,
            "absolute_error_fraction": abs(signed_error),
            "outcome_observed_at": _datetime(outcome["payload"].get("observed_at")).isoformat(),
            "outcome_source": outcome["payload"].get("source"),
        })

    errors = [row["signed_error_fraction"] for row in matched]
    absolute_errors = [row["absolute_error_fraction"] for row in matched]
    return {
        "report_type": "descriptive_earnings_move_validation",
        "research_only": True,
        "exact_replay_possible": False,
        "as_of": cutoff.isoformat(),
        "symbol_filter": wanted,
        "sample_size": len(matched),
        "summary": {
            "mean_absolute_error_fraction": mean(absolute_errors) if absolute_errors else None,
            "median_absolute_error_fraction": median(absolute_errors) if absolute_errors else None,
            "mean_signed_error_fraction": mean(errors) if errors else None,
            "interpretation": (
                "Positive signed error means implied move exceeded the absolute realized move."
            ),
        },
        "matches": matched,
        "excluded_candidate_counts": excluded_candidates,
        "excluded_outcome_counts": excluded_outcomes,
        "ambiguous_events": sorted(set(ambiguous_events)),
        "candidate_event_count": len(candidate_events),
        "limitations": [
            "The explicit post_event_date identifies the supplied price measurement date, but market close/time convention is not modeled.",
            "The realized move may not cover the same expiration horizon as the option-implied move; compare the reported measurement horizon before interpretation.",
            "This compares move magnitudes only; it does not simulate a position, fills, fees, slippage, or strategy returns.",
            "Stored candidates are not an unbiased sample of all events or contracts.",
        ],
    }


def validate_database(
    database_path: str | Path,
    *,
    symbol: str | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Load the persisted candidate and outcome payloads read-only."""
    path = Path(database_path)
    if not path.exists():
        raise FileNotFoundError(f"Research database does not exist: {path}")
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("DuckDB is required to validate earnings outcomes") from exc
    connection = duckdb.connect(str(path), read_only=True)
    try:
        candidates = _read_payload_table(connection, "candidates")
        moves = _read_payload_table(connection, "realized_moves")
    finally:
        connection.close()
    return validate_records(candidates, moves, symbol=symbol, as_of=as_of)


def _read_payload_table(connection: Any, table: str) -> list[dict[str, Any]]:
    exists = connection.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema='main' AND table_name=?", [table]
    ).fetchone()[0]
    if not exists:
        return []
    rows = connection.execute(f"SELECT payload FROM {table}").fetchall()
    return [json.loads(row[0]) if isinstance(row[0], str) else row[0] for row in rows]


def _candidate_event_date(candidate: dict[str, Any]) -> date | None:
    try:
        return date.fromisoformat(str(candidate.get("candidate_id", "")).rsplit(":", 1)[-1])
    except ValueError:
        return None


def _move_key(move: dict[str, Any]) -> tuple[str, date] | None:
    symbol = str(move.get("symbol", "")).strip().upper()
    try:
        event_date = date.fromisoformat(str(move.get("event_date", ""))[:10])
    except ValueError:
        return None
    return (symbol, event_date) if symbol else None


def _outcome_magnitude(move: dict[str, Any]) -> float | None:
    reference = _finite_number(move.get("reference_price"))
    post = _finite_number(move.get("post_event_price"))
    reported = _finite_number(move.get("realized_move"))
    if reference is None or post is None or reference <= 0 or post <= 0 or reported is None:
        return None
    calculated = (post - reference) / reference
    # Reject internally inconsistent payloads rather than silently select one value.
    if not isfinite(calculated) or abs(calculated - reported) > 1e-6:
        return None
    return abs(calculated)


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if _aware(value) else None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if _aware(parsed) else None


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _increment(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise TypeError(f"Expected a mapping or dataclass record, got {type(value).__name__}")


def main() -> int:
    from .settings import Settings

    parser = argparse.ArgumentParser(
        description="Summarize pre-event earnings-implied moves against later observed move records"
    )
    parser.add_argument("--symbol", help="Limit the report to one symbol")
    parser.add_argument(
        "--database", type=Path, default=Settings.from_env().database_path,
        help="DuckDB path (defaults to THETAHAWK_DATABASE_PATH)",
    )
    args = parser.parse_args()
    try:
        report = validate_database(args.database, symbol=args.symbol)
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
