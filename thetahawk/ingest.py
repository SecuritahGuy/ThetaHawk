"""One-shot read-only ingestion of current Alpaca market snapshots."""

from __future__ import annotations

import argparse

from .providers import AlpacaMarketDataProvider
from .settings import Settings
from .storage import ResearchStore


def main() -> int:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(description="Fetch and store read-only Alpaca market quotes")
    parser.add_argument(
        "--symbols",
        help="Comma-separated symbols (defaults to THETAHAWK_SYMBOLS)",
    )
    parser.add_argument(
        "--no-options",
        action="store_true",
        help="Only request equity quotes",
    )
    args = parser.parse_args()
    symbols = tuple(
        dict.fromkeys(
            symbol.strip().upper()
            for symbol in (args.symbols or ",".join(settings.symbols)).split(",")
            if symbol.strip()
        )
    )
    if not symbols:
        parser.error("At least one symbol is required")

    provider = AlpacaMarketDataProvider(settings)
    quotes = provider.get_equity_quotes(symbols)
    if not args.no_options:
        for symbol in symbols:
            quotes.extend(provider.get_option_quotes(symbol))

    with ResearchStore(settings.database_path) as store:
        saved = store.save_quotes(quotes)
    print(
        f"Saved {saved} Alpaca quote snapshots for {len(symbols)} symbols to "
        f"{settings.database_path} (stock feed={settings.alpaca_stock_feed}, "
        f"options feed={settings.alpaca_options_feed})"
    )
    print("Data ingestion is read-only; no account, position, or order endpoints are used.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
