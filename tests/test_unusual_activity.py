from __future__ import annotations

import unittest
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from thetahawk.domain import CandidateStatus, OptionRight, Quote
from thetahawk.unusual_activity import (
    UnusualOptionsActivityInput,
    UnusualOptionsActivityScanner,
)


EASTERN = ZoneInfo("America/New_York")
CONTRACT = "AAPL261218C00100000"
SOURCE = "alpaca:options:indicative"


class UnusualOptionsActivityScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.current_at = datetime(2026, 11, 5, 15, 30, tzinfo=EASTERN)
        self.as_of = self.current_at + timedelta(minutes=1)
        self.current = self.quote(
            self.current_at,
            volume=1_000,
            open_interest=2_500,
            contract=CONTRACT,
        )
        self.history = []
        day = self.current_at.date() - timedelta(days=1)
        while len(self.history) < 20:
            if day.weekday() < 5:
                observed_at = datetime.combine(day, time(15, 30), tzinfo=EASTERN)
                self.history.append(self.quote(
                    observed_at,
                    volume=100 + len(self.history) % 5,
                    open_interest=2_400,
                    contract=CONTRACT,
                ))
            day -= timedelta(days=1)

    def quote(
        self,
        observed_at: datetime,
        *,
        volume: int,
        open_interest: int,
        contract: str,
        source: str = SOURCE,
        source_timestamp: datetime | None = None,
    ) -> Quote:
        return Quote(
            symbol="AAPL",
            instrument_type="option",
            expiration=date(2026, 12, 18),
            strike=100.0,
            right=OptionRight.CALL,
            observed_at=observed_at,
            source_timestamp=source_timestamp or observed_at,
            bid=1.0,
            ask=1.1,
            volume=volume,
            open_interest=open_interest,
            source=source,
            metadata={"contract_symbol": contract},
        )

    def test_flags_volume_surge_against_same_contract_time_bucket_baseline(self) -> None:
        candidate = UnusualOptionsActivityScanner().scan(UnusualOptionsActivityInput(
            symbol="AAPL",
            as_of=self.as_of,
            current=self.current,
            history=self.history,
        ))

        self.assertEqual(candidate.status, CandidateStatus.RESEARCH)
        self.assertEqual(candidate.metrics["baseline_session_count"], 20)
        self.assertGreater(candidate.metrics["robust_z_score"], 3.5)
        self.assertIn("does not identify trade direction", candidate.risks[0])

    def test_suppresses_insufficient_comparable_sessions(self) -> None:
        candidate = UnusualOptionsActivityScanner().scan(UnusualOptionsActivityInput(
            symbol="AAPL",
            as_of=self.as_of,
            current=self.current,
            history=self.history[:5],
        ))

        self.assertEqual(candidate.status, CandidateStatus.SUPPRESSED)
        self.assertIn("comparable sessions", candidate.summary)

    def test_suppresses_stale_current_quote(self) -> None:
        stale = self.quote(
            self.current_at,
            volume=1_000,
            open_interest=2_500,
            contract=CONTRACT,
            source_timestamp=self.current_at - timedelta(minutes=20),
        )
        candidate = UnusualOptionsActivityScanner().scan(UnusualOptionsActivityInput(
            symbol="AAPL",
            as_of=self.as_of,
            current=stale,
            history=self.history,
        ))

        self.assertEqual(candidate.status, CandidateStatus.SUPPRESSED)
        self.assertIn("stale", candidate.summary)

    def test_suppresses_mismatched_contract_baseline(self) -> None:
        other_contract = [
            self.quote(
                row.observed_at,
                volume=row.volume or 0,
                open_interest=row.open_interest or 0,
                contract="AAPL261218P00100000",
            )
            for row in self.history
        ]
        candidate = UnusualOptionsActivityScanner().scan(UnusualOptionsActivityInput(
            symbol="AAPL",
            as_of=self.as_of,
            current=self.current,
            history=other_contract,
        ))

        self.assertEqual(candidate.status, CandidateStatus.SUPPRESSED)
        self.assertIn("comparable sessions", candidate.summary)

    def test_suppresses_mixed_provider_history(self) -> None:
        mixed = [*self.history[:-1], self.quote(
            self.history[-1].observed_at,
            volume=100,
            open_interest=2_400,
            contract=CONTRACT,
            source="yahoo-options",
        )]
        candidate = UnusualOptionsActivityScanner().scan(UnusualOptionsActivityInput(
            symbol="AAPL",
            as_of=self.as_of,
            current=self.current,
            history=mixed,
        ))

        self.assertEqual(candidate.status, CandidateStatus.SUPPRESSED)
        self.assertIn("mixes provider or feed", candidate.summary)


if __name__ == "__main__":
    unittest.main()
