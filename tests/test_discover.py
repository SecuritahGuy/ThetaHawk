from datetime import date, datetime, timedelta, timezone

from thetahawk.discover import run_discovery
from thetahawk.domain import OptionRight, Quote, record_payload
from thetahawk.settings import Settings


class FakeMarketProvider:
    def __init__(self):
        self.now = datetime.now(timezone.utc)
        self.expiry = self.now.date() + timedelta(days=33)

    def get_equity_quotes(self, symbols):
        return [Quote(
            symbol=symbol,
            observed_at=self.now,
            source_timestamp=self.now,
            bid=99.9,
            ask=100.1,
            source="fake:stocks",
        ) for symbol in symbols]

    def get_option_expirations(self, symbol, *, start=None, end=None):
        return [self.expiry]

    def get_option_quotes(self, symbol, expiration):
        return [
            self._option(symbol, expiration, OptionRight.CALL, "C", 1.0),
            self._option(symbol, expiration, OptionRight.PUT, "P", 1.0),
        ]

    def _option(self, symbol, expiration, right, suffix, price):
        occ = f"{symbol:<6}{expiration:%y%m%d}{suffix}{100000:08d}".replace(" ", "")
        return Quote(
            symbol=symbol,
            instrument_type="option",
            expiration=expiration,
            strike=100.0,
            right=right,
            observed_at=self.now,
            source_timestamp=self.now,
            bid=price - 0.01,
            ask=price + 0.01,
            volume=20,
            open_interest=100,
            implied_volatility=0.30,
            source="fake:options",
            metadata={"contract_symbol": occ, "open_interest_date": self.now.date().isoformat()},
        )


class FakeHistoryProvider:
    def get_daily_prices(self, symbol, *, period="1y"):
        return []


class MemoryStore:
    records = []

    def __init__(self, _path):
        self.records = []
        MemoryStore.records = self.records

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def option_quotes(self, _symbol, **_kwargs):
        return []

    def save_quotes(self, _quotes):
        return 0

    def save_historical_prices(self, _prices):
        return 0

    def save_candidates(self, candidates):
        self.records.extend(record_payload(candidate) for candidate in candidates)
        return len(candidates)

    def recent_candidates(self, limit=100):
        return self.records[:limit]


def test_integrated_discovery_persists_ranked_pair_and_screen_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr("thetahawk.discover.ResearchStore", MemoryStore)
    settings = Settings(database_path=tmp_path / "research.duckdb")
    result = run_discovery(
        symbols=["AAPL"],
        settings=settings,
        market_provider=FakeMarketProvider(),
        history_provider=FakeHistoryProvider(),
    )

    assert result.symbols == ("AAPL",)
    assert len(result.selected_pairs) == 1
    assert result.discoveries
    top = result.discoveries[0]
    assert top.symbol == "AAPL"
    assert len(top.contract_symbols) == 2
    assert "contract_selection" not in top.supporting_screens
    rows = MemoryStore.records
    discovery_rows = [row for row in rows if row["scanner"] == "options_discovery"]
    assert discovery_rows
    assert discovery_rows[0]["metrics"]["research_screen_count"] == 0
    assert any(row["scanner"] == "volatility_divergence" for row in rows)


def test_equity_quote_provider_failure_keeps_auditable_discovery_run(tmp_path, monkeypatch):
    monkeypatch.setattr("thetahawk.discover.ResearchStore", MemoryStore)

    class FailingMarketProvider(FakeMarketProvider):
        def get_equity_quotes(self, symbols):
            raise ConnectionError("temporary outage")

    result = run_discovery(
        symbols=["AAPL"],
        settings=Settings(database_path=tmp_path / "research.duckdb"),
        market_provider=FailingMarketProvider(),
        history_provider=FakeHistoryProvider(),
    )

    assert result.discoveries
    assert result.errors == ("equity quotes: ConnectionError: temporary outage",)
    equity_candidate = next(
        candidate for candidate in result.screen_candidates
        if candidate.scanner == "overextended_equity"
    )
    assert equity_candidate.status.value == "suppressed"
