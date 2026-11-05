"""Integrated, read-only options discovery across the configured symbol universe."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence
from zoneinfo import ZoneInfo

from .contract_selection import OptionPairSelection, select_option_pairs
from .discovery_ranking import RankedDiscovery, rank_discoveries
from .domain import Candidate, CandidateStatus, Quote
from .providers import AlpacaMarketDataProvider
from .scanners import (
    OverextendedEquityInput,
    OverextendedEquityScanner,
    VolatilityDivergenceInput,
    VolatilityDivergenceScanner,
)
from .settings import Settings
from .sec_filings import SecCompanyFilingsProvider, SecFiling, filings_to_candidates
from .storage import ResearchStore
from .run_manifest import create_scan_run_manifest
from .unusual_activity import UnusualOptionsActivityInput, UnusualOptionsActivityScanner
from .yahoo import YahooHistoricalPriceProvider


EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class DiscoveryRun:
    """Records produced by one integrated universe scan."""

    as_of: datetime
    symbols: tuple[str, ...]
    selected_pairs: tuple[OptionPairSelection, ...]
    screen_candidates: tuple[Candidate, ...]
    discoveries: tuple[RankedDiscovery, ...]
    errors: tuple[str, ...]


def run_discovery(
    *,
    symbols: Sequence[str] | None = None,
    history_period: str = "1y",
    settings: Settings | None = None,
    market_provider=None,
    history_provider=None,
    include_sec_filings: bool = False,
    sec_provider=None,
    sec_days_back: int = 7,
) -> DiscoveryRun:
    """Enumerate liquid near-ATM pairs, run available screens, rank, and persist.

    Universe membership stays explicit (``THETAHAWK_SYMBOLS`` or ``--symbols``).
    Option-specific unusual-activity baselines use stored snapshots from before
    this scan, so the current observation cannot leak into its own history.
    """
    settings = settings or Settings.from_env()
    selected = tuple(dict.fromkeys(
        str(symbol).strip().upper()
        for symbol in (symbols or settings.symbols)
        if str(symbol).strip()
    ))
    if not selected:
        raise ValueError("At least one symbol is required")
    if sec_days_back < 1:
        raise ValueError("sec_days_back must be positive")
    market_provider = market_provider or AlpacaMarketDataProvider(settings)
    history_provider = history_provider or YahooHistoricalPriceProvider()
    collection_started_at = datetime.now(timezone.utc)
    today = collection_started_at.astimezone(EASTERN).date()
    try:
        equity_quotes = market_provider.get_equity_quotes(selected)
    except Exception as exc:
        equity_quotes = []
        errors = [f"equity quotes: {type(exc).__name__}: {exc}"]
    else:
        errors = []
    equity_by_symbol = {quote.symbol.upper(): quote for quote in equity_quotes}
    histories: dict[str, list] = {}
    chains: dict[str, list[Quote]] = {}

    for symbol in selected:
        try:
            histories[symbol] = history_provider.get_daily_prices(symbol, period=history_period)
        except Exception as exc:
            histories[symbol] = []
            errors.append(f"{symbol} history: {type(exc).__name__}: {exc}")
        try:
            start, end = today + timedelta(days=14), today + timedelta(days=45)
            expirations = market_provider.get_option_expirations(symbol, start=start, end=end)
            eligible = [expiry for expiry in expirations if start <= expiry <= end]
            if not eligible:
                chains[symbol] = []
                errors.append(f"{symbol} options: no listed expiration in 14-45 DTE window")
                continue
            expiry = min(eligible, key=lambda item: (abs((item - today).days - 30), item))
            quotes = market_provider.get_option_quotes(symbol, expiry)
            chains[symbol] = [
                quote for quote in quotes
                if quote.instrument_type.lower() == "option"
                and quote.symbol.upper() == symbol
                and quote.expiration == expiry
            ]
        except Exception as exc:
            chains[symbol] = []
            errors.append(f"{symbol} options: {type(exc).__name__}: {exc}")

    # Provider timestamps can be a fraction of a second newer than request
    # start; define the scan's evaluation time after all observations arrive.
    as_of = datetime.now(timezone.utc)
    sec_filings: list[SecFiling] = []
    sec_candidates: list[Candidate] = []
    if include_sec_filings:
        try:
            provider = sec_provider or SecCompanyFilingsProvider(user_agent=settings.sec_user_agent or "")
            sec_filings = provider.get_filings(
                selected,
                start=as_of.date() - timedelta(days=sec_days_back),
                end=as_of.date(),
            )
            sec_candidates = filings_to_candidates(sec_filings, as_of=as_of)
        except Exception as exc:
            errors.append(f"SEC filings: {type(exc).__name__}: {exc}")
    selected_pairs: list[OptionPairSelection] = []
    candidates: list[Candidate] = []
    equity_scanner = OverextendedEquityScanner()
    volatility_scanner = VolatilityDivergenceScanner()
    unusual_scanner = UnusualOptionsActivityScanner()
    with ResearchStore(settings.database_path) as store:
        unusual_inputs_by_candidate: dict[str, dict[str, object]] = {}
        for symbol in selected:
            underlying = equity_by_symbol.get(symbol) or Quote(
                symbol=symbol, observed_at=as_of, source="alpaca:stocks:unavailable"
            )
            pairs = select_option_pairs(
                chains.get(symbol, []), underlying, as_of,
                max_quote_age_seconds=settings.stale_quote_seconds,
            )
            selected_pairs.extend(pairs)
            for pair in pairs:
                candidates.append(_pair_candidate(pair, as_of))

            candidates.append(equity_scanner.scan(OverextendedEquityInput(
                symbol=symbol,
                as_of=as_of,
                quote=underlying,
                historical_prices=histories.get(symbol, []),
            )))
            candidates.append(volatility_scanner.scan(VolatilityDivergenceInput(
                symbol=symbol,
                as_of=as_of,
                underlying_quote=underlying,
                option_quotes=chains.get(symbol, []),
                historical_prices=histories.get(symbol, []),
                maximum_quote_age_seconds=settings.stale_quote_seconds,
            )))

            # Scan today's chain against prior stored observations. This builds
            # the baseline incrementally when a full chain is collected each session.
            previous = store.option_quotes(symbol, before_observed_at=as_of)
            previous_by_contract: dict[str, list[Quote]] = {}
            for row in previous:
                contract = row.metadata.get("contract_symbol")
                if contract:
                    previous_by_contract.setdefault(str(contract), []).append(row)
            pair_quotes = {
                str(quote.metadata.get("contract_symbol")): quote
                for pair in pairs
                for quote in (pair.call, pair.put)
                if quote.metadata.get("contract_symbol")
            }
            for contract, current in pair_quotes.items():
                history = previous_by_contract.get(contract, [])
                candidate = unusual_scanner.scan(UnusualOptionsActivityInput(
                    symbol=symbol,
                    as_of=as_of,
                    current=current,
                    history=history,
                ))
                candidates.append(candidate)
                unusual_inputs_by_candidate[candidate.candidate_id] = {
                    "symbol": symbol,
                    "current": current,
                    "history": history,
                }

        candidates.extend(sec_candidates)

        all_option_quotes = [quote for rows in chains.values() for quote in rows]
        all_history = [row for rows in histories.values() for row in rows]
        store.save_quotes([*equity_quotes, *all_option_quotes])
        store.save_historical_prices(all_history)
        store.save_candidates(candidates)
        # Persist a compact, inspectable ranked view. Raw per-screen evidence
        # remains alongside each discovery in the same candidates table.
        ranked = rank_discoveries(candidates)
        discovery_candidates = _discovery_records(ranked, as_of)
        store.save_candidates(discovery_candidates)
        manifest = create_scan_run_manifest(
            scanner="options_discovery",
            scanner_version="1",
            policy_version="integrated-discovery-policy-v1",
            as_of=as_of,
            candidate_ids=tuple(
                candidate.candidate_id for candidate in (*candidates, *discovery_candidates)
            ),
            inputs={
                "symbols": selected,
                "history_period": history_period,
                "evaluation_date": today,
                "stale_quote_seconds": settings.stale_quote_seconds,
                "selection_policy": {
                    "min_dte": 14,
                    "max_dte": 45,
                    "target_dte": 30,
                    "max_strike_deviation_fraction": 0.05,
                    "max_leg_spread_fraction": 0.20,
                    "min_option_volume": 1,
                    "min_open_interest": 1,
                    "max_pairs": 10,
                },
                "screen_policies": {
                    "overextended_equity": {
                        "window_sessions": 20,
                        "min_history_prices": 60,
                        "minimum_move_fraction": 0.05,
                        "minimum_robust_z_score": 3.5,
                    },
                    "volatility_divergence": {
                        "realized_window_sessions": 20,
                        "minimum_history_prices": 21,
                        "minimum_divergence_fraction": 0.25,
                        "minimum_option_volume": 1,
                        "minimum_open_interest": 1,
                        "maximum_quote_age_seconds": settings.stale_quote_seconds,
                        "maximum_spread_fraction": 0.20,
                        "maximum_open_interest_age_days": 5,
                    },
                    "unusual_options_activity": {
                        "minimum_history_sessions": 20,
                        "time_bucket_minutes": 15,
                        "minimum_current_volume": 50,
                        "minimum_robust_z_score": 3.5,
                        "maximum_quote_age_seconds": settings.stale_quote_seconds,
                    },
                },
                "equity_quotes": equity_quotes,
                "option_quotes_by_symbol": chains,
                "historical_prices_by_symbol": histories,
                "unusual_activity_inputs_by_candidate": unusual_inputs_by_candidate,
                "include_sec_filings": include_sec_filings,
                "sec_days_back": sec_days_back,
                "sec_filings": sec_filings,
            },
        )
        store.save_scan_run(manifest)

    return DiscoveryRun(
        as_of=as_of,
        symbols=selected,
        selected_pairs=tuple(selected_pairs),
        screen_candidates=tuple(candidates),
        discoveries=tuple(ranked),
        errors=tuple(errors),
    )


def _pair_candidate(pair: OptionPairSelection, as_of: datetime) -> Candidate:
    contracts = tuple(
        str(quote.metadata.get("contract_symbol", ""))
        for quote in (pair.call, pair.put)
        if quote.metadata.get("contract_symbol")
    )
    return Candidate(
        candidate_id=(
            f"{pair.symbol}:contract_selection:{pair.expiration.isoformat()}:{pair.strike:g}:"
            f"{as_of.strftime('%Y%m%dT%H%M%S')}"
        ),
        symbol=pair.symbol,
        scanner="contract_selection",
        score=None,
        created_at=as_of,
        status=CandidateStatus.RESEARCH,
        summary=f"Liquid near-ATM call/put pair selected for {pair.dte} DTE research.",
        rationale=(
            "Both legs passed quote freshness, spread, volume, open-interest, and moneyness filters.",
            "This is a contract shortlist entry, not a directional or profitability signal.",
        ),
        risks=("Indicative options quotes may not be executable.", "Selection does not assess strategy payoff or suitability."),
        input_ids=contracts,
        source_timestamps=tuple(
            quote.source_timestamp or quote.observed_at for quote in (pair.call, pair.put)
        ),
        metrics={
            "option_expiration": pair.expiration.isoformat(),
            "option_dte": pair.dte,
            "option_strike": pair.strike,
            "moneyness_deviation_fraction": round(pair.moneyness_deviation_fraction, 6),
            "call_spread_fraction": round(pair.call_spread_fraction, 6),
            "put_spread_fraction": round(pair.put_spread_fraction, 6),
            "combined_midpoint": round(pair.combined_midpoint, 4),
            "contract_symbols": list(contracts),
        },
    )


def _discovery_records(discoveries: Sequence[RankedDiscovery], as_of: datetime) -> list[Candidate]:
    output = []
    for item in discoveries:
        supporting = list(item.supporting_screens)
        statuses = [candidate.status for candidate in item.candidates]
        selected_contract = any(
            candidate.scanner == "contract_selection"
            and candidate.status == CandidateStatus.RESEARCH
            for candidate in item.candidates
        )
        status = (
            CandidateStatus.RESEARCH
            if selected_contract or CandidateStatus.RESEARCH in statuses
            else CandidateStatus.SUPPRESSED
        )
        option_metrics = next((
            candidate.metrics for candidate in item.candidates
            if candidate.scanner == "contract_selection"
        ), {})
        metrics = dict(option_metrics)
        metrics.update({
            "rank": item.rank,
            "research_screen_count": item.research_screen_count,
            "supporting_screens": supporting,
            "suppressed_screens": list(item.suppressed_screens),
            "ranking_basis": item.ranking_basis,
            "contract_symbols": list(item.contract_symbols),
            "discovery_id": item.discovery_id,
        })
        risks = tuple(dict.fromkeys(
            risk for candidate in item.candidates for risk in candidate.risks
        ))
        output.append(Candidate(
            candidate_id=f"{item.discovery_id}:{as_of.strftime('%Y%m%dT%H%M%S')}",
            symbol=item.symbol,
            scanner="options_discovery",
            score=float(item.research_screen_count),
            created_at=as_of,
            status=status,
            summary=item.ranking_basis,
            rationale=tuple(
                f"{candidate.scanner}: {candidate.summary}" for candidate in item.candidates
            ),
            risks=risks,
            input_ids=item.contract_symbols + tuple(
                candidate.candidate_id for candidate in item.candidates
            ),
            source_timestamps=tuple(dict.fromkeys(
                timestamp for candidate in item.candidates
                for timestamp in candidate.source_timestamps
            )),
            metrics=metrics,
        ))
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Run integrated read-only options discovery")
    parser.add_argument("--symbols", help="Comma-separated symbols; defaults to THETAHAWK_SYMBOLS")
    parser.add_argument("--history-period", choices=("6mo", "1y", "2y", "5y"), default="1y")
    parser.add_argument("--include-sec-filings", action="store_true",
                        help="Include recent SEC filing headers as contextual evidence")
    parser.add_argument("--sec-days-back", type=int, default=7,
                        help="Inclusive SEC filing lookback window (default: 7 days)")
    args = parser.parse_args()
    symbols = tuple(part.strip().upper() for part in args.symbols.split(",") if part.strip()) if args.symbols else None
    result = run_discovery(
        symbols=symbols,
        history_period=args.history_period,
        include_sec_filings=args.include_sec_filings,
        sec_days_back=args.sec_days_back,
    )
    print(
        f"Scanned {len(result.symbols)} symbols; selected {len(result.selected_pairs)} liquid near-ATM pairs; "
        f"ranked {len(result.discoveries)} discovery groups."
    )
    for item in result.discoveries[:20]:
        contracts = ", ".join(item.contract_symbols) or "underlying context"
        print(f"#{item.rank} {item.symbol} [{contracts}] — {item.ranking_basis}")
    for error in result.errors:
        print(f"Data unavailable for {error}")
    print(f"Results saved to {Settings.from_env().database_path}; no orders were placed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
