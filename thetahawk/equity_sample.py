"""Deterministically sample listed U.S. equities, collect Yahoo history, and validate the screen."""

from __future__ import annotations

import argparse
import csv
import io
import json
import random
import re
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from .equity_backtest import walk_forward_overextended
from .providers import AlpacaMarketDataProvider
from .settings import Settings
from .storage import ResearchStore
from .yahoo import YahooHistoricalPriceProvider


NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
EASTERN = ZoneInfo("America/New_York")
_SIMPLE_SYMBOL = re.compile(r"^[A-Z]{1,5}$")
_NON_COMMON = re.compile(
    r"\b(warrants?|rights?|preferred|depositary|units?|notes?|debentures?|"
    r"exchange\s+traded\s+fund|funds?|\betf\b|\betn\b|bonds?|indices|index|"
    r"closed-end|investment\s+company)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ListedEquity:
    symbol: str
    security_name: str
    listing_source: str


def parse_symbol_directories(nasdaq_text: str, other_text: str) -> list[ListedEquity]:
    """Parse current Nasdaq Trader directories, filtering ETFs, tests, and non-common symbols."""
    rows: dict[str, ListedEquity] = {}
    for text, directory in ((nasdaq_text, "nasdaqlisted.txt"),
                            (other_text, "otherlisted.txt")):
        for row in csv.DictReader(io.StringIO(text), delimiter="|"):
            symbol = (
                row.get("Symbol") or row.get("CQS Symbol") or row.get("ACT Symbol") or ""
            ).strip().upper()
            name = (row.get("Security Name") or "").strip()
            if not _SIMPLE_SYMBOL.fullmatch(symbol):
                continue
            if str(row.get("Test Issue", "N")).strip().upper() == "Y":
                continue
            if str(row.get("ETF", "N")).strip().upper() == "Y" or _NON_COMMON.search(name):
                continue
            rows.setdefault(symbol, ListedEquity(symbol, name, directory))
    return [rows[symbol] for symbol in sorted(rows)]


def fetch_symbol_universe(*, timeout_seconds: float = 20.0) -> tuple[list[ListedEquity], dict[str, Any]]:
    """Fetch the two official Nasdaq Trader symbol directories with provenance."""
    session = requests.Session()
    session.headers.update({"User-Agent": "Thetahawk personal research data collector"})
    retrieved_at = datetime.now(timezone.utc)
    responses = {}
    for name, url in (("nasdaqlisted.txt", NASDAQ_LISTED_URL),
                      ("otherlisted.txt", OTHER_LISTED_URL)):
        response = session.get(url, timeout=timeout_seconds)
        response.raise_for_status()
        responses[name] = response.text
    universe = parse_symbol_directories(
        responses["nasdaqlisted.txt"], responses["otherlisted.txt"]
    )
    return universe, {
        "source": "Nasdaq Trader Symbol Directory",
        "retrieved_at": retrieved_at.isoformat(),
        "files": [NASDAQ_LISTED_URL, OTHER_LISTED_URL],
        "file_creation_times": {
            "nasdaqlisted.txt": _file_creation_time(responses["nasdaqlisted.txt"]),
            "otherlisted.txt": _file_creation_time(responses["otherlisted.txt"]),
        },
    }


def _file_creation_time(text: str) -> str | None:
    for line in reversed(text.splitlines()):
        first_field = line.split("|", 1)[0]
        if first_field.lower().startswith("file creation time:"):
            return first_field.partition(":")[2].strip()
    return None


def check_option_expiration_coverage(
    symbols: list[str],
    *,
    provider: Any,
    as_of: date,
    min_dte: int = 14,
    max_dte: int = 45,
    request_spacing_seconds: float = 0.25,
    sleep=time.sleep,
) -> dict[str, Any]:
    """Check which sampled symbols have listed options, without fetching quotes."""
    if min_dte < 0 or max_dte < min_dte:
        raise ValueError("option DTE window must satisfy 0 <= min_dte <= max_dte")
    if request_spacing_seconds < 0:
        raise ValueError("request_spacing_seconds cannot be negative")
    rows = []
    for index, symbol in enumerate(symbols):
        try:
            expirations = provider.get_option_expirations(
                symbol,
                start=as_of + timedelta(days=min_dte),
                end=as_of + timedelta(days=max_dte),
            )
            rows.append({
                "symbol": symbol,
                "status": "listed" if expirations else "no_expirations_in_window",
                "expiration_count": len(expirations),
                "expirations": [day.isoformat() for day in expirations],
            })
        except Exception as exc:
            rows.append({
                "symbol": symbol,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            })
        if index + 1 < len(symbols) and request_spacing_seconds:
            sleep(request_spacing_seconds)
    return {
        "provider": getattr(provider, "name", type(provider).__name__),
        "as_of_date": as_of.isoformat(),
        "dte_window": {"min": min_dte, "max": max_dte},
        "symbols_checked": len(rows),
        "symbols_with_expirations": sum(row["status"] == "listed" for row in rows),
        "symbols_without_expirations": sum(
            row["status"] == "no_expirations_in_window" for row in rows
        ),
        "failures": [row for row in rows if row["status"] == "error"],
        "results": rows,
        "quotes_fetched": False,
    }


def run_random_equity_backtest(
    *,
    count: int = 20,
    seed: int,
    period: str = "5y",
    settings: Settings | None = None,
    output_path: str | Path | None = None,
    check_options: bool = False,
    option_provider: Any | None = None,
    option_request_spacing_seconds: float = 0.25,
) -> dict[str, Any]:
    """Select a reproducible random sample, fetch history, persist it, and run screen validation."""
    if count < 1:
        raise ValueError("count must be positive")
    settings = settings or Settings.from_env()
    universe, universe_provenance = fetch_symbol_universe()
    excluded = {symbol.upper() for symbol in settings.symbols}
    pool = [row for row in universe if row.symbol not in excluded]
    if count > len(pool):
        raise ValueError(f"requested {count} symbols, but only {len(pool)} are eligible")
    selected = random.Random(seed).sample(pool, count)

    history_provider = YahooHistoricalPriceProvider()
    gathered = []
    history_results = []
    failures = []
    for listing in selected:
        try:
            prices = history_provider.get_daily_prices(listing.symbol, period=period)
        except Exception as exc:
            failures.append({
                "symbol": listing.symbol,
                "error": f"{type(exc).__name__}: {exc}",
            })
            history_results.append({"symbol": listing.symbol, "rows": 0, "status": "error"})
            continue
        gathered.extend(prices)
        history_results.append({
            "symbol": listing.symbol,
            "rows": len(prices),
            "first_session": prices[0].session_date.isoformat() if prices else None,
            "last_session": prices[-1].session_date.isoformat() if prices else None,
            "status": "collected" if prices else "empty",
        })
        if not prices:
            failures.append({"symbol": listing.symbol, "error": "Yahoo returned no daily prices"})

    if gathered:
        with ResearchStore(settings.database_path) as store:
            stored_rows = store.save_historical_prices(gathered)
    else:
        stored_rows = 0

    as_of = datetime.now(EASTERN).date()
    screen_report = walk_forward_overextended(gathered, as_of=as_of)
    option_coverage = None
    if check_options:
        try:
            option_provider = option_provider or AlpacaMarketDataProvider(settings)
            option_coverage = check_option_expiration_coverage(
                [row.symbol for row in selected],
                provider=option_provider,
                as_of=as_of,
                request_spacing_seconds=option_request_spacing_seconds,
            )
        except Exception as exc:
            option_coverage = {
                "provider": "alpaca",
                "as_of_date": as_of.isoformat(),
                "error": f"{type(exc).__name__}: {exc}",
                "quotes_fetched": False,
            }
    report = {
        "report_type": "random_universe_equity_screen_validation",
        "research_only": True,
        "strategy_backtest": False,
        "seed": seed,
        "requested_sample_size": count,
        "as_of_date_exclusive": as_of.isoformat(),
        "symbol_universe": universe_provenance,
        "eligible_universe_size": len(pool),
        "excluded_core_symbols": sorted(excluded),
        "sample": [asdict(row) for row in selected],
        "history_period": period,
        "history_results": history_results,
        "history_failures": failures,
        "history_rows_fetched": len(gathered),
        "history_rows_saved": stored_rows,
        "option_coverage": option_coverage,
        "screen_validation": screen_report,
        "limitations": [
            "The universe is a current listing snapshot, so delisted firms are absent and survivorship bias remains.",
            "The sample is random over eligible plain-letter symbols, not market-cap or liquidity weighted.",
            "Yahoo history may be unavailable or incomplete for some sampled tickers; failures remain in the report.",
            "The embedded screen study uses adjusted daily closes and does not test options, fills, fees, or slippage.",
            "Optional option coverage checks only for listed expirations; they do not assess quote freshness, spread, volume, or open interest.",
        ],
    }
    destination = Path(output_path) if output_path else (
        settings.data_dir / f"random_equity_backtest_{seed}.json"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    report["report_path"] = str(destination)
    destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Randomly sample listed U.S. equities, collect Yahoo history, and validate the equity screen"
    )
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--seed", type=int, default=int(datetime.now(timezone.utc).strftime("%Y%m%d")))
    parser.add_argument("--period", choices=("1y", "2y", "5y", "10y", "max"), default="5y")
    parser.add_argument("--check-options", action="store_true",
                        help="Check listed expirations via Alpaca without fetching option quotes")
    parser.add_argument("--output", type=Path, help="JSON report path (defaults to THETAHAWK_DATA_DIR)")
    args = parser.parse_args()
    try:
        report = run_random_equity_backtest(
            count=args.count,
            seed=args.seed,
            period=args.period,
            output_path=args.output,
            check_options=args.check_options,
        )
    except (requests.RequestException, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    summary = report["screen_validation"]
    print(
        f"Sampled {report['requested_sample_size']} symbols with seed {report['seed']}; "
        f"saved {report['history_rows_saved']} history rows; "
        f"observed {summary['threshold_crossing_days']} threshold days in "
        f"{summary['episode_count']} episodes. Report: {report['report_path']}"
    )
    if report["option_coverage"]:
        option_coverage = report["option_coverage"]
        if "symbols_with_expirations" in option_coverage:
            print(
                f"Listed options in the {option_coverage['dte_window']['min']}-"
                f"{option_coverage['dte_window']['max']} DTE window for "
                f"{option_coverage['symbols_with_expirations']}/"
                f"{option_coverage['symbols_checked']} symbols (no quotes fetched)."
            )
    if report["history_failures"]:
        print(f"Yahoo history unavailable for {len(report['history_failures'])} sampled symbols.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
