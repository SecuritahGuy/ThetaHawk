"""Read-only maturity report for local research inputs and option history."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class SymbolDataStatus:
    symbol: str
    option_snapshot_count: int = 0
    earnings_option_snapshot_count: int = 0
    option_contract_count: int = 0
    option_session_count: int = 0
    max_comparable_sessions: int = 0
    unusual_activity_baseline_possible: bool = False
    historical_price_rows: int = 0
    earnings_event_rows: int = 0
    earnings_events_with_reviewed_timing: int = 0
    realized_move_rows: int = 0
    realized_moves_with_post_event_date: int = 0


@dataclass(frozen=True, slots=True)
class DataStatusReport:
    database_path: Path
    minimum_baseline_sessions: int
    symbols: tuple[SymbolDataStatus, ...]
    database_exists: bool


def inspect_data_coverage(
    database_path: str | Path,
    *,
    minimum_baseline_sessions: int = 20,
) -> DataStatusReport:
    """Summarize local input coverage without creating or mutating a database.

    `unusual_activity_baseline_possible` means there are enough prior distinct
    session observations for at least one exact contract/source/intraday bucket.
    It does not guarantee that a specific current quote will match those rows;
    the scanner applies additional freshness, identity, and time-bucket checks.
    """
    if minimum_baseline_sessions < 1:
        raise ValueError("minimum_baseline_sessions must be positive")
    path = Path(database_path).expanduser()
    if not path.exists():
        return DataStatusReport(path, minimum_baseline_sessions, (), False)

    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("DuckDB is required to inspect the local research database") from exc

    connection = duckdb.connect(str(path), read_only=True)
    try:
        tables = {row[0] for row in connection.execute("SHOW TABLES").fetchall()}
        grouped: dict[str, dict[str, Any]] = {}
        if "quotes" in tables:
            quote_rows = connection.execute(
                "SELECT payload FROM quotes WHERE instrument_type = 'option'"
            ).fetchall()
            for (raw_payload,) in quote_rows:
                payload = _json_object(raw_payload)
                symbol = str(payload.get("symbol", "")).strip().upper()
                if not symbol:
                    continue
                item = grouped.setdefault(symbol, _empty_counts())
                item["option_snapshot_count"] += 1
                observed_at = _parse_datetime(payload.get("observed_at"))
                source_time = _parse_datetime(payload.get("source_timestamp")) or observed_at
                metadata = payload.get("metadata") or {}
                contract = metadata.get("contract_symbol") if isinstance(metadata, dict) else None
                if (
                    isinstance(metadata, dict)
                    and metadata.get("snapshot_purpose") == "reviewed_earnings_event"
                ):
                    item["earnings_option_snapshot_count"] += 1
                source = str(payload.get("source") or "unknown")
                try:
                    volume = int(payload["volume"]) if payload.get("volume") is not None else None
                except (TypeError, ValueError):
                    volume = None
                if contract:
                    item["contracts"].add(str(contract))
                if observed_at is not None:
                    session = observed_at.astimezone(EASTERN).date()
                    item["option_sessions"].add(session)
                    if (
                        contract
                        and source not in {"", "unknown"}
                        and volume is not None
                        and volume >= 0
                        and source_time is not None
                        and 0 <= (observed_at - source_time).total_seconds() <= 900
                    ):
                        bucket_minute = (observed_at.astimezone(EASTERN).minute // 15) * 15
                        bucket = observed_at.astimezone(EASTERN).hour * 60 + bucket_minute
                        key = (str(contract), source, bucket)
                        item["comparable_sessions"].setdefault(key, set()).add(session)

        _count_table(connection, tables, grouped, "historical_prices", "historical_price_rows")
        _count_table(connection, tables, grouped, "earnings_events", "earnings_event_rows")
        _count_table(connection, tables, grouped, "realized_moves", "realized_move_rows")
        if "realized_moves" in tables:
            for (raw_payload,) in connection.execute(
                "SELECT payload FROM realized_moves"
            ).fetchall():
                payload = _json_object(raw_payload)
                symbol = str(payload.get("symbol", "")).strip().upper()
                if symbol and payload.get("post_event_date"):
                    grouped.setdefault(symbol, _empty_counts())["measured_moves"] += 1

        if "earnings_events" in tables:
            for (raw_payload,) in connection.execute(
                "SELECT payload FROM earnings_events"
            ).fetchall():
                payload = _json_object(raw_payload)
                symbol = str(payload.get("symbol", "")).strip().upper()
                timing = str(payload.get("timing") or "").strip().lower()
                if symbol and timing in {"before_open", "after_close"}:
                    grouped.setdefault(symbol, _empty_counts())["reviewed_timing"] += 1

        report_symbols = []
        for symbol, counts in grouped.items():
            comparable = max(
                (len(sessions) for sessions in counts["comparable_sessions"].values()),
                default=0,
            )
            report_symbols.append(SymbolDataStatus(
                symbol=symbol,
                option_snapshot_count=counts["option_snapshot_count"],
                earnings_option_snapshot_count=counts["earnings_option_snapshot_count"],
                option_contract_count=len(counts["contracts"]),
                option_session_count=len(counts["option_sessions"]),
                max_comparable_sessions=comparable,
                unusual_activity_baseline_possible=(comparable >= minimum_baseline_sessions),
                historical_price_rows=counts["historical_price_rows"],
                earnings_event_rows=counts["earnings_event_rows"],
                earnings_events_with_reviewed_timing=counts["reviewed_timing"],
                realized_move_rows=counts["realized_move_rows"],
                realized_moves_with_post_event_date=counts["measured_moves"],
            ))
    finally:
        connection.close()

    return DataStatusReport(
        database_path=path,
        minimum_baseline_sessions=minimum_baseline_sessions,
        symbols=tuple(sorted(report_symbols, key=lambda row: row.symbol)),
        database_exists=True,
    )


def _empty_counts() -> dict[str, Any]:
    return {
        "option_snapshot_count": 0,
        "earnings_option_snapshot_count": 0,
        "contracts": set(),
        "option_sessions": set(),
        "comparable_sessions": {},
        "historical_price_rows": 0,
        "earnings_event_rows": 0,
        "reviewed_timing": 0,
        "realized_move_rows": 0,
        "measured_moves": 0,
    }


def _count_table(connection, tables: set[str], grouped: dict[str, dict[str, Any]], table: str, field: str) -> None:
    if table not in tables:
        return
    for (raw_payload,) in connection.execute(f"SELECT payload FROM {table}").fetchall():
        payload = _json_object(raw_payload)
        symbol = str(payload.get("symbol", "")).strip().upper()
        if symbol:
            grouped.setdefault(symbol, _empty_counts())[field] += 1


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        decoded = json.loads(value)
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Report local Thetahawk data coverage")
    parser.add_argument("--database", default="./data/thetahawk.duckdb")
    parser.add_argument("--minimum-baseline-sessions", type=int, default=20)
    args = parser.parse_args()
    report = inspect_data_coverage(
        args.database,
        minimum_baseline_sessions=args.minimum_baseline_sessions,
    )
    if not report.database_exists:
        print(f"No research database found at {report.database_path}.")
        return 0
    print(
        f"Data coverage from {report.database_path} (unusual-activity threshold: "
        f"{report.minimum_baseline_sessions} sessions)."
    )
    if not report.symbols:
        print("No symbol-level research inputs are stored yet.")
    for row in report.symbols:
        possible = "possible" if row.unusual_activity_baseline_possible else "not yet possible"
        print(
            f"{row.symbol}: {row.option_snapshot_count} option snapshots "
            f"({row.earnings_option_snapshot_count} earnings-linked), "
            f"{row.option_contract_count} contracts, {row.option_session_count} option sessions; "
            f"max same-contract/feed/time-bucket history {row.max_comparable_sessions} "
            f"({possible}); {row.historical_price_rows} equity-history rows; "
            f"{row.earnings_events_with_reviewed_timing}/{row.earnings_event_rows} earnings events "
            f"with reviewed timing; {row.realized_moves_with_post_event_date}/"
            f"{row.realized_move_rows} realized moves with measurement dates."
        )
    print(
        "Coverage counts indicate whether a baseline may be attainable; each scan still "
        "checks exact contract, feed, bucket, timestamp, and volume validity."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
