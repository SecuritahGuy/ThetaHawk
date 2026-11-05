"""One-shot IV versus realized-volatility scan."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from typing import Sequence
from zoneinfo import ZoneInfo

from .domain import Candidate, Quote
from .providers import AlpacaMarketDataProvider
from .scanners import (
    VolatilityDivergenceInput,
    VolatilityDivergenceScanner,
)
from .settings import Settings
from .storage import ResearchStore
from .yahoo import YahooHistoricalPriceProvider


EASTERN = ZoneInfo("America/New_York")


def run_volatility_scan(
    *,
    symbols: Sequence[str] | None = None,
    history_period: str = "1y",
    settings: Settings | None = None,
    market_provider=None,
    history_provider=None,
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
    equity_quotes = market_provider.get_equity_quotes(selected)
    equity_by_symbol = {quote.symbol.upper(): quote for quote in equity_quotes}
    histories = {}
    options_by_symbol: dict[str, list[Quote]] = {}
    errors: list[str] = []
    today = datetime.now(EASTERN).date()

    for symbol in selected:
        try:
            histories[symbol] = history_provider.get_daily_prices(
                symbol, period=history_period
            )
        except Exception as exc:
            histories[symbol] = []
            errors.append(f"{symbol} history: {type(exc).__name__}: {exc}")
        try:
            expirations = market_provider.get_option_expirations(
                symbol,
                start=today + timedelta(days=14),
                end=today + timedelta(days=45),
            )
            if expirations:
                target = min(expirations, key=lambda expiry: abs((expiry - today).days - 30))
                options_by_symbol[symbol] = market_provider.get_option_quotes(
                    symbol, target
                )
            else:
                options_by_symbol[symbol] = []
        except Exception as exc:
            options_by_symbol[symbol] = []
            errors.append(f"{symbol} options: {type(exc).__name__}: {exc}")

    as_of = datetime.now(timezone.utc)
    scanner = VolatilityDivergenceScanner()
    candidates: list[Candidate] = []
    for symbol in selected:
        equity = equity_by_symbol.get(symbol) or Quote(
            symbol=symbol,
            observed_at=as_of,
            source="alpaca:stocks:unavailable",
        )
        candidates.append(scanner.scan(VolatilityDivergenceInput(
            symbol=symbol,
            as_of=as_of,
            underlying_quote=equity,
            option_quotes=options_by_symbol[symbol],
            historical_prices=histories[symbol],
        )))

    option_quotes = [quote for rows in options_by_symbol.values() for quote in rows]
    historical_prices = [row for rows in histories.values() for row in rows]
    quotes = [*equity_quotes, *option_quotes]
    with ResearchStore(settings.database_path) as store:
        store.save_quotes(quotes)
        store.save_historical_prices(historical_prices)
        store.save_candidates(candidates)

    research_count = sum(candidate.status.value == "research" for candidate in candidates)
    print(
        f"Scanned {len(selected)} symbols; stored {len(historical_prices)} Yahoo daily closes, "
        f"{len(quotes)} Alpaca quotes, and {len(candidates)} candidates "
        f"({research_count} flagged) in {settings.database_path}."
    )
    for error in errors:
        print(f"Data unavailable for {error}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare current near-ATM option IV with realized volatility"
    )
    parser.add_argument("--symbols", help="Comma-separated symbols; defaults to THETAHAWK_SYMBOLS")
    parser.add_argument(
        "--history-period",
        choices=("6mo", "1y", "2y", "5y"),
        default="1y",
    )
    args = parser.parse_args()
    symbols = tuple(part.strip().upper() for part in args.symbols.split(",") if part.strip()) if args.symbols else None
    return run_volatility_scan(symbols=symbols, history_period=args.history_period)


if __name__ == "__main__":
    raise SystemExit(main())
