from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, time, timedelta, timezone
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

from thetahawk.domain import OptionRight, Quote
from thetahawk.settings import Settings
from thetahawk.storage import ResearchStore
from thetahawk.unusual_scan import run_unusual_activity_scan


EASTERN = ZoneInfo("America/New_York")
CONTRACT = "AAPL261218C00345000"
SOURCE = "alpaca:options:indicative"


class UnusualActivityPipelineTests(unittest.TestCase):
    def test_scans_and_persists_candidates_from_stored_option_history(self) -> None:
        current_at = datetime(2026, 11, 5, 15, 30, tzinfo=EASTERN)
        as_of = current_at + timedelta(minutes=1)
        quotes = []
        day = current_at.date() - timedelta(days=1)
        volume_index = 0
        while volume_index < 20:
            if day.weekday() < 5:
                observed = datetime.combine(day, time(15, 30), tzinfo=EASTERN)
                quotes.append(self.quote(observed, 100 + volume_index % 5))
                volume_index += 1
            day -= timedelta(days=1)
        quotes.append(self.quote(current_at, 1_000))

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(database_path=Path(tmp) / "research.duckdb")
            with ResearchStore(settings.database_path) as store:
                store.save_quotes(quotes)
            with redirect_stdout(StringIO()):
                self.assertEqual(run_unusual_activity_scan(
                    symbols=("AAPL",),
                    settings=settings,
                    as_of=as_of,
                ), 0)
            with ResearchStore(settings.database_path) as store:
                [candidate] = store.recent_candidates(1)

        self.assertEqual(candidate["scanner"], "unusual_options_activity")
        self.assertEqual(candidate["status"], "research")
        self.assertEqual(candidate["metrics"]["baseline_session_count"], 20)

    @staticmethod
    def quote(observed_at: datetime, volume: int) -> Quote:
        return Quote(
            symbol="AAPL",
            instrument_type="option",
            expiration=date(2026, 12, 18),
            strike=345.0,
            right=OptionRight.CALL,
            observed_at=observed_at,
            source_timestamp=observed_at,
            bid=2.0,
            ask=2.1,
            volume=volume,
            open_interest=1500,
            source=SOURCE,
            metadata={"contract_symbol": CONTRACT},
        )


if __name__ == "__main__":
    unittest.main()
