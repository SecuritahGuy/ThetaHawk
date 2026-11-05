"""Collect one listed option expiration per symbol for local research history."""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Protocol, Sequence

from .domain import Quote
from .providers import AlpacaMarketDataProvider
from .settings import Settings
from .storage import ResearchStore


logger = logging.getLogger(__name__)


class OptionSnapshotProvider(Protocol):
    def get_option_expirations(
        self, symbol: str, *, start: date | None = None, end: date | None = None
    ) -> list[date]: ...

    def get_option_quotes(self, symbol: str, expiration: date | None = None) -> list[Quote]: ...


class QuoteStore(Protocol):
    def save_quotes(self, quotes: Sequence[Quote]) -> int: ...


class ExpirationPlanStore(QuoteStore, Protocol):
    def option_snapshot_expiration(self, symbol: str) -> date | None: ...

    def save_option_snapshot_expiration(
        self, symbol: str, expiration: date, *, updated_at: datetime
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class OptionSnapshotCollectionResult:
    requested_symbols: int
    symbols_with_expiration: int
    symbols_without_eligible_expiration: tuple[str, ...]
    selected_expirations: dict[str, date]
    fetched_quotes: int
    saved_quotes: int
    as_of: date


def select_expiration(
    expirations: Sequence[date],
    *,
    as_of: date,
    min_dte: int = 14,
    max_dte: int = 45,
    target_dte: int = 30,
) -> date | None:
    """Choose the listed expiration nearest target DTE within an inclusive window."""
    _validate_dte_window(min_dte, max_dte, target_dte)
    eligible = [
        expiration for expiration in set(expirations)
        if min_dte <= (expiration - as_of).days <= max_dte
    ]
    if not eligible:
        return None
    return min(eligible, key=lambda expiry: (abs((expiry - as_of).days - target_dte), expiry))


def collect_option_snapshots(
    symbols: Sequence[str] | None = None,
    *,
    provider: OptionSnapshotProvider | None = None,
    store: QuoteStore | None = None,
    settings: Settings | None = None,
    as_of: date | None = None,
    min_dte: int = 14,
    max_dte: int = 45,
    target_dte: int = 30,
    preserve_expiration: bool = False,
) -> OptionSnapshotCollectionResult:
    """Fetch and persist snapshots for at most one eligible expiry per symbol.

    Each underlying's expiration list is requested once. Only the chosen expiry
    is sent to the snapshot API, and returned records are rechecked before save.
    With ``preserve_expiration=True``, the store pins the furthest listed
    expiration in the configured window and reuses it until it ages below
    ``min_dte``, giving exact contracts time to mature a session baseline.
    An internally opened DuckDB store is closed before returning; an injected
    store remains owned by its caller.
    """
    _validate_dte_window(min_dte, max_dte, target_dte)
    settings = settings or Settings.from_env()
    requested = tuple(dict.fromkeys(
        symbol.strip().upper()
        for symbol in (symbols or settings.symbols)
        if symbol.strip()
    ))
    if not requested:
        raise ValueError("At least one symbol is required")

    today = as_of or datetime.now(timezone.utc).date()
    provider = provider or AlpacaMarketDataProvider(settings)
    owns_store = store is None
    store = store or ResearchStore(settings.database_path)

    selected: dict[str, date] = {}
    missing: list[str] = []
    quotes: list[Quote] = []
    try:
        for symbol in requested:
            listed = provider.get_option_expirations(
                symbol,
                start=date.fromordinal(today.toordinal() + min_dte),
                end=date.fromordinal(today.toordinal() + max_dte),
            )
            if preserve_expiration:
                plan_store = store
                if not hasattr(plan_store, "option_snapshot_expiration") or not hasattr(
                    plan_store, "save_option_snapshot_expiration"
                ):
                    raise TypeError("preserve_expiration requires an expiration-plan capable store")
                planned = plan_store.option_snapshot_expiration(symbol)
                planned_dte = (planned - today).days if planned else -1
                if planned and min_dte <= planned_dte <= max_dte:
                    expiration = planned if planned in listed else None
                    if expiration is None:
                        logger.warning(
                            "Skipping %s option snapshot: pinned expiration %s is not in provider listing",
                            symbol,
                            planned,
                        )
                else:
                    eligible = [
                        expiry for expiry in set(listed)
                        if min_dte <= (expiry - today).days <= max_dte
                    ]
                    expiration = max(eligible) if eligible else None
                    if expiration is not None:
                        plan_store.save_option_snapshot_expiration(
                            symbol,
                            expiration,
                            updated_at=datetime.now(timezone.utc),
                        )
            else:
                expiration = select_expiration(
                    listed,
                    as_of=today,
                    min_dte=min_dte,
                    max_dte=max_dte,
                    target_dte=target_dte,
                )
            if expiration is None:
                missing.append(symbol)
                logger.warning(
                    "Skipping %s option snapshot: no listed expiration in %d-%d DTE window",
                    symbol,
                    min_dte,
                    max_dte,
                )
                continue

            selected[symbol] = expiration
            fetched = provider.get_option_quotes(symbol, expiration)
            quotes.extend(
                quote for quote in fetched
                if quote.instrument_type.lower() == "option"
                and quote.symbol.upper() == symbol
                and quote.expiration == expiration
            )
        saved = store.save_quotes(quotes)
    finally:
        if owns_store:
            store.close()

    return OptionSnapshotCollectionResult(
        requested_symbols=len(requested),
        symbols_with_expiration=len(selected),
        symbols_without_eligible_expiration=tuple(missing),
        selected_expirations=selected,
        fetched_quotes=len(quotes),
        saved_quotes=saved,
        as_of=today,
    )


def _validate_dte_window(min_dte: int, max_dte: int, target_dte: int) -> None:
    if min_dte < 0 or max_dte < min_dte:
        raise ValueError("DTE window must satisfy 0 <= min_dte <= max_dte")
    if not min_dte <= target_dte <= max_dte:
        raise ValueError("target_dte must fall within the configured DTE window")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect read-only Alpaca option snapshots for one expiry per symbol"
    )
    parser.add_argument("--symbols", help="Comma-separated symbols; defaults to THETAHAWK_SYMBOLS")
    parser.add_argument("--min-dte", type=int, default=14)
    parser.add_argument("--max-dte", type=int, default=45)
    parser.add_argument("--target-dte", type=int, default=30)
    args = parser.parse_args()
    symbols = tuple(part.strip().upper() for part in args.symbols.split(",") if part.strip()) if args.symbols else None
    result = collect_option_snapshots(
        symbols,
        min_dte=args.min_dte,
        max_dte=args.max_dte,
        target_dte=args.target_dte,
    )
    print(
        f"Requested {result.requested_symbols} symbols; collected {result.fetched_quotes} "
        f"quotes across {result.symbols_with_expiration} expirations and saved "
        f"{result.saved_quotes} snapshots. No eligible expiration: "
        f"{', '.join(result.symbols_without_eligible_expiration) or 'none'}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
