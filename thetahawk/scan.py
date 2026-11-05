"""One-shot earnings scan from timestamped CSV inputs and market quotes."""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .csv_inputs import CsvResearchInputs
from .domain import Candidate, CandidateStatus, EarningsEvent, Quote, record_payload
from .providers import AlpacaMarketDataProvider, DemoProvider, MarketDataProvider
from .scanners import EarningsImpliedMoveScanner, EarningsScanInput, ScreenPolicy
from .run_manifest import create_scan_run_manifest
from .settings import Settings
from .storage import ResearchStore


EASTERN = ZoneInfo("America/New_York")


def _event_datetime(event: EarningsEvent) -> datetime | None:
    times = {"before_open": time(8, 15), "after_close": time(16, 15)}
    local_time = times.get((event.timing or "").lower())
    if local_time is None:
        return None
    return datetime.combine(event.event_date, local_time, tzinfo=EASTERN)


def _suppressed(event: EarningsEvent, reason: str, as_of: datetime) -> Candidate:
    return Candidate(
        candidate_id=f"{event.symbol.upper()}:earnings_implied_move:{event.event_date.isoformat()}",
        symbol=event.symbol.upper(),
        scanner="earnings_implied_move",
        score=0.0,
        created_at=as_of,
        status=CandidateStatus.SUPPRESSED,
        summary=f"Suppressed: {reason}",
        rationale=(f"Suppressed: {reason}",),
        risks=("A suppressed record is not a trade candidate.",),
        input_ids=(str(event.metadata.get("event_id", "")),),
        source_timestamps=(event.source_timestamp or event.observed_at,),
        metrics={"event_source": event.source},
    )


def _best_atm_pair(
    options: list[Quote], symbol: str, expiration: date, underlying_price: float
) -> tuple[Quote, Quote] | None:
    by_strike: dict[float, dict[str, Quote]] = {}
    for quote in options:
        if quote.symbol.upper() != symbol.upper() or quote.expiration != expiration:
            continue
        if quote.strike is None or quote.right is None:
            continue
        by_strike.setdefault(quote.strike, {})[quote.right.value] = quote
    pairs = [
        (abs(strike - underlying_price), legs["call"], legs["put"])
        for strike, legs in by_strike.items()
        if "call" in legs and "put" in legs
    ]
    if not pairs:
        return None
    _, call, put = min(pairs, key=lambda pair: pair[0])
    return call, put


def evaluate_earnings_inputs(
    events: list[EarningsEvent],
    realized_moves,
    quotes: list[Quote],
    as_of: datetime,
    *,
    use_alpaca: bool,
    policy: ScreenPolicy | None = None,
) -> list[Candidate]:
    """Pure earnings evaluation shared by collection and deterministic replay."""
    equity_by_symbol = {
        quote.symbol.upper(): quote for quote in quotes
        if quote.instrument_type.lower() == "equity"
    }
    option_by_key: dict[tuple[str, date], list[Quote]] = {}
    for quote in quotes:
        if quote.instrument_type.lower() == "option" and quote.expiration is not None:
            option_by_key.setdefault((quote.symbol.upper(), quote.expiration), []).append(quote)

    candidates: list[Candidate] = []
    scanner = EarningsImpliedMoveScanner(policy)
    for event in events:
        event_at = _event_datetime(event)
        if event_at is None:
            candidates.append(_suppressed(event, "earnings timing must be before_open or after_close", as_of))
            continue
        if event_at.astimezone(timezone.utc) <= as_of:
            candidates.append(_suppressed(event, "event time is not in the future", as_of))
            continue
        expiration_raw = event.metadata.get("expiration_date")
        if not expiration_raw:
            candidates.append(_suppressed(event, "option expiration_date is missing from the event input", as_of))
            continue
        expiration = date.fromisoformat(str(expiration_raw))
        underlying = equity_by_symbol.get(event.symbol.upper())
        if underlying is None or underlying.midpoint is None:
            candidates.append(_suppressed(event, "underlying quote is unavailable", as_of))
            continue
        if not use_alpaca:
            candidates.append(_suppressed(event, "demo provider has no option-chain fixture", as_of))
            continue
        pair = _best_atm_pair(
            option_by_key.get((event.symbol.upper(), expiration), []),
            event.symbol,
            expiration,
            underlying.midpoint,
        )
        if pair is None:
            candidates.append(_suppressed(event, "no matching call and put at the selected expiration", as_of))
            continue
        call, put = pair
        candidates.append(scanner.scan(EarningsScanInput(
            symbol=event.symbol,
            as_of=as_of,
            underlying_quote=underlying,
            event_observed_at=event.source_timestamp or event.observed_at,
            event_source=event.source,
            event_id=str(event.metadata.get("event_id", "")),
            event_at=event_at,
            expiration_at=datetime.combine(expiration, time(16, 0), tzinfo=EASTERN),
            call=call,
            put=put,
            historical_realized_moves=tuple(
                move for move in realized_moves if move.symbol.upper() == event.symbol.upper()
            ),
        )))
    return candidates


def run_scan(*, use_alpaca: bool, symbols: tuple[str, ...] | None = None) -> int:
    settings = Settings.from_env()
    selected_symbols = tuple(symbol.upper() for symbol in (symbols or settings.symbols))
    provider: MarketDataProvider = (
        AlpacaMarketDataProvider(settings) if use_alpaca else DemoProvider()
    )
    inputs = CsvResearchInputs(settings)
    initial_time = datetime.now(timezone.utc)
    events = inputs.get_earnings_events(
        selected_symbols,
        start=initial_time.astimezone(EASTERN).date(),
        end=(initial_time + timedelta(days=45)).astimezone(EASTERN).date(),
    )
    realized_moves = inputs.get_realized_moves(selected_symbols)
    quotes: list[Quote] = []
    fetched_chains: set[tuple[str, date]] = set()
    if events:
        equity_quotes = provider.get_equity_quotes(selected_symbols)
        quotes.extend(equity_quotes)
        if use_alpaca:
            for event in events:
                expiration_raw = event.metadata.get("expiration_date")
                if not expiration_raw:
                    continue
                expiration = date.fromisoformat(str(expiration_raw))
                key = (event.symbol.upper(), expiration)
                if key not in fetched_chains:
                    fetched_chains.add(key)
                    chain = provider.get_option_quotes(event.symbol, expiration)
                    quotes.extend(chain)

    as_of = datetime.now(timezone.utc)
    scanner = EarningsImpliedMoveScanner()
    candidates = evaluate_earnings_inputs(
        events,
        realized_moves,
        quotes,
        as_of,
        use_alpaca=use_alpaca,
        policy=scanner.policy,
    )

    manifest = create_scan_run_manifest(
        scanner=scanner.name,
        scanner_version="1",
        policy_version="earnings-screen-policy-v1",
        as_of=as_of,
        candidate_ids=tuple(candidate.candidate_id for candidate in candidates),
        inputs={
            "symbols": selected_symbols,
            "provider": provider.name,
            "use_alpaca": use_alpaca,
            "policy": record_payload(scanner.policy),
            "earnings_events": events,
            "realized_moves": realized_moves,
            "quotes": quotes,
        },
    )
    with ResearchStore(settings.database_path) as store:
        store.save_quotes(quotes)
        store.save_earnings_events(events)
        store.save_realized_moves(realized_moves)
        store.save_candidates(candidates)
        store.save_scan_run(manifest)
    print(
        f"Scanned {len(events)} earnings events; stored {len(quotes)} quotes and "
        f"{len(candidates)} research records in {settings.database_path}."
    )
    print(f"Provider: {provider.name}; options feed: {settings.alpaca_options_feed if use_alpaca else 'fictional demo'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a one-shot Thetahawk earnings scan")
    parser.add_argument(
        "--alpaca",
        action="store_true",
        help="Use read-only Alpaca quote endpoints; requires API keys and market-data access",
    )
    parser.add_argument("--symbols", help="Comma-separated symbols; defaults to THETAHAWK_SYMBOLS")
    args = parser.parse_args()
    symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip()) if args.symbols else None
    return run_scan(use_alpaca=args.alpaca, symbols=symbols)


if __name__ == "__main__":
    raise SystemExit(main())
