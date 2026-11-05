from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from thetahawk.domain import (
    Candidate,
    EarningsEvent,
    HistoricalPrice,
    OptionRight,
    Quote,
    RealizedMove,
)
from thetahawk.replay import audit_candidate
from thetahawk.run_manifest import create_scan_run_manifest
from thetahawk.storage import ResearchStore


class ReplayAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.as_of = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)
        self.event_date = date(2026, 12, 1)
        self.call_id = "AAPL261204C00250000"
        self.put_id = "AAPL261204P00250000"
        self.event_id = "AAPL:2026-12-01:fixture-calendar"
        self.candidate_id = f"AAPL:earnings_implied_move:{self.event_date.isoformat()}"
        self.candidate = Candidate(
            candidate_id=self.candidate_id,
            symbol="AAPL",
            scanner="earnings_implied_move",
            score=8.5,
            created_at=self.as_of,
            summary="Stored scan summary.",
            input_ids=(self.event_id, self.call_id, self.put_id),
            source_timestamps=(
                self.as_of - timedelta(minutes=5),
                self.as_of - timedelta(minutes=2),
                self.as_of - timedelta(minutes=1),
                self.as_of,
            ),
            metrics={"history_sample_count": 4},
        )

    def test_audits_stored_quotes_event_and_historical_move_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "research.duckdb"
            with ResearchStore(database) as store:
                store.save_candidates([self.candidate])
                store.save_earnings_events([EarningsEvent(
                    symbol="AAPL",
                    event_date=self.event_date,
                    timing="after_close",
                    observed_at=self.as_of - timedelta(minutes=5),
                    source="fixture-calendar",
                    source_timestamp=self.as_of - timedelta(minutes=5),
                    metadata={"event_id": self.event_id},
                )])
                store.save_quotes([
                    Quote(
                        symbol="AAPL",
                        observed_at=self.as_of - timedelta(minutes=2),
                        source_timestamp=self.as_of - timedelta(minutes=2),
                        bid=249.9,
                        ask=250.1,
                        source="fixture:stocks",
                    ),
                    Quote(
                        symbol="AAPL",
                        instrument_type="option",
                        expiration=date(2026, 12, 4),
                        strike=250,
                        right=OptionRight.CALL,
                        observed_at=self.as_of - timedelta(minutes=1),
                        source_timestamp=self.as_of - timedelta(minutes=1),
                        bid=4.9,
                        ask=5.1,
                        source="fixture:options",
                        metadata={"contract_symbol": self.call_id},
                    ),
                    Quote(
                        symbol="AAPL",
                        instrument_type="option",
                        expiration=date(2026, 12, 4),
                        strike=250,
                        right=OptionRight.PUT,
                        observed_at=self.as_of,
                        source_timestamp=self.as_of,
                        bid=4.8,
                        ask=5.0,
                        source="fixture:options",
                        metadata={"contract_symbol": self.put_id},
                    ),
                    Quote(
                        symbol="AAPL",
                        observed_at=self.as_of + timedelta(minutes=1),
                        source_timestamp=self.as_of + timedelta(minutes=1),
                        bid=251,
                        ask=252,
                        source="future-fixture",
                    ),
                ])
                store.save_realized_moves([
                    RealizedMove(
                        symbol="AAPL",
                        event_date=date(2024, 1, month),
                        reference_price=100,
                        post_event_price=105,
                        realized_move=0.05,
                        source="fixture-history",
                        observed_at=self.as_of - timedelta(days=10),
                    )
                    for month in range(1, 5)
                ])
                store.save_historical_prices([HistoricalPrice(
                    symbol="AAPL",
                    session_date=date(2026, 11, 4),
                    close=250,
                    source="fixture-history",
                    observed_at=self.as_of - timedelta(days=1),
                    adjusted=True,
                )])

            audit = audit_candidate(database, self.candidate_id)

        self.assertEqual(audit["audit_type"], "stored_input_audit")
        self.assertFalse(audit["exact_replay_possible"])
        self.assertEqual(len(audit["matched_inputs"]["quotes"]), 3)
        self.assertEqual(len(audit["matched_inputs"]["earnings_events"]), 1)
        self.assertEqual(audit["matched_inputs"]["realized_move_count"], 4)
        self.assertEqual(audit["unmatched"]["option_contract_ids"], [])
        self.assertEqual(audit["unmatched"]["source_timestamps"], [])
        self.assertIn("no scan-run manifest", " ".join(audit["limitations"]))

    def test_missing_candidate_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "research.duckdb"
            with ResearchStore(database):
                pass
            with self.assertRaisesRegex(KeyError, "Candidate not found"):
                audit_candidate(database, "missing")

    def test_audit_includes_versioned_manifest_without_claiming_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "research.duckdb"
            manifest = create_scan_run_manifest(
                scanner="earnings_implied_move",
                scanner_version="1",
                policy_version="earnings-screen-policy-v1",
                as_of=self.as_of,
                candidate_ids=(self.candidate_id,),
                inputs={"realized_moves": [
                    RealizedMove(
                        symbol="AAPL",
                        event_date=date(2026, 1, 1),
                        reference_price=100,
                        post_event_price=105,
                        realized_move=0.05,
                        source="fixture",
                        observed_at=self.as_of,
                    ),
                ]},
                code_version="git:abc123:dirty=false",
                run_id="run-fixture",
            )
            with ResearchStore(database) as store:
                store.save_candidates([self.candidate])
                store.save_scan_run(manifest)

            audit = audit_candidate(database, self.candidate_id)

        self.assertFalse(audit["exact_replay_possible"])
        self.assertEqual(audit["scan_run_manifest"]["run_id"], "run-fixture")
        self.assertEqual(audit["scan_run_manifest"]["code_version"], "git:abc123:dirty=false")
        self.assertEqual(
            audit["scan_run_manifest"]["inputs"]["realized_moves"][0]["realized_move"],
            0.05,
        )
        self.assertIn("no registered deterministic replay adapter", " ".join(audit["limitations"]))

    def test_audits_historical_closes_for_overextended_screen(self) -> None:
        candidate_id = "AAPL:overextended_equity:2026-11-05"
        session_dates = (date(2026, 11, 3), date(2026, 11, 4))
        fetch_time = self.as_of - timedelta(minutes=1)
        candidate = Candidate(
            candidate_id=candidate_id,
            symbol="AAPL",
            scanner="overextended_equity",
            score=0.0,
            created_at=self.as_of,
            input_ids=tuple(f"AAPL:{session_day.isoformat()}" for session_day in session_dates),
            source_timestamps=(self.as_of - timedelta(seconds=15), fetch_time),
        )
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "research.duckdb"
            with ResearchStore(database) as store:
                store.save_candidates([candidate])
                store.save_quotes([Quote(
                    symbol="AAPL",
                    observed_at=self.as_of - timedelta(seconds=15),
                    source_timestamp=self.as_of - timedelta(seconds=15),
                    bid=250,
                    ask=250.1,
                    source="fixture:quote",
                )])
                store.save_historical_prices([
                    HistoricalPrice(
                        symbol="AAPL",
                        session_date=session_day,
                        close=250,
                        source="fixture:history",
                        observed_at=fetch_time,
                        adjusted=True,
                    )
                    for session_day in session_dates
                ])

            audit = audit_candidate(database, candidate_id)

        self.assertEqual(audit["matched_inputs"]["historical_price_count"], 2)
        self.assertEqual(audit["unmatched"]["historical_price_ids"], [])
        self.assertEqual(audit["unmatched"]["source_timestamps"], [])


if __name__ == "__main__":
    unittest.main()
