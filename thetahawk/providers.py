"""Provider interfaces and a deterministic demo data provider."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Protocol, Sequence

import requests

from .domain import EarningsEvent, OptionRight, Quote
from .settings import Settings


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


class AlpacaMarketDataProvider:
    """Read-only Alpaca quote and option-chain adapter.

    Uses the market-data API for quotes and the broker API only to read option
    contract metadata such as open interest. No trading endpoints are called.
    """

    name = "alpaca"
    data_base_url = "https://data.alpaca.markets"

    def __init__(self, settings: Settings | None = None, *, timeout_seconds: float = 20.0) -> None:
        self.settings = settings or Settings.from_env()
        if not self.settings.alpaca_key_id or not self.settings.alpaca_secret_key:
            raise ValueError("Set APCA_API_KEY_ID and APCA_API_SECRET_KEY to use Alpaca data")
        if self.settings.alpaca_stock_feed not in {
            "sip", "iex", "delayed_sip", "boats", "overnight", "otc"
        }:
            raise ValueError("Unsupported Alpaca stock feed")
        if self.settings.alpaca_options_feed not in {"opra", "indicative"}:
            raise ValueError("Unsupported Alpaca options feed")
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.session.headers.update({
            "APCA-API-KEY-ID": self.settings.alpaca_key_id,
            "APCA-API-SECRET-KEY": self.settings.alpaca_secret_key,
            "Accept": "application/json",
        })

    def _get_json(self, base_url: str, path: str, params: dict[str, object]) -> dict:
        response = self.session.get(
            f"{base_url.rstrip('/')}/{path.lstrip('/')}",
            params=params,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("Alpaca returned a non-object JSON response")
        return body

    def get_equity_quotes(self, symbols: Sequence[str]) -> list[Quote]:
        normalized = tuple(dict.fromkeys(s.strip().upper() for s in symbols if s.strip()))
        if not normalized:
            return []
        body = self._get_json(
            self.data_base_url,
            "/v2/stocks/quotes/latest",
            {"symbols": ",".join(normalized), "feed": self.settings.alpaca_stock_feed},
        )
        received_at = datetime.now(timezone.utc)
        result = []
        for symbol, raw in body.get("quotes", {}).items():
            source_time = _parse_datetime(raw.get("t"))
            result.append(Quote(
                symbol=symbol.upper(),
                instrument_type="equity",
                observed_at=received_at,
                source_timestamp=source_time,
                bid=_optional_float(raw.get("bp")),
                ask=_optional_float(raw.get("ap")),
                source=f"alpaca:stocks:{self.settings.alpaca_stock_feed}",
                metadata={"feed": self.settings.alpaca_stock_feed},
            ))
        return result

    def get_option_quotes(self, symbol: str, expiration: date | None = None) -> list[Quote]:
        underlying = symbol.strip().upper()
        if not underlying:
            return []
        contracts = self._get_option_contracts(underlying, expiration)
        contract_by_symbol = {row.get("symbol"): row for row in contracts if row.get("symbol")}
        page_token: str | None = None
        result: list[Quote] = []
        received_at = datetime.now(timezone.utc)

        while True:
            params: dict[str, object] = {
                "feed": self.settings.alpaca_options_feed,
                "limit": 1000,
            }
            if expiration is not None:
                params["expiration_date"] = expiration.isoformat()
            if page_token:
                params["page_token"] = page_token
            body = self._get_json(
                self.data_base_url,
                f"/v1beta1/options/snapshots/{underlying}",
                params,
            )
            snapshots = body.get("snapshots", {})
            for contract_symbol, snapshot in snapshots.items():
                contract = contract_by_symbol.get(contract_symbol, {})
                parsed = _parse_occ_symbol(contract_symbol)
                if not parsed:
                    continue
                expiry, right, strike = parsed
                if expiration is not None and expiry != expiration:
                    continue
                latest_quote = snapshot.get("latestQuote") or {}
                latest_trade = snapshot.get("latestTrade") or {}
                daily_bar = snapshot.get("dailyBar") or {}
                contract_oi_date = contract.get("open_interest_date")
                open_interest = _optional_int(contract.get("open_interest"))
                source_time = _parse_datetime(latest_quote.get("t")) or _parse_datetime(
                    latest_trade.get("t")
                )
                result.append(Quote(
                    symbol=underlying,
                    instrument_type="option",
                    expiration=expiry,
                    strike=strike,
                    right=right,
                    observed_at=received_at,
                    source_timestamp=source_time,
                    bid=_optional_float(latest_quote.get("bp")),
                    ask=_optional_float(latest_quote.get("ap")),
                    last=_optional_float(latest_trade.get("p")),
                    volume=_optional_int(daily_bar.get("v")),
                    open_interest=open_interest,
                    implied_volatility=_optional_float(snapshot.get("impliedVolatility")),
                    delta=_optional_float((snapshot.get("greeks") or {}).get("delta")),
                    source=f"alpaca:options:{self.settings.alpaca_options_feed}",
                    metadata={
                        "feed": self.settings.alpaca_options_feed,
                        "contract_symbol": contract_symbol,
                        "open_interest_date": contract_oi_date,
                        "open_interest_source": "alpaca-contract-metadata",
                        "daily_bar_timestamp": daily_bar.get("t"),
                    },
                ))
            page_token = body.get("next_page_token")
            if not page_token:
                break
        return result

    def _get_option_contracts(self, symbol: str, expiration: date | None) -> list[dict]:
        params: dict[str, object] = {
            "underlying_symbols": symbol,
            "status": "active",
            "limit": 10000,
        }
        if expiration is not None:
            params["expiration_date"] = expiration.isoformat()
        page_token: str | None = None
        rows = []
        broker_base_url = self.settings.alpaca_broker_base_url.rstrip("/")
        if broker_base_url.endswith("/v2"):
            broker_path = "/options/contracts"
        else:
            broker_path = "/v2/options/contracts"
        while True:
            if page_token:
                params["page_token"] = page_token
            body = self._get_json(
                broker_base_url,
                broker_path,
                params,
            )
            rows.extend(body.get("option_contracts", []))
            page_token = body.get("next_page_token")
            if not page_token:
                return rows


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _optional_float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_int(value: object) -> int | None:
    try:
        return int(float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _parse_occ_symbol(symbol: str) -> tuple[date, OptionRight, float] | None:
    match = re.search(r"(\d{6})([CP])(\d{8})$", symbol)
    if not match:
        return None
    expiry_code, right_code, strike_code = match.groups()
    try:
        expiry = datetime.strptime(expiry_code, "%y%m%d").date()
    except ValueError:
        return None
    right = OptionRight.CALL if right_code == "C" else OptionRight.PUT
    return expiry, right, int(strike_code) / 1000
