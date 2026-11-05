"""One-shot overextended-equity scan using Alpaca quotes and Yahoo history."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import Sequence

from .domain import Candidate, Quote
from .providers import AlpacaMarketDataProvider
from .scanners import OverextendedEquityInput, OverextendedEquityScanner
from .settings import Settings
from .storage import ResearchStore
from .yahoo import YahooHistoricalPriceProvider


def run_equity_scan(
    *,
    symbols: Sequence[str] | None = None,
    history_period: str = "1y",
    settings: Settings | None = None,
    market_provider: AlpacaMarketDataProvider | None = None,
    history_provider: YahooHistoricalPriceProvider | None = None,
) -> int:
    settings = settings or Settings.from_env()
    selected = tuple(dict.fromkeys(
        symbol.strip().upper()
        for symbol in (symbols or settings.symbols)
        if symbol.strip()
    ))
    if not selected:
        raise ValueError("At least one symbol is required")

    market_provider = market_provider or AlpacaMarketDataProvider(settings)
    history_provider = history_provider or YahooHistoricalPriceProvider()
    quotes = market_provider.get_equity_quotes(selected)
    quote_by_symbol = {quote.symbol.upper(): quote for quote in quotes}

    historical_by_symbol = {}
    errors: list[str] = []
    for symbol in selected:
        try:
            historical_by_symbol[symbol] = history_provider.get_daily_prices(
                symbol, period=history_period
            )
        except Exception as exc:
            historical_by_symbol[symbol] = []
            errors.append(f"{symbol}: {type(exc).__name__}: {exc}")

    as_of = datetime.now(timezone.utc)
    scanner = OverextendedEquityScanner()
    candidates: list[Candidate] = []
    for symbol in selected:
        quote = quote_by_symbol.get(symbol) or Quote(
            symbol=symbol,
            observed_at=as_of,
            source="alpaca:stocks:unavailable",
        )
        candidates.append(scanner.scan(OverextendedEquityInput(
            symbol=symbol,
            as_of=as_of,
            quote=quote,
            historical_prices=historical_by_symbol[symbol],
        )))

    all_prices = [price for rows in historical_by_symbol.values() for price in rows]
    with ResearchStore(settings.database_path) as store:
        saved_prices = store.save_historical_prices(all_prices)
        store.save_quotes(quotes)
        store.save_candidates(candidates)

    research_count = sum(candidate.status.value == "research" for candidate in candidates)
    print(
        f"Scanned {len(selected)} symbols; stored {saved_prices} Yahoo daily closes, "
        f"{len(quotes)} Alpaca quotes, and {len(candidates)} candidates "
        f"({research_count} flagged) in {settings.database_path}."
    )
    print("Read-only mode: no account, position, or order endpoints are used.")
    for error in errors:
        print(f"History unavailable for {error}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a read-only overextended-equity screen"
    )
    parser.add_argument("--symbols", help="Comma-separated symbols; defaults to THETAHAWK_SYMBOLS")
    parser.add_argument(
        "--history-period",
        choices=("6mo", "1y", "2y", "5y"),
        default="1y",
        help="Yahoo Finance daily history lookback (default: 1y)",
    )
    args = parser.parse_args()
    symbols = tuple(part.strip().upper() for part in args.symbols.split(",") if part.strip()) if args.symbols else None
    return run_equity_scan(symbols=symbols, history_period=args.history_period)


if __name__ == "__main__":
    raise SystemExit(main())
