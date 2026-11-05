"""Yahoo Finance history adapter used for temporary, read-only research inputs."""

from __future__ import annotations

from datetime import date, datetime, timezone
from math import isfinite
from typing import Any, Callable

from .domain import HistoricalPrice


class YahooHistoricalPriceProvider:
    """Fetch adjusted daily closes through the optional ``yfinance`` package.

    Yahoo's daily history is treated as session-date data. The provider records
    when each batch was fetched, but does not invent an intraday quote time.
    """

    name = "yahoo-finance:yfinance"

    def __init__(self, ticker_factory: Callable[[str], Any] | None = None) -> None:
        if ticker_factory is None:
            try:
                import yfinance as yf
            except ImportError as exc:  # pragma: no cover - install dependent
                raise RuntimeError(
                    "Yahoo history needs yfinance; install with `python -m pip install -e '.[yahoo]'`."
                ) from exc
            ticker_factory = yf.Ticker
        self._ticker_factory = ticker_factory

    def get_daily_prices(self, symbol: str, *, period: str = "1y") -> list[HistoricalPrice]:
        normalized = symbol.strip().upper()
        if not normalized:
            return []
        frame = self._ticker_factory(normalized).history(
            period=period,
            interval="1d",
            auto_adjust=True,
            actions=False,
            raise_errors=True,
        )
        if frame is None or getattr(frame, "empty", False):
            return []

        observed_at = datetime.now(timezone.utc)
        prices: list[HistoricalPrice] = []
        for index, row in frame.iterrows():
            session_date = _session_date(index)
            try:
                close = float(row["Close"])
            except (KeyError, TypeError, ValueError):
                continue
            if not isfinite(close) or close <= 0:
                continue
            prices.append(HistoricalPrice(
                symbol=normalized,
                session_date=session_date,
                close=close,
                source=f"{self.name}:auto-adjusted-daily",
                observed_at=observed_at,
                adjusted=True,
            ))
        return prices


def _session_date(index: object) -> date:
    if isinstance(index, datetime):
        return index.date()
    if isinstance(index, date):
        return index
    if hasattr(index, "date"):
        value = index.date()
        if isinstance(value, date):
            return value
    return date.fromisoformat(str(index)[:10])
