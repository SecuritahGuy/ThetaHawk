from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from thetahawk.domain import CandidateStatus, HistoricalPrice, OptionRight, Quote, RealizedMove
from thetahawk.scanners import (
    EarningsImpliedMoveScanner,
    EarningsScanInput,
    OverextendedEquityInput,
    OverextendedEquityScanner,
)


class EarningsScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.as_of = datetime.now(timezone.utc)
        self.event_at = self.as_of + timedelta(days=7)
        self.expiration_at = self.as_of + timedelta(days=10)

    def quote(self, right: OptionRight) -> Quote:
        return Quote(
            symbol="DEMO",
            instrument_type="option",
            observed_at=self.as_of,
            source_timestamp=self.as_of,
            bid=1.95,
            ask=2.05,
            volume=250,
            open_interest=1200,
            expiration=self.expiration_at.date(),
            strike=100.0,
            right=right,
            source="fixture",
            metadata={"open_interest_date": self.as_of.date().isoformat()},
        )

    def underlying_quote(self) -> Quote:
        return Quote(
            symbol="DEMO",
            instrument_type="equity",
            observed_at=self.as_of,
            source_timestamp=self.as_of,
            bid=99.9,
            ask=100.1,
            last=100,
            volume=1_000_000,
            source="fixture",
        )

    def inputs(self, *, oi_date: str | None = None) -> EarningsScanInput:
        history = tuple(
            RealizedMove(
                symbol="DEMO",
                event_date=(self.as_of - timedelta(days=index + 1)).date(),
                reference_price=100,
                post_event_price=108,
                realized_move=0.08,
                source="fixture",
                observed_at=self.as_of,
            )
            for index in range(5)
        )
        call = self.quote(OptionRight.CALL)
        put = self.quote(OptionRight.PUT)
        if oi_date is not None:
            call = replace(call, metadata={"open_interest_date": oi_date})
            put = replace(put, metadata={"open_interest_date": oi_date})
        return EarningsScanInput(
            symbol="DEMO",
            as_of=self.as_of,
            underlying_quote=self.underlying_quote(),
            event_observed_at=self.as_of,
            event_source="fixture",
            event_id="DEMO:earnings",
            event_at=self.event_at,
            expiration_at=self.expiration_at,
            call=call,
            put=put,
            historical_realized_moves=history,
        )

    def test_returns_research_candidate_when_history_exceeds_implied_move(self) -> None:
        candidate = EarningsImpliedMoveScanner().scan(self.inputs())
        self.assertEqual(candidate.status, CandidateStatus.RESEARCH)
        self.assertGreater(candidate.score, 0)
        self.assertEqual(candidate.metrics["history_sample_count"], 5)

    def test_suppresses_stale_open_interest(self) -> None:
        candidate = EarningsImpliedMoveScanner().scan(
            self.inputs(oi_date=(self.as_of - timedelta(days=7)).date().isoformat())
        )
        self.assertEqual(candidate.status, CandidateStatus.SUPPRESSED)
        self.assertIn("open interest date", candidate.summary)

    def test_checks_provider_quote_timestamp_not_only_ingestion_timestamp(self) -> None:
        item = self.inputs()
        stale_call = replace(
            item.call,
            source_timestamp=self.as_of - timedelta(minutes=30),
            observed_at=self.as_of,
        )
        candidate = EarningsImpliedMoveScanner().scan(replace(item, call=stale_call))
        self.assertEqual(candidate.status, CandidateStatus.SUPPRESSED)
        self.assertIn("quotes are future-dated or stale", candidate.summary)


class OverextendedEquityScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.as_of = datetime.now(timezone.utc)
        self.quote = Quote(
            symbol="DEMO",
            observed_at=self.as_of,
            source_timestamp=self.as_of,
            bid=119.9,
            ask=120.1,
            source="fixture",
        )
        self.history = tuple(
            HistoricalPrice(
                symbol="DEMO",
                timestamp=self.as_of - timedelta(days=65 - index + 1),
                close=100 + index * 0.03 + ((index * 7) % 11) * 0.07,
                source="fixture",
                adjusted=True,
            )
            for index in range(65)
        )

    def test_flags_large_extreme_move_against_robust_baseline(self) -> None:
        candidate = OverextendedEquityScanner().scan(OverextendedEquityInput(
            symbol="DEMO",
            as_of=self.as_of,
            quote=self.quote,
            historical_prices=self.history,
        ))

        self.assertEqual(candidate.status, CandidateStatus.RESEARCH)
        self.assertGreater(candidate.metrics["robust_z_score"], 3.5)
        self.assertIn("does not imply", candidate.risks[0])

    def test_suppresses_insufficient_price_history(self) -> None:
        candidate = OverextendedEquityScanner().scan(OverextendedEquityInput(
            symbol="DEMO",
            as_of=self.as_of,
            quote=self.quote,
            historical_prices=self.history[:40],
        ))

        self.assertEqual(candidate.status, CandidateStatus.SUPPRESSED)
        self.assertIn("historical closes", candidate.summary)


if __name__ == "__main__":
    unittest.main()
