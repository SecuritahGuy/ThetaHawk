"""Read-only audit of the persisted inputs associated with a scan candidate.

The current database stores candidate metrics and some input identifiers, but
not a scan-run manifest or versioned policy. This module therefore reconstructs
the persisted evidence that can be matched to a candidate and reports whether
an exact recomputation is supported. It never labels an audit as an exact
replay when the required run metadata is absent.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any


def audit_candidate(database_path: str | Path, candidate_id: str) -> dict[str, Any]:
    """Return an evidence audit for one stored candidate.

    Rows are selected at or before the candidate creation time. Option quotes
    are additionally matched by contract ID and provider timestamp when those
    identifiers exist in the candidate record.
    """
    path = Path(database_path)
    if not path.exists():
        raise FileNotFoundError(f"Research database does not exist: {path}")
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("DuckDB is required to audit scan history") from exc

    connection = duckdb.connect(str(path), read_only=True)
    try:
        candidate = _find_candidate(connection, candidate_id)
        run_manifests = _run_manifests_for_candidate(connection, candidate_id)
        run_manifest = run_manifests[0] if run_manifests else None
        created_at = _parse_datetime(candidate["created_at"])
        symbol = str(candidate.get("symbol", "")).upper()
        input_ids = {str(value) for value in candidate.get("input_ids", []) if value}
        source_timestamps = {
            _normalize_datetime(value)
            for value in candidate.get("source_timestamps", [])
            if value
        }

        quote_payloads = _rows_before(
            connection,
            "quotes",
            "symbol = ? AND observed_at <= ?",
            [symbol, created_at],
        )
        quote_matches = _match_quotes(quote_payloads, input_ids, source_timestamps)

        event_payloads = _rows_before(
            connection,
            "earnings_events",
            "symbol = ? AND observed_at <= ?",
            [symbol, created_at],
        )
        event_matches = [
            row for row in event_payloads
            if _event_matches(row, input_ids, candidate_id)
        ]

        historical_prices: list[dict[str, Any]] = []
        expected_price_ids = {
            value for value in input_ids if _looks_like_price_id(value, symbol)
        }
        if expected_price_ids:
            historical_prices = [
                row for row in _rows_before(
                    connection,
                    "historical_prices",
                    "symbol = ? AND observed_at <= ?",
                    [symbol, created_at],
                )
                if _historical_price_id(row) in expected_price_ids
            ]

        event_date = _candidate_event_date(candidate)
        realized_moves: list[dict[str, Any]] = []
        if event_date is not None:
            realized_moves = [
                row for row in _rows_before(
                    connection,
                    "realized_moves",
                    "symbol = ? AND event_date < ? AND observed_at <= ?",
                    [symbol, event_date, created_at],
                )
                if _parse_date(row.get("event_date")) < event_date
            ]

        missing_contracts = sorted(
            contract_id for contract_id in input_ids
            if _looks_like_contract_id(contract_id)
            and contract_id not in quote_matches["option_contract_ids"]
        )
        matched_timestamps = quote_matches["matched_source_timestamps"]
        matched_event_timestamps = {
            _normalize_datetime(row.get("source_timestamp") or row.get("observed_at"))
            for row in event_matches
            if row.get("source_timestamp") or row.get("observed_at")
        }
        matched_history_timestamps = {
            _normalize_datetime(row["observed_at"])
            for row in historical_prices
            if row.get("observed_at")
        }
        missing_timestamps = sorted(
            source_timestamps
            - matched_timestamps
            - matched_event_timestamps
            - matched_history_timestamps
        )
        matched_price_ids = {_historical_price_id(row) for row in historical_prices}
        missing_price_ids = sorted(expected_price_ids - matched_price_ids)

        limitations = []
        if run_manifest is None:
            limitations.append(
                "The database has no scan-run manifest containing the scanner policy and code version."
            )
        else:
            limitations.append(
                "A versioned manifest and exact normalized run inputs are stored, but this scanner has no registered deterministic replay adapter yet."
            )
        limitations.extend((
            "The candidate does not identify the exact realized-move row IDs used by the calculation.",
            "A successful audit of stored inputs does not prove that current code would reproduce the stored result.",
        ))
        if missing_contracts:
            limitations.append("One or more candidate option contract IDs did not match a stored quote.")
        if missing_timestamps:
            limitations.append("One or more candidate source timestamps did not match a stored quote.")
        if not event_matches and candidate.get("scanner") == "earnings_implied_move":
            limitations.append("No matching earnings-event input was found in the stored event table.")
        if missing_price_ids:
            limitations.append("One or more candidate historical-price session IDs did not match stored history.")

        return {
            "audit_type": "stored_input_audit",
            "exact_replay_possible": False,
            "scan_run_manifest": run_manifest,
            "candidate": candidate,
            "matched_inputs": {
                "quotes": quote_matches["quotes"],
                "earnings_events": event_matches,
                "realized_move_count": len(realized_moves),
                "realized_move_ids": [
                    _realized_move_id(row) for row in realized_moves
                ],
                "historical_price_count": len(historical_prices),
                "historical_price_ids": sorted(matched_price_ids),
            },
            "unmatched": {
                "option_contract_ids": missing_contracts,
                "historical_price_ids": missing_price_ids,
                "source_timestamps": missing_timestamps,
            },
            "limitations": limitations,
        }
    finally:
        connection.close()


def _find_candidate(connection: Any, candidate_id: str) -> dict[str, Any]:
    _require_table(connection, "candidates")
    rows = connection.execute(
        "SELECT payload FROM candidates WHERE candidate_id = ?", [candidate_id]
    ).fetchall()
    if not rows:
        raise KeyError(f"Candidate not found: {candidate_id}")
    return _payload(rows[0][0])


def _run_manifests_for_candidate(connection: Any, candidate_id: str) -> list[dict[str, Any]]:
    if not _table_exists(connection, "scan_runs"):
        return []
    rows = connection.execute("SELECT payload FROM scan_runs ORDER BY as_of DESC").fetchall()
    manifests = [_payload(row[0]) for row in rows]
    return [
        manifest for manifest in manifests
        if candidate_id in manifest.get("candidate_ids", [])
    ]


def _rows_before(
    connection: Any,
    table: str,
    predicate: str,
    parameters: list[Any],
) -> list[dict[str, Any]]:
    if not _table_exists(connection, table):
        return []
    rows = connection.execute(
        f"SELECT payload FROM {table} WHERE {predicate}", parameters
    ).fetchall()
    return [_payload(row[0]) for row in rows]


def _table_exists(connection: Any, table: str) -> bool:
    return connection.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema = 'main' AND table_name = ?",
        [table],
    ).fetchone()[0] > 0


def _require_table(connection: Any, table: str) -> None:
    if not _table_exists(connection, table):
        raise ValueError(f"Database is missing required table: {table}")


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return json.loads(value)
    if isinstance(value, dict):
        return value
    raise ValueError("Stored payload is not a JSON object")


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _normalize_datetime(value: Any) -> str:
    parsed = _parse_datetime(value)
    return parsed.isoformat()


def _match_quotes(
    payloads: list[dict[str, Any]],
    input_ids: set[str],
    expected_timestamps: set[str],
) -> dict[str, Any]:
    matched: list[dict[str, Any]] = []
    contract_ids: set[str] = set()
    timestamps: set[str] = set()
    for row in payloads:
        quote_time = row.get("source_timestamp") or row.get("observed_at")
        normalized_time = _normalize_datetime(quote_time) if quote_time else None
        contract_id = (row.get("metadata") or {}).get("contract_symbol")
        instrument_type = str(row.get("instrument_type", "equity")).lower()
        if instrument_type == "option":
            if not contract_id or str(contract_id) not in input_ids:
                continue
            if expected_timestamps and normalized_time not in expected_timestamps:
                continue
            contract_ids.add(str(contract_id))
        elif expected_timestamps and normalized_time not in expected_timestamps:
            continue
        if normalized_time:
            timestamps.add(normalized_time)
        matched.append({
            "symbol": row.get("symbol"),
            "instrument_type": instrument_type,
            "source": row.get("source"),
            "source_timestamp": quote_time,
            "contract_id": contract_id,
        })
    return {
        "quotes": matched,
        "option_contract_ids": contract_ids,
        "matched_source_timestamps": timestamps,
    }


def _event_matches(
    row: dict[str, Any], input_ids: set[str], candidate_id: str
) -> bool:
    event_id = str((row.get("metadata") or {}).get("event_id", ""))
    if event_id and event_id in input_ids:
        return True
    try:
        _, _, event_date = candidate_id.rsplit(":", 2)
    except ValueError:
        return False
    return str(row.get("event_date", "")) == event_date


def _candidate_event_date(candidate: dict[str, Any]) -> date | None:
    if candidate.get("scanner") != "earnings_implied_move":
        return None
    try:
        return date.fromisoformat(str(candidate["candidate_id"]).rsplit(":", 1)[-1])
    except (KeyError, ValueError):
        return None


def _parse_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _realized_move_id(row: dict[str, Any]) -> str:
    return ":".join((
        str(row.get("symbol", "")),
        str(row.get("event_date", "")),
        str(row.get("source", "")),
        str(row.get("observed_at", "")),
    ))


def _looks_like_price_id(value: str, symbol: str) -> bool:
    prefix = f"{symbol}:"
    if not symbol or not value.startswith(prefix):
        return False
    try:
        date.fromisoformat(value[len(prefix):])
        return True
    except ValueError:
        return False


def _historical_price_id(row: dict[str, Any]) -> str:
    return f"{str(row.get('symbol', '')).upper()}:{row.get('session_date', '')}"


def _looks_like_contract_id(value: str) -> bool:
    return len(value) >= 15 and value[-15:-9].isdigit() and value[-9:-8] in {"C", "P"}


def main() -> int:
    from .settings import Settings

    parser = argparse.ArgumentParser(
        description="Audit persisted inputs for a Thetahawk scan candidate"
    )
    parser.add_argument("candidate_id", help="Stored candidate ID to audit")
    parser.add_argument(
        "--database",
        type=Path,
        default=Settings.from_env().database_path,
        help="DuckDB path (defaults to THETAHAWK_DATABASE_PATH)",
    )
    args = parser.parse_args()
    try:
        audit = audit_candidate(args.database, args.candidate_id)
    except (FileNotFoundError, KeyError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(audit, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
