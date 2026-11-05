from datetime import date, datetime, timezone

import pytest

from thetahawk.domain import CandidateStatus
from thetahawk.replay import audit_candidate
from thetahawk.sec_filings import (
    SEC_SUBMISSIONS_URL,
    SEC_TICKER_URL,
    SecCompanyFilingsProvider,
    SecFiling,
    filing_to_candidate,
    run_sec_filing_scan,
)
from thetahawk.settings import Settings
from thetahawk.storage import ResearchStore


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.headers = {}
        self.urls = []

    def get(self, url, *, timeout):
        self.urls.append((url, timeout))
        return FakeResponse(self.payloads.pop(0))


def _submissions():
    return {
        "filings": {
            "recent": {
                "accessionNumber": ["0000320193-26-000101", "0000320193-26-000100"],
                "filingDate": ["2026-11-05", "2026-11-04"],
                "form": ["8-K", "10-Q"],
                "reportDate": ["2026-11-05", "2026-09-30"],
                "acceptanceDateTime": [
                    "2026-11-05T12:30:00.000Z",
                    "2026-11-04T20:00:00.000Z",
                ],
                "items": ["2.02,9.01", ""],
                "primaryDocument": ["aapl-20261105.htm", "aapl-20261001.htm"],
            }
        }
    }


def test_provider_maps_ticker_fetches_recent_filings_and_preserves_sec_provenance():
    session = FakeSession([
        {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}},
        _submissions(),
    ])
    provider = SecCompanyFilingsProvider(
        user_agent="Thetahawk Research contact@thetahawk.dev",
        session=session,
        minimum_request_interval_seconds=0,
    )

    filings = provider.get_filings(
        ["aapl"], start=date(2026, 11, 4), end=date(2026, 11, 5)
    )

    assert [row.form_type for row in filings] == ["8-K", "10-Q"]
    filing = filings[0]
    assert filing.symbol == "AAPL"
    assert filing.cik == "0000320193"
    assert filing.accession_number == "0000320193-26-000101"
    assert filing.acceptance_datetime == datetime(2026, 11, 5, 12, 30, tzinfo=timezone.utc)
    assert filing.items == ("2.02", "9.01")
    assert filing.filing_url == (
        "https://www.sec.gov/Archives/edgar/data/320193/"
        "000032019326000101/aapl-20261105.htm"
    )
    assert session.urls == [
        (SEC_TICKER_URL, 20.0),
        (SEC_SUBMISSIONS_URL.format(cik="0000320193"), 20.0),
    ]
    assert session.headers["User-Agent"] == "Thetahawk Research contact@thetahawk.dev"


def test_provider_requires_declared_user_agent_contact():
    with pytest.raises(ValueError, match="contact email"):
        SecCompanyFilingsProvider(user_agent="Thetahawk")


def test_filing_mapper_is_neutral_and_suppresses_future_acceptance():
    session = FakeSession([
        {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}},
        _submissions(),
    ])
    provider = SecCompanyFilingsProvider(
        user_agent="Thetahawk Research contact@thetahawk.dev",
        session=session,
        minimum_request_interval_seconds=0,
    )
    filing = provider.get_filings(
        ["AAPL"], start=date(2026, 11, 5), end=date(2026, 11, 5), forms=("8-K",)
    )[0]

    candidate = filing_to_candidate(
        filing,
        as_of=datetime(2026, 11, 5, 12, 29, tzinfo=timezone.utc),
    )
    assert candidate.status == CandidateStatus.SUPPRESSED
    assert candidate.scanner == "event_catalyst"
    assert candidate.score is None
    assert candidate.metrics["cik"] == "0000320193"
    assert candidate.metrics["form_type"] == "8-K"
    assert candidate.metrics["filing_url"] == filing.filing_url
    assert "reaction" not in candidate.summary.lower()

    candidate = filing_to_candidate(
        filing,
        as_of=datetime(2026, 11, 5, 12, 31, tzinfo=timezone.utc),
    )
    assert candidate.status == CandidateStatus.RESEARCH
    assert candidate.source_timestamps == (filing.acceptance_datetime, filing.observed_at)


def test_provider_rejects_inconsistent_sec_column_lengths():
    malformed = _submissions()
    malformed["filings"]["recent"]["form"].pop()
    provider = SecCompanyFilingsProvider(
        user_agent="Thetahawk Research contact@thetahawk.dev",
        session=FakeSession([
            {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}},
            malformed,
        ]),
        minimum_request_interval_seconds=0,
    )
    with pytest.raises(ValueError, match="inconsistent lengths"):
        provider.get_filings(
            ["AAPL"], start=date(2026, 11, 4), end=date(2026, 11, 5)
        )


def test_scan_persists_and_replays_sec_filing_candidate(tmp_path):
    scan_time = datetime(2026, 11, 5, 20, 0, tzinfo=timezone.utc)
    filing = SecFiling(
        symbol="AAPL",
        cik="0000320193",
        accession_number="0000320193-26-000101",
        form_type="8-K",
        filing_date=date(2026, 11, 5),
        acceptance_datetime=datetime(2026, 11, 5, 19, 30, tzinfo=timezone.utc),
        report_date=None,
        items=("2.02", "9.01"),
        primary_document="aapl-20261105.htm",
        filing_url="https://www.sec.gov/Archives/edgar/data/320193/000032019326000101/aapl-20261105.htm",
        observed_at=scan_time,
    )

    class FixtureProvider:
        def get_filings(self, symbols, *, start, end):
            assert tuple(symbols) == ("AAPL",)
            return [filing]

    settings = Settings(database_path=tmp_path / "research.duckdb")
    result = run_sec_filing_scan(
        symbols=("AAPL",),
        settings=settings,
        provider=FixtureProvider(),
        as_of=scan_time,
    )
    with ResearchStore(settings.database_path) as store:
        [candidate] = store.recent_candidates(1)

    audit = audit_candidate(settings.database_path, candidate["candidate_id"])

    assert result.filings == result.candidates == 1
    assert audit["exact_replay_possible"] is True
    assert audit["recomputation"]["matches_stored_candidate"] is True
