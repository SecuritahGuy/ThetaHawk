"""Read-only audit and supported deterministic replay for scan candidates."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .domain import EarningsEvent, HistoricalPrice, RealizedMove, record_payload
from .run_manifest import current_code_version
from .scan import evaluate_earnings_inputs
from .scanners import (
    OverextendedEquityInput,
    OverextendedEquityScanner,
    ScreenPolicy,
    VolatilityDivergenceInput,
    VolatilityDivergenceScanner,
)
from .storage import _quote_from_payload
from .unusual_activity import UnusualOptionsActivityInput, UnusualOptionsActivityScanner


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
        recomputation = _recompute_from_manifest(candidate, run_manifest)
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
        elif not recomputation["supported"]:
            limitations.append(
                recomputation["reason"]
            )
        elif not recomputation["code_version_matches"]:
            limitations.append(
                "The captured code version differs from the current checkout; recomputation is diagnostic, not exact for the original implementation."
            )
        elif not recomputation["matches_stored_candidate"]:
            limitations.append(
                "The registered deterministic adapter ran, but its result differs from the stored candidate."
            )
        limitations.extend((
            "Replay uses the manifest's captured normalized inputs and the currently installed compatible adapter.",
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
            "audit_type": (
                "deterministic_replay"
                if recomputation["supported"] and recomputation["code_version_matches"]
                else "stored_input_audit"
            ),
            "exact_replay_possible": (
                recomputation["supported"] and recomputation["code_version_matches"]
            ),
            "recomputation": recomputation,
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


def _recompute_from_manifest(
    stored_candidate: dict[str, Any], manifest: dict[str, Any] | None
) -> dict[str, Any]:
    if manifest is None:
        return {"supported": False, "code_version_matches": False, "matches_stored_candidate": False,
                "reason": "No matching scan-run manifest is available."}
    scanner_name = manifest.get("scanner")
    expected_policies = {
        "earnings_implied_move": "earnings-screen-policy-v1",
        "overextended_equity": "overextended-equity-policy-v1",
        "volatility_divergence": "volatility-divergence-policy-v1",
        "unusual_options_activity": "unusual-options-activity-policy-v1",
        "options_discovery": "integrated-discovery-policy-v1",
    }
    if manifest.get("scanner_version") != "1" or expected_policies.get(scanner_name) != manifest.get("policy_version"):
        return {"supported": False, "code_version_matches": False, "matches_stored_candidate": False,
                "reason": "No compatible deterministic replay adapter is registered for this scanner version."}
    inputs = manifest.get("inputs")
    if not isinstance(inputs, dict):
        return {"supported": False, "code_version_matches": False, "matches_stored_candidate": False,
                "reason": "The scan manifest does not contain normalized input records."}
    try:
        candidates = _replay_scanner(scanner_name, inputs, _parse_datetime(manifest["as_of"]))
        recomputed = next(
            candidate for candidate in candidates
            if candidate.candidate_id == stored_candidate.get("candidate_id")
        )
        payload = record_payload(recomputed)
    except (KeyError, StopIteration, TypeError, ValueError) as exc:
        return {"supported": False, "code_version_matches": False, "matches_stored_candidate": False,
                "reason": f"Manifest inputs could not be replayed: {type(exc).__name__}: {exc}"}

    matches = json.dumps(payload, sort_keys=True, separators=(",", ":")) == json.dumps(
        stored_candidate, sort_keys=True, separators=(",", ":")
    )
    code_version_matches = (
        manifest.get("code_version") == current_code_version()
        and manifest.get("code_version") != "unknown"
    )
    return {
        "supported": True,
        "code_version_matches": code_version_matches,
        "matches_stored_candidate": matches,
        "recomputed_candidate": payload,
        "reason": "Recomputed with the registered earnings scanner version and captured inputs.",
    }


def _replay_scanner(scanner_name: str, inputs: dict[str, Any], as_of: datetime) -> list[Any]:
    if scanner_name == "earnings_implied_move":
        events = [_earnings_event(row) for row in inputs["earnings_events"]]
        moves = [_realized_move(row) for row in inputs["realized_moves"]]
        quotes = [_quote_from_payload(row) for row in inputs["quotes"]]
        return evaluate_earnings_inputs(
            events,
            moves,
            quotes,
            as_of,
            use_alpaca=bool(inputs["use_alpaca"]),
            policy=ScreenPolicy(**inputs["policy"]),
        )
    if scanner_name == "overextended_equity":
        return [OverextendedEquityScanner().scan(OverextendedEquityInput(
            symbol=row["symbol"],
            as_of=_parse_datetime(row["as_of"]),
            quote=_quote_from_payload(row["quote"]),
            historical_prices=[_historical_price(price) for price in row["historical_prices"]],
            window_sessions=int(row["window_sessions"]),
            min_history_prices=int(row["min_history_prices"]),
            minimum_move_fraction=float(row["minimum_move_fraction"]),
            minimum_robust_z_score=float(row["minimum_robust_z_score"]),
        )) for row in inputs["screen_inputs"]]
    if scanner_name == "volatility_divergence":
        return [VolatilityDivergenceScanner().scan(VolatilityDivergenceInput(
            symbol=row["symbol"],
            as_of=_parse_datetime(row["as_of"]),
            underlying_quote=_quote_from_payload(row["underlying_quote"]),
            option_quotes=[_quote_from_payload(quote) for quote in row["option_quotes"]],
            historical_prices=[_historical_price(price) for price in row["historical_prices"]],
            realized_window_sessions=int(row["realized_window_sessions"]),
            minimum_history_prices=int(row["minimum_history_prices"]),
            minimum_divergence_fraction=float(row["minimum_divergence_fraction"]),
            minimum_option_volume=int(row["minimum_option_volume"]),
            minimum_open_interest=int(row["minimum_open_interest"]),
            maximum_quote_age_seconds=int(row["maximum_quote_age_seconds"]),
            maximum_spread_fraction=float(row["maximum_spread_fraction"]),
            maximum_open_interest_age_days=int(row["maximum_open_interest_age_days"]),
        )) for row in inputs["screen_inputs"]]
    if scanner_name == "unusual_options_activity":
        output = []
        scanner = UnusualOptionsActivityScanner()
        for candidate_id, row in inputs["candidate_inputs"].items():
            output.append(scanner.scan(UnusualOptionsActivityInput(
                symbol=row["symbol"],
                as_of=as_of,
                current=_quote_from_payload(row["current"]),
                history=[_quote_from_payload(quote) for quote in row["history"]],
            )))
        return output
    if scanner_name == "options_discovery":
        from .contract_selection import select_option_pairs
        from .discovery_ranking import rank_discoveries
        from .discover import _discovery_records, _pair_candidate

        symbols = tuple(inputs["symbols"])
        stale_seconds = int(inputs["stale_quote_seconds"])
        selection_policy = dict(inputs["selection_policy"])
        equity_quotes = {
            quote.symbol.upper(): quote
            for quote in (_quote_from_payload(row) for row in inputs["equity_quotes"])
        }
        options_by_symbol = {
            symbol.upper(): [_quote_from_payload(row) for row in rows]
            for symbol, rows in inputs["option_quotes_by_symbol"].items()
        }
        prices_by_symbol = {
            symbol.upper(): [_historical_price(row) for row in rows]
            for symbol, rows in inputs["historical_prices_by_symbol"].items()
        }
        policies = inputs["screen_policies"]
        equity_policy = policies["overextended_equity"]
        volatility_policy = policies["volatility_divergence"]
        candidates = []
        for symbol in symbols:
            symbol = str(symbol).upper()
            underlying = equity_quotes.get(symbol) or _quote_from_payload({
                "symbol": symbol,
                "observed_at": as_of.isoformat(),
                "instrument_type": "equity",
                "source": "alpaca:stocks:unavailable",
                "metadata": {},
            })
            option_chain = options_by_symbol.get(symbol, [])
            history = prices_by_symbol.get(symbol, [])
            pairs = select_option_pairs(
                option_chain,
                underlying,
                as_of,
                max_quote_age_seconds=stale_seconds,
                **selection_policy,
            )
            candidates.extend(_pair_candidate(pair, as_of) for pair in pairs)
            candidates.append(OverextendedEquityScanner().scan(OverextendedEquityInput(
                symbol=symbol,
                as_of=as_of,
                quote=underlying,
                historical_prices=history,
                **equity_policy,
            )))
            candidates.append(VolatilityDivergenceScanner().scan(VolatilityDivergenceInput(
                symbol=symbol,
                as_of=as_of,
                underlying_quote=underlying,
                option_quotes=option_chain,
                historical_prices=history,
                **volatility_policy,
            )))

        unusual_scanner = UnusualOptionsActivityScanner()
        for row in inputs["unusual_activity_inputs_by_candidate"].values():
            candidates.append(unusual_scanner.scan(UnusualOptionsActivityInput(
                symbol=row["symbol"],
                as_of=as_of,
                current=_quote_from_payload(row["current"]),
                history=[_quote_from_payload(quote) for quote in row["history"]],
            )))
        ranked = rank_discoveries(candidates)
        return [*candidates, *_discovery_records(ranked, as_of)]
    raise ValueError(f"No deterministic replay adapter for scanner: {scanner_name}")


def _historical_price(row: dict[str, Any]) -> HistoricalPrice:
    return HistoricalPrice(
        symbol=str(row["symbol"]),
        session_date=_parse_date(row["session_date"]),
        close=float(row["close"]),
        source=str(row.get("source", "unknown")),
        observed_at=_parse_datetime(row["observed_at"]),
        adjusted=bool(row.get("adjusted", False)),
    )


def _earnings_event(row: dict[str, Any]) -> EarningsEvent:
    return EarningsEvent(
        symbol=str(row["symbol"]),
        event_date=_parse_date(row["event_date"]),
        timing=row.get("timing"),
        observed_at=_parse_datetime(row["observed_at"]),
        source=str(row.get("source", "unknown")),
        source_timestamp=_parse_datetime(row["source_timestamp"])
        if row.get("source_timestamp") else None,
        fiscal_period=row.get("fiscal_period"),
        metadata=row.get("metadata") or {},
    )


def _realized_move(row: dict[str, Any]) -> RealizedMove:
    return RealizedMove(
        symbol=str(row["symbol"]),
        event_date=_parse_date(row["event_date"]),
        reference_price=float(row["reference_price"]),
        post_event_price=float(row["post_event_price"]),
        realized_move=float(row["realized_move"]),
        source=str(row.get("source", "unknown")),
        observed_at=_parse_datetime(row["observed_at"]),
        post_event_date=_parse_date(row["post_event_date"])
        if row.get("post_event_date") else None,
    )


def _parse_date(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


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
