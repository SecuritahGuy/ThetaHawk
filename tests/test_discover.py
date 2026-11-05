from datetime import datetime, timedelta, timezone

from thetahawk.discover import run_discovery
from thetahawk.domain import OptionRight, Quote, record_payload
from thetahawk.replay import audit_candidate
from thetahawk.settings import Settings
from thetahawk.sec_filings import SecFiling
from thetahawk.storage import ResearchStore


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


class FakeSecProvider:
    def __init__(self, filings=None, error=None):
        self.filings = list(filings or [])
        self.error = error
        self.calls = []

    def get_filings(self, symbols, *, start, end):
        self.calls.append((tuple(symbols), start, end))
        if self.error:
            raise self.error
        return self.filings


def _filing(symbol="AAPL"):
    now = datetime.now(timezone.utc)
    return SecFiling(
        symbol=symbol,
        cik="0000320193",
        accession_number="0000320193-26-000001",
        form_type="8-K",
        filing_date=now.date(),
        acceptance_datetime=now - timedelta(minutes=1),
        report_date=now.date(),
        items=("2.02",),
        primary_document="aapl-20261105.htm",
        filing_url="https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/aapl.htm",
        observed_at=now,
    )


class MemoryStore:
    records = []
    manifests = []

    def __init__(self, _path):
        self.records = []
        self.manifests = []
        MemoryStore.records = self.records
        MemoryStore.manifests = self.manifests

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

    def save_scan_run(self, manifest):
        self.manifests.append(record_payload(manifest))

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
    [manifest] = MemoryStore.manifests
    assert manifest["scanner"] == "options_discovery"
    assert manifest["policy_version"] == "integrated-discovery-policy-v1"
    assert discovery_rows[0]["candidate_id"] in manifest["candidate_ids"]
    assert len(manifest["inputs"]["option_quotes_by_symbol"]["AAPL"]) == 2


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


def test_integrated_discovery_recomputes_from_its_manifest(tmp_path):
    settings = Settings(database_path=tmp_path / "research.duckdb")
    result = run_discovery(
        symbols=["AAPL"],
        settings=settings,
        market_provider=FakeMarketProvider(),
        history_provider=FakeHistoryProvider(),
    )
    with ResearchStore(settings.database_path) as store:
        rows = store.recent_candidates(100)
        aggregate = next(row for row in rows if row["scanner"] == "options_discovery")

    audit = audit_candidate(settings.database_path, aggregate["candidate_id"])

    assert result.discoveries
    assert audit["exact_replay_possible"] is True
    assert audit["recomputation"]["matches_stored_candidate"] is True


def test_sec_filings_are_opt_in_context_and_not_ranking_support(tmp_path, monkeypatch):
    monkeypatch.setattr("thetahawk.discover.ResearchStore", MemoryStore)
    settings = Settings(database_path=tmp_path / "research.duckdb")
    provider = FakeSecProvider([_filing()])

    default_result = run_discovery(
        symbols=["AAPL"], settings=settings,
        market_provider=FakeMarketProvider(), history_provider=FakeHistoryProvider(),
        sec_provider=provider,
    )
    assert provider.calls == []
    assert not any(candidate.scanner == "event_catalyst" for candidate in default_result.screen_candidates)

    result = run_discovery(
        symbols=["AAPL"], settings=settings,
        market_provider=FakeMarketProvider(), history_provider=FakeHistoryProvider(),
        include_sec_filings=True, sec_provider=provider,
    )
    assert len(provider.calls) == 1
    sec_candidate = next(candidate for candidate in result.screen_candidates if candidate.scanner == "event_catalyst")
    assert all("event_catalyst" not in item.supporting_screens for item in result.discoveries)
    assert any(sec_candidate.candidate_id == candidate.candidate_id for candidate in result.discoveries[0].candidates)
    baseline_counts = {item.discovery_id: item.research_screen_count for item in default_result.discoveries}
    assert all(item.research_screen_count == baseline_counts[item.discovery_id] for item in result.discoveries)
    manifest = MemoryStore.manifests[-1]
    assert manifest["inputs"]["include_sec_filings"] is True
    assert manifest["inputs"]["sec_filings"][0]["accession_number"] == sec_candidate.metrics["accession_number"]


def test_sec_provider_failure_is_recorded_and_market_discovery_continues(tmp_path, monkeypatch):
    monkeypatch.setattr("thetahawk.discover.ResearchStore", MemoryStore)
    result = run_discovery(
        symbols=["AAPL"], settings=Settings(database_path=tmp_path / "research.duckdb"),
        market_provider=FakeMarketProvider(), history_provider=FakeHistoryProvider(),
        include_sec_filings=True, sec_provider=FakeSecProvider(error=ConnectionError("SEC unavailable")),
    )
    assert result.discoveries
    assert result.errors == ("SEC filings: ConnectionError: SEC unavailable",)
    manifest = MemoryStore.manifests[-1]
    assert manifest["inputs"]["include_sec_filings"] is True
    assert manifest["inputs"]["sec_filings"] == []


def test_integrated_discovery_with_sec_filings_replays_exactly(tmp_path):
    settings = Settings(database_path=tmp_path / "research.duckdb")
    result = run_discovery(
        symbols=["AAPL"], settings=settings,
        market_provider=FakeMarketProvider(), history_provider=FakeHistoryProvider(),
        include_sec_filings=True, sec_provider=FakeSecProvider([_filing()]),
    )
    with ResearchStore(settings.database_path) as store:
        rows = store.recent_candidates(100)
        aggregate = next(row for row in rows if row["scanner"] == "options_discovery")

    audit = audit_candidate(settings.database_path, aggregate["candidate_id"])
    assert result.discoveries
    assert audit["exact_replay_possible"] is True
    assert audit["recomputation"]["matches_stored_candidate"] is True
