"""Read-only SEC company-filing metadata and neutral catalyst candidates.

This module records filing metadata and source links only. It does not fetch or
interpret filing text, infer price reaction, or assign bullish/bearish meaning.
"""

from __future__ import annotations

import argparse
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import requests

from .domain import Candidate, CandidateStatus
from .run_manifest import create_scan_run_manifest
from .settings import Settings
from .storage import ResearchStore


SEC_TICKER_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data"
SEC_SOURCE = "sec-edgar:company-submissions"
DEFAULT_FORMS = ("8-K", "10-Q", "10-K", "6-K", "20-F")
_EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class SecFiling:
    """A company-submissions record with source and acceptance provenance."""

    symbol: str
    cik: str
    accession_number: str
    form_type: str
    filing_date: date
    acceptance_datetime: datetime | None
    report_date: date | None
    items: tuple[str, ...]
    primary_document: str | None
    filing_url: str
    observed_at: datetime
    source: str = SEC_SOURCE


class SecCompanyFilingsProvider:
    """Fetch recent SEC EDGAR submissions for known tickers.

    The SEC requires a declared User-Agent with an administrative contact.
    A small delay is applied between requests to stay below the published
    10-requests-per-second maximum even when a caller supplies many symbols.
    """

    name = SEC_SOURCE

    def __init__(
        self,
        *,
        user_agent: str,
        session: Any | None = None,
        timeout_seconds: float = 20.0,
        minimum_request_interval_seconds: float = 0.12,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not user_agent.strip() or not _EMAIL.search(user_agent):
            raise ValueError("SEC User-Agent must identify the application and include a contact email")
        if timeout_seconds <= 0 or minimum_request_interval_seconds < 0:
            raise ValueError("timeout must be positive and request interval cannot be negative")
        self.user_agent = user_agent.strip()
        self.timeout_seconds = timeout_seconds
        self.minimum_request_interval_seconds = minimum_request_interval_seconds
        self.sleep = sleep
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": self.user_agent,
            "Accept-Encoding": "gzip, deflate",
            "Accept": "application/json",
        })
        self._last_request_monotonic: float | None = None
        self._ticker_to_cik: dict[str, str] | None = None

    def get_filings(
        self,
        symbols: Iterable[str],
        *,
        start: date,
        end: date,
        forms: Iterable[str] = DEFAULT_FORMS,
        limit_per_symbol: int = 100,
    ) -> list[SecFiling]:
        """Return recent filings in an inclusive filing-date range.

        The SEC recent-submissions object covers at least one year or 1,000
        filings, whichever is greater. This adapter intentionally does not
        fetch older archive index pages; older-range requests simply return
        the matching rows available in the recent-submissions object.
        """
        if end < start:
            raise ValueError("end date must be on or after start date")
        if limit_per_symbol < 1:
            raise ValueError("limit_per_symbol must be positive")
        normalized_forms = {str(form).strip().upper() for form in forms if str(form).strip()}
        wanted = tuple(dict.fromkeys(
            str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()
        ))
        ticker_to_cik = self._load_ticker_map()
        result: list[SecFiling] = []
        for symbol in wanted:
            cik = ticker_to_cik.get(symbol)
            if cik is None:
                continue
            body = self._get_json(SEC_SUBMISSIONS_URL.format(cik=cik))
            recent = ((body.get("filings") or {}).get("recent") or {})
            result.extend(_parse_recent_filings(
                symbol=symbol,
                cik=cik,
                recent=recent,
                start=start,
                end=end,
                forms=normalized_forms,
                limit=limit_per_symbol,
                observed_at=datetime.now(timezone.utc),
            ))
        return sorted(result, key=lambda filing: (
            filing.filing_date,
            filing.acceptance_datetime or datetime.min.replace(tzinfo=timezone.utc),
            filing.symbol,
            filing.accession_number,
        ), reverse=True)

    def _load_ticker_map(self) -> dict[str, str]:
        if self._ticker_to_cik is not None:
            return self._ticker_to_cik
        payload = self._get_json(SEC_TICKER_URL)
        rows = payload.values() if isinstance(payload, dict) else payload
        lookup: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("ticker", "")).strip().upper()
            try:
                cik = f"{int(row['cik_str']):010d}"
            except (KeyError, TypeError, ValueError):
                continue
            if symbol:
                lookup[symbol] = cik
        self._ticker_to_cik = lookup
        return lookup

    def _get_json(self, url: str) -> dict[str, Any]:
        if self._last_request_monotonic is not None:
            elapsed = time.monotonic() - self._last_request_monotonic
            delay = self.minimum_request_interval_seconds - elapsed
            if delay > 0:
                self.sleep(delay)
        self._last_request_monotonic = time.monotonic()
        response = self.session.get(url, timeout=self.timeout_seconds)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("SEC returned a non-object JSON response")
        return payload


def filing_to_candidate(
    filing: SecFiling,
    *,
    as_of: datetime | None = None,
) -> Candidate:
    """Create a neutral, auditable catalyst record from a filing header."""
    observed_at = as_of or datetime.now(timezone.utc)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    candidate_id = f"{filing.symbol}:event_catalyst:sec:{filing.accession_number}"
    future_accepted = (
        filing.acceptance_datetime is not None
        and filing.acceptance_datetime > observed_at
    )
    status = CandidateStatus.SUPPRESSED if future_accepted else CandidateStatus.RESEARCH
    summary = (
        "Suppressed: SEC acceptance timestamp is after the scan time."
        if future_accepted
        else f"SEC {filing.form_type} filing dated {filing.filing_date.isoformat()}; review source document."
    )
    source_times = tuple(dict.fromkeys(
        value for value in (filing.acceptance_datetime, filing.observed_at) if value is not None
    ))
    return Candidate(
        candidate_id=candidate_id,
        symbol=filing.symbol.upper(),
        scanner="event_catalyst",
        score=None,
        created_at=observed_at,
        status=status,
        summary=summary,
        rationale=(
            f"SEC recorded a {filing.form_type} filing for CIK {filing.cik}.",
            "The filing header is surfaced for human review; no price reaction or directional meaning is inferred.",
        ),
        risks=(
            "Filing metadata alone does not establish materiality, market reaction, or a trade thesis.",
            "Review the linked filing and verify its timestamp and contents before drawing conclusions.",
        ),
        input_ids=(filing.accession_number, filing.filing_url),
        source_timestamps=source_times,
        metrics={
            "cik": filing.cik,
            "form_type": filing.form_type,
            "accession_number": filing.accession_number,
            "filing_date": filing.filing_date.isoformat(),
            "acceptance_datetime": filing.acceptance_datetime.isoformat()
            if filing.acceptance_datetime else None,
            "report_date": filing.report_date.isoformat() if filing.report_date else None,
            "items": list(filing.items),
            "filing_url": filing.filing_url,
            "filing_source": filing.source,
        },
    )


def filings_to_candidates(
    filings: Iterable[SecFiling],
    *,
    as_of: datetime | None = None,
) -> list[Candidate]:
    """Map filings to deterministic candidate records, newest first."""
    scan_time = as_of or datetime.now(timezone.utc)
    pairs = [(filing, filing_to_candidate(filing, as_of=scan_time)) for filing in filings]
    return [candidate for _filing, candidate in sorted(
        pairs,
        key=lambda row: (
            row[0].filing_date,
            row[0].acceptance_datetime or datetime.min.replace(tzinfo=timezone.utc),
            row[0].symbol,
            row[0].accession_number,
        ),
        reverse=True,
    )]


@dataclass(frozen=True, slots=True)
class SecFilingScanResult:
    symbols: tuple[str, ...]
    filings: int
    candidates: int
    research_candidates: int
    as_of: datetime
    database_path: Path


def run_sec_filing_scan(
    *,
    symbols: Iterable[str] | None = None,
    days_back: int = 7,
    settings: Settings | None = None,
    provider: SecCompanyFilingsProvider | None = None,
    as_of: datetime | None = None,
) -> SecFilingScanResult:
    """Fetch recent SEC filing headers and persist neutral catalyst records."""
    if days_back < 0:
        raise ValueError("days_back cannot be negative")
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
    provider = provider or SecCompanyFilingsProvider(
        user_agent=settings.sec_user_agent or ""
    )
    start = scan_time.date() - timedelta(days=days_back)
    filings = provider.get_filings(requested, start=start, end=scan_time.date())
    candidates = filings_to_candidates(filings, as_of=scan_time)
    manifest = create_scan_run_manifest(
        scanner="event_catalyst",
        scanner_version="1",
        policy_version="sec-filing-catalyst-policy-v1",
        as_of=scan_time,
        candidate_ids=tuple(candidate.candidate_id for candidate in candidates),
        inputs={
            "symbols": requested,
            "start_date": start,
            "end_date": scan_time.date(),
            "days_back": days_back,
            "forms": DEFAULT_FORMS,
            "filings": filings,
        },
    )
    with ResearchStore(settings.database_path) as store:
        store.save_candidates(candidates)
        store.save_scan_run(manifest)
    return SecFilingScanResult(
        symbols=requested,
        filings=len(filings),
        candidates=len(candidates),
        research_candidates=sum(
            candidate.status == CandidateStatus.RESEARCH for candidate in candidates
        ),
        as_of=scan_time,
        database_path=settings.database_path,
    )


def _parse_recent_filings(
    *,
    symbol: str,
    cik: str,
    recent: dict[str, Any],
    start: date,
    end: date,
    forms: set[str],
    limit: int,
    observed_at: datetime,
) -> list[SecFiling]:
    required_columns = ("accessionNumber", "filingDate", "form")
    optional_columns = ("reportDate", "acceptanceDateTime", "items", "primaryDocument")
    arrays: dict[str, list[Any]] = {}
    for key in required_columns:
        value = recent.get(key)
        if not isinstance(value, list):
            raise ValueError(f"SEC recent filings object is missing array column {key}")
        arrays[key] = value
    count = len(arrays["accessionNumber"])
    if any(len(arrays[key]) != count for key in required_columns):
        raise ValueError("SEC recent filings columns have inconsistent lengths")
    for key in optional_columns:
        value = recent.get(key)
        if value is None:
            arrays[key] = [None] * count
        elif not isinstance(value, list) or len(value) != count:
            raise ValueError(f"SEC recent filings column {key} has inconsistent length")
        else:
            arrays[key] = value
    results = []
    for index in range(count):
        try:
            form = str(arrays["form"][index]).strip().upper()
            filing_date = date.fromisoformat(str(arrays["filingDate"][index]))
            accession = str(arrays["accessionNumber"][index]).strip()
        except (IndexError, TypeError, ValueError):
            continue
        if not accession or not start <= filing_date <= end or (forms and form not in forms):
            continue
        acceptance = _parse_acceptance(arrays["acceptanceDateTime"][index])
        report_date_raw = arrays["reportDate"][index]
        try:
            report_date = date.fromisoformat(str(report_date_raw)) if report_date_raw else None
        except ValueError:
            report_date = None
        items = tuple(
            item.strip() for item in str(arrays["items"][index] or "").split(",") if item.strip()
        )
        document = str(arrays["primaryDocument"][index] or "").strip() or None
        accn_compact = accession.replace("-", "")
        cik_path = str(int(cik))
        base = f"{SEC_ARCHIVES_URL}/{cik_path}/{accn_compact}"
        filing_url = f"{base}/{document}" if document else f"{base}/"
        results.append(SecFiling(
            symbol=symbol,
            cik=cik,
            accession_number=accession,
            form_type=form,
            filing_date=filing_date,
            acceptance_datetime=acceptance,
            report_date=report_date,
            items=items,
            primary_document=document,
            filing_url=filing_url,
            observed_at=observed_at,
        ))
        if len(results) >= limit:
            break
    return results


def _parse_acceptance(value: Any) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch recent SEC filing headers for the configured symbol universe"
    )
    parser.add_argument("--symbols", help="Comma-separated symbols; defaults to THETAHAWK_SYMBOLS")
    parser.add_argument("--days-back", type=int, default=7)
    args = parser.parse_args()
    settings = Settings.from_env()
    result = run_sec_filing_scan(
        symbols=tuple(part.strip().upper() for part in args.symbols.split(",") if part.strip())
        if args.symbols else None,
        days_back=args.days_back,
        settings=settings,
    )
    print(
        f"Scanned {len(result.symbols)} symbols; found {result.filings} recent SEC filings "
        f"and stored {result.candidates} neutral catalyst candidates in {result.database_path}."
    )
    print("Filing headers are for human review; no market reaction or direction is inferred.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
