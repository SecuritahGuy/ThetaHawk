"""Collect prospective option snapshots for reviewed upcoming earnings events."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Protocol, Sequence
from zoneinfo import ZoneInfo

from .csv_inputs import CsvResearchInputs
from .domain import EarningsEvent, Quote
from .providers import AlpacaMarketDataProvider
from .settings import Settings
from .storage import ResearchStore


EASTERN = ZoneInfo("America/New_York")
_EVENT_TIMES = {"before_open": time(8, 15), "after_close": time(16, 15)}


class EarningsSnapshotProvider(Protocol):
    name: str

    def get_option_quotes(self, symbol: str, expiration: date | None = None) -> list[Quote]: ...


class QuoteStore(Protocol):
    def save_quotes(self, quotes: Sequence[Quote]) -> int: ...


class EarningsInputStore(QuoteStore, Protocol):
    def save_earnings_events(self, events: Sequence[EarningsEvent]) -> int: ...


@dataclass(frozen=True, slots=True)
class EarningsSnapshotResult:
    requested_symbols: tuple[str, ...]
    reviewed_events: int
    eligible_events: int
    requested_chains: int
    failed_chains: tuple[str, ...]
    skipped_events: tuple[str, ...]
    saved_events: int
    fetched_quotes: int
    saved_quotes: int
    as_of: datetime


def collect_earnings_option_snapshots(
    symbols: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    provider: EarningsSnapshotProvider | None = None,
    store: EarningsInputStore | None = None,
    as_of: datetime | None = None,
    days_ahead: int = 45,
) -> EarningsSnapshotResult:
    """Save quote-level snapshots for reviewed events and event-covering expiries.

    Upcoming events are read from the configured earnings CSV. Rows with
    unreviewed timing, no chosen expiration, a past event time, or expiration
    before the event are excluded. Each `(symbol, expiration)` option chain is
    fetched once even when several reviewed events use it. Stored quote metadata
    preserves the associated event IDs/dates and input source provenance.
    """
    if days_ahead < 0:
        raise ValueError("days_ahead cannot be negative")
    settings = settings or Settings.from_env()
    requested = tuple(dict.fromkeys(
        str(symbol).strip().upper()
        for symbol in (symbols or settings.symbols)
        if str(symbol).strip()
    ))
    if not requested:
        raise ValueError("At least one symbol is required")
    scan_time = as_of or datetime.now(timezone.utc)
    if scan_time.tzinfo is None or scan_time.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    today = scan_time.astimezone(EASTERN).date()
    inputs = CsvResearchInputs(settings)
    events = inputs.get_earnings_events(
        requested,
        start=today,
        end=today + timedelta(days=days_ahead),
    )

    grouped: dict[tuple[str, date], list[EarningsEvent]] = {}
    skipped: list[str] = []
    for event in events:
        event_id = str(event.metadata.get("event_id") or _event_id(event))
        event_observed_at = event.source_timestamp or event.observed_at
        if event_observed_at > scan_time:
            skipped.append(f"{event_id}: event record was observed after the collection time")
            continue
        event_at = _event_datetime(event)
        if event_at is None:
            skipped.append(f"{event_id}: timing is unreviewed or unsupported")
            continue
        if event_at <= scan_time:
            skipped.append(f"{event_id}: event is no longer upcoming")
            continue
        expiration_raw = event.metadata.get("expiration_date")
        if not expiration_raw:
            skipped.append(f"{event_id}: expiration is not reviewed")
            continue
        expiration = date.fromisoformat(str(expiration_raw))
        expiration_at = datetime.combine(expiration, time(16, 0), tzinfo=EASTERN)
        if expiration_at < event_at:
            skipped.append(f"{event_id}: expiration precedes the event")
            continue
        grouped.setdefault((event.symbol.upper(), expiration), []).append(event)

    provider = provider or AlpacaMarketDataProvider(settings)
    owns_store = store is None
    store = store or ResearchStore(settings.database_path)
    all_quotes: list[Quote] = []
    failed: list[str] = []
    eligible_events = sum(len(rows) for rows in grouped.values())
    try:
        saved_events = store.save_earnings_events(events)
        for (symbol, expiration), matching_events in grouped.items():
            try:
                fetched = provider.get_option_quotes(symbol, expiration)
            except Exception as exc:
                failed.append(f"{symbol} {expiration.isoformat()}: {type(exc).__name__}: {exc}")
                continue
            event_refs = [
                {
                    "event_id": str(event.metadata.get("event_id") or _event_id(event)),
                    "event_date": event.event_date.isoformat(),
                    "timing": event.timing,
                    "event_source": event.source,
                    "event_observed_at": event.observed_at.isoformat(),
                }
                for event in matching_events
            ]
            for quote in fetched:
                if (
                    quote.instrument_type.lower() != "option"
                    or quote.symbol.upper() != symbol
                    or quote.expiration != expiration
                ):
                    continue
                metadata = dict(quote.metadata)
                metadata["earnings_events"] = event_refs
                metadata["snapshot_purpose"] = "reviewed_earnings_event"
                all_quotes.append(replace(quote, metadata=metadata))
        saved = store.save_quotes(all_quotes)
    finally:
        if owns_store:
            store.close()

    return EarningsSnapshotResult(
        requested_symbols=requested,
        reviewed_events=len(events),
        eligible_events=eligible_events,
        requested_chains=len(grouped),
        failed_chains=tuple(failed),
        skipped_events=tuple(skipped),
        saved_events=saved_events,
        fetched_quotes=len(all_quotes),
        saved_quotes=saved,
        as_of=scan_time,
    )


def _event_datetime(event: EarningsEvent) -> datetime | None:
    local_time = _EVENT_TIMES.get((event.timing or "").strip().lower())
    if local_time is None:
        return None
    return datetime.combine(event.event_date, local_time, tzinfo=EASTERN)


def _event_id(event: EarningsEvent) -> str:
    return f"{event.symbol.upper()}:{event.event_date.isoformat()}:{event.source}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect read-only option snapshots for reviewed upcoming earnings"
    )
    parser.add_argument("--symbols", help="Comma-separated symbols; defaults to THETAHAWK_SYMBOLS")
    parser.add_argument("--days-ahead", type=int, default=45)
    args = parser.parse_args()
    symbols = tuple(part.strip().upper() for part in args.symbols.split(",") if part.strip()) if args.symbols else None
    result = collect_earnings_option_snapshots(symbols, days_ahead=args.days_ahead)
    print(
        f"Reviewed {result.reviewed_events} upcoming event rows; selected "
        f"persisted {result.saved_events} input events and selected {result.eligible_events} "
        f"across {result.requested_chains} distinct chains; "
        f"saved {result.saved_quotes}/{result.fetched_quotes} quotes."
    )
    if result.skipped_events:
        print("Skipped events: " + "; ".join(result.skipped_events))
    if result.failed_chains:
        print("Failed chains: " + "; ".join(result.failed_chains))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
