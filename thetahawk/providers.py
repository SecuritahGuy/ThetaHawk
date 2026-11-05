"""Provider interfaces and a deterministic demo data provider."""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol, Sequence

from .domain import EarningsEvent, Quote


class MarketDataProvider(Protocol):
    name: str

    def get_equity_quotes(self, symbols: Sequence[str]) -> list[Quote]: ...

    def get_option_quotes(
        self, symbol: str, expiration: date | None = None
    ) -> list[Quote]: ...


class EventDataProvider(Protocol):
    name: str

    def get_earnings_events(
        self, symbols: Sequence[str], start: date, end: date
    ) -> list[EarningsEvent]: ...


class DemoProvider:
    """Small repeatable fixture provider; values are fictional and labeled."""

    name = "demo-fictional"

    def __init__(self, as_of: datetime | None = None) -> None:
        from datetime import timezone

        self.as_of = as_of or datetime.now(timezone.utc)

    def get_equity_quotes(self, symbols: Sequence[str]) -> list[Quote]:
        # Stable per-symbol prices make UI and scanner behavior reproducible.
        reference = {"AAPL": 225.0, "AMZN": 220.0, "ARM": 145.0, "GOOG": 185.0, "MSFT": 510.0, "NVDA": 190.0}
        result = []
        for raw_symbol in symbols:
            symbol = raw_symbol.upper()
            price = reference.get(symbol, 100.0)
            result.append(Quote(
                symbol=symbol,
                observed_at=self.as_of,
                source_timestamp=self.as_of,
                bid=round(price * 0.999, 2),
                ask=round(price * 1.001, 2),
                last=price,
                volume=1_000_000,
                source=self.name,
                metadata={"fictional": True},
            ))
        return result

    def get_option_quotes(self, symbol: str, expiration: date | None = None) -> list[Quote]:
        # Demo fixture deliberately empty: synthetic options prices must not be
        # confused with executable market quotes or used as real trade signals.
        return []

    def get_earnings_events(
        self, symbols: Sequence[str], start: date, end: date
    ) -> list[EarningsEvent]:
        return []
