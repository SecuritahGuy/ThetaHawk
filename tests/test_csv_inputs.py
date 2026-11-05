from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from thetahawk.csv_inputs import CsvResearchInputs
from thetahawk.settings import Settings


class CsvResearchInputsTests(unittest.TestCase):
    def test_parses_provenance_and_derives_realized_return(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events_path = root / "earnings.csv"
            moves_path = root / "moves.csv"
            events_path.write_text(
                "symbol,event_date,timing,expiration_date,observed_at,source,fiscal_period\n"
                "DEMO,2026-11-20,after_close,2026-11-27,2026-11-01T12:00:00Z,fixture,Q3\n",
                encoding="utf-8",
            )
            moves_path.write_text(
                "symbol,event_date,post_event_date,reference_price,post_event_price,observed_at,source\n"
                "DEMO,2026-08-01,2026-08-03,100,108,2026-08-04T10:00:00-04:00,fixture\n",
                encoding="utf-8",
            )
            provider = CsvResearchInputs(Settings(
                earnings_csv_path=events_path,
                realized_moves_csv_path=moves_path,
            ))

            events = provider.get_earnings_events(
                ["DEMO"], date(2026, 11, 1), date(2026, 11, 30)
            )
            moves = provider.get_realized_moves(["DEMO"])

            self.assertEqual(events[0].source, "fixture")
            self.assertEqual(events[0].metadata["expiration_date"], "2026-11-27")
            self.assertEqual(moves[0].realized_move, 0.08)
            self.assertEqual(moves[0].post_event_date, date(2026, 8, 3))
            self.assertEqual(moves[0].observed_at.utcoffset().total_seconds(), -4 * 3600)

    def test_rejects_timezone_naive_observed_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events_path = Path(tmp) / "earnings.csv"
            events_path.write_text(
                "symbol,event_date,timing,expiration_date,observed_at,source\n"
                "DEMO,2026-11-20,after_close,2026-11-27,2026-11-01T12:00:00,fixture\n",
                encoding="utf-8",
            )
            provider = CsvResearchInputs(Settings(earnings_csv_path=events_path))
            with self.assertRaisesRegex(ValueError, "timezone"):
                provider.get_earnings_events(["DEMO"], date(2026, 11, 1), date(2026, 11, 30))


if __name__ == "__main__":
    unittest.main()
