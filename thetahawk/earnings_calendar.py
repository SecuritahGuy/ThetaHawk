"""Export Yahoo earnings-date observations for operator review."""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from .domain import EarningsEvent


SOURCE = "yahoo-finance:yfinance-earnings-dates"
CSV_FIELDS = (
    "symbol",
    "event_date",
    "timing",
    "expiration_date",
    "observed_at",
    "source",
)


class YahooEarningsCalendarProvider:
    """Read Yahoo's reported earnings dates without guessing announcement time.

    Yahoo's returned earnings date rows do not reliably identify before-open
    versus after-close timing. Consequently each event has ``timing=None`` and
    a missing option expiration; an analyst must review and fill those fields.
    """

    name = SOURCE

    def __init__(self, ticker_factory: Callable[[str], Any] | None = None) -> None:
        if ticker_factory is None:
            try:
                import yfinance as yf
            except ImportError as exc:  # pragma: no cover - installation dependent
                raise RuntimeError(
                    "Yahoo earnings dates need yfinance; install with `python -m pip install -e '.[yahoo]'`."
                ) from exc
            ticker_factory = yf.Ticker
        self._ticker_factory = ticker_factory

    def get_earnings_events(
        self,
        symbols: Sequence[str],
        start: date,
        end: date,
        *,
        limit_per_symbol: int = 100,
    ) -> list[EarningsEvent]:
        if end < start:
            raise ValueError("end date must be on or after start date")
        if limit_per_symbol < 1:
            raise ValueError("limit_per_symbol must be positive")

        events: list[EarningsEvent] = []
        for raw_symbol in symbols:
            symbol = raw_symbol.strip().upper()
            if not symbol:
                continue
            rows = self._ticker_factory(symbol).get_earnings_dates(
                limit=limit_per_symbol,
                offset=0,
            )
            if rows is None or getattr(rows, "empty", False):
                continue

            observed_at = datetime.now(timezone.utc)
            seen_dates: set[date] = set()
            for index, _row in rows.iterrows():
                try:
                    event_date = _event_date(index)
                except (TypeError, ValueError):
                    continue
                if event_date in seen_dates or not start <= event_date <= end:
                    continue
                seen_dates.add(event_date)
                events.append(EarningsEvent(
                    symbol=symbol,
                    event_date=event_date,
                    timing=None,
                    observed_at=observed_at,
                    source=self.name,
                    source_timestamp=None,
                    metadata={
                        "event_id": f"{symbol}:{event_date.isoformat()}:{self.name}",
                        "timing_status": "unknown_requires_review",
                        "expiration_date": None,
                    },
                ))

        return sorted(events, key=lambda event: (event.event_date, event.symbol))


def export_review_csv(events: Sequence[EarningsEvent], output: Path, *, overwrite: bool = False) -> int:
    """Write review rows in the existing earnings CSV input schema."""
    if output.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite existing file: {output}; pass --overwrite to replace it")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for event in events:
            writer.writerow({
                "symbol": event.symbol,
                "event_date": event.event_date.isoformat(),
                "timing": event.timing or "",
                "expiration_date": "",
                "observed_at": event.observed_at.astimezone(timezone.utc).isoformat(),
                "source": event.source,
            })
    return len(events)


def _event_date(index: object) -> date:
    if isinstance(index, datetime):
        return index.date()
    if isinstance(index, date):
        return index
    if hasattr(index, "date"):
        parsed = index.date()
        if isinstance(parsed, date):
            return parsed
    return date.fromisoformat(str(index)[:10])


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export Yahoo earnings dates to a reviewable Thetahawk CSV"
    )
    parser.add_argument("--symbols", required=True, help="Comma-separated symbols")
    parser.add_argument("--start", required=True, type=date.fromisoformat, help="Inclusive YYYY-MM-DD")
    parser.add_argument("--end", required=True, type=date.fromisoformat, help="Inclusive YYYY-MM-DD")
    parser.add_argument("--limit-per-symbol", type=int, default=100)
    parser.add_argument("--output", type=Path, default=Path("./data/yahoo_earnings_review.csv"))
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing output file")
    args = parser.parse_args()
    symbols = tuple(dict.fromkeys(
        token.strip().upper() for token in args.symbols.split(",") if token.strip()
    ))
    if not symbols:
        parser.error("At least one non-empty symbol is required")
    provider = YahooEarningsCalendarProvider()
    events = provider.get_earnings_events(
        symbols,
        args.start,
        args.end,
        limit_per_symbol=args.limit_per_symbol,
    )
    count = export_review_csv(events, args.output, overwrite=args.overwrite)
    print(f"Exported {count} event dates to {args.output} for review.")
    print("Timing and option expiration are intentionally blank; review before using as scan inputs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
