"""Run the unusual-options-activity screen over stored option snapshots."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from typing import Sequence

from .domain import Candidate
from .run_manifest import create_scan_run_manifest
from .settings import Settings
from .storage import ResearchStore
from .unusual_activity import (
    UnusualOptionsActivityInput,
    UnusualOptionsActivityScanner,
)


def run_unusual_activity_scan(
    *,
    symbols: Sequence[str] | None = None,
    settings: Settings | None = None,
    as_of: datetime | None = None,
    history_days: int = 45,
) -> int:
    settings = settings or Settings.from_env()
    selected = tuple(dict.fromkeys(
        symbol.strip().upper()
        for symbol in (symbols or settings.symbols)
        if symbol.strip()
    ))
    if not selected:
        raise ValueError("At least one symbol is required")
    if history_days < 1:
        raise ValueError("history_days must be positive")
    scan_time = as_of or datetime.now(timezone.utc)
    if scan_time.tzinfo is None or scan_time.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    scanner = UnusualOptionsActivityScanner()
    candidates: list[Candidate] = []
    snapshots_seen = 0
    candidate_inputs: dict[str, dict[str, object]] = {}

    with ResearchStore(settings.database_path) as store:
        for symbol in selected:
            quotes = store.option_quotes(
                symbol,
                from_observed_at=scan_time - timedelta(days=history_days),
                before_observed_at=scan_time + timedelta(microseconds=1),
            )
            snapshots_seen += len(quotes)
            latest_by_contract = {}
            for quote in quotes:
                contract = quote.metadata.get("contract_symbol")
                if not contract:
                    continue
                contract = str(contract)
                if (
                    contract not in latest_by_contract
                    or quote.observed_at > latest_by_contract[contract].observed_at
                ):
                    latest_by_contract[contract] = quote

            for contract, current in latest_by_contract.items():
                history = [
                    quote for quote in quotes
                    if quote.metadata.get("contract_symbol") == contract
                    and quote.observed_at < current.observed_at
                ]
                candidates.append(scanner.scan(UnusualOptionsActivityInput(
                    symbol=symbol,
                    as_of=scan_time,
                    current=current,
                    history=history,
                )))
                candidate_inputs[candidates[-1].candidate_id] = {
                    "symbol": symbol,
                    "current": current,
                    "history": history,
                }
        manifest = create_scan_run_manifest(
            scanner=scanner.name,
            scanner_version="1",
            policy_version="unusual-options-activity-policy-v1",
            as_of=scan_time,
            candidate_ids=tuple(candidate.candidate_id for candidate in candidates),
            inputs={
                "symbols": selected,
                "history_days": history_days,
                "policy": {
                    "minimum_history_sessions": 20,
                    "time_bucket_minutes": 15,
                    "minimum_current_volume": 50,
                    "minimum_robust_z_score": 3.5,
                    "maximum_quote_age_seconds": 900,
                },
                "candidate_inputs": candidate_inputs,
            },
        )
        store.save_candidates(candidates)
        store.save_scan_run(manifest)

    flagged = sum(candidate.status.value == "research" for candidate in candidates)
    print(
        f"Reviewed {snapshots_seen} stored option snapshots across {len(selected)} symbols; "
        f"stored {len(candidates)} contract candidates ({flagged} flagged) in "
        f"{settings.database_path}."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Screen accumulated Alpaca snapshots for unusual option volume"
    )
    parser.add_argument("--symbols", help="Comma-separated symbols; defaults to THETAHAWK_SYMBOLS")
    parser.add_argument("--history-days", type=int, default=45)
    args = parser.parse_args()
    symbols = tuple(part.strip().upper() for part in args.symbols.split(",") if part.strip()) if args.symbols else None
    return run_unusual_activity_scan(symbols=symbols, history_days=args.history_days)


if __name__ == "__main__":
    raise SystemExit(main())
