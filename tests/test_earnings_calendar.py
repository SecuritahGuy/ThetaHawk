from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date
from pathlib import Path

from thetahawk.csv_inputs import CsvResearchInputs
from thetahawk.earnings_calendar import YahooEarningsCalendarProvider, export_review_csv
from thetahawk.settings import Settings


class FakeFrame:
    empty = False

    def iterrows(self):
        return iter([
            (date(2026, 10, 28), {"Reported EPS": 1.25}),
            (date(2026, 11, 12), {"Reported EPS": None}),
            (date(2026, 11, 12), {"Reported EPS": None}),
            (date(2026, 12, 3), {"Reported EPS": None}),
        ])


class FakeTicker:
    def __init__(self) -> None:
        self.calls = []

    def get_earnings_dates(self, **kwargs):
        self.calls.append(kwargs)
        return FakeFrame()


class YahooEarningsCalendarTests(unittest.TestCase):
    def test_fetches_dates_filters_range_and_does_not_infer_timing(self) -> None:
        ticker = FakeTicker()
        provider = YahooEarningsCalendarProvider(lambda symbol: self._ticker(symbol, ticker))

        events = provider.get_earnings_events(
            ("aapl",), date(2026, 11, 1), date(2026, 11, 30), limit_per_symbol=50
        )

        self.assertEqual(len(events), 1)
        event, = events
        self.assertEqual(event.symbol, "AAPL")
        self.assertEqual(event.event_date, date(2026, 11, 12))
        self.assertIsNone(event.timing)
        self.assertIsNone(event.source_timestamp)
        self.assertEqual(event.metadata["expiration_date"], None)
        self.assertEqual(event.metadata["timing_status"], "unknown_requires_review")
        self.assertIsNotNone(event.observed_at.tzinfo)
        self.assertEqual(ticker.calls, [{"limit": 50, "offset": 0}])

    def test_export_is_reviewable_and_compatible_with_existing_csv_reader(self) -> None:
        provider = YahooEarningsCalendarProvider(lambda _symbol: FakeTicker())
        events = provider.get_earnings_events(
            ("AAPL",), date(2026, 11, 1), date(2026, 11, 30)
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "earnings.csv"
            self.assertEqual(export_review_csv(events, path), 1)
            with path.open(newline="", encoding="utf-8") as stream:
                [row] = list(csv.DictReader(stream))
            self.assertEqual(row["timing"], "")
            self.assertEqual(row["expiration_date"], "")
            loaded = CsvResearchInputs(Settings(earnings_csv_path=path)).get_earnings_events(
                ("AAPL",), date(2026, 11, 1), date(2026, 11, 30)
            )
            self.assertEqual(len(loaded), 1)
            self.assertIsNone(loaded[0].timing)
            self.assertIsNone(loaded[0].metadata["expiration_date"])

    def test_export_refuses_to_overwrite_without_explicit_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "existing.csv"
            path.write_text("keep", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                export_review_csv([], path)
            self.assertEqual(path.read_text(encoding="utf-8"), "keep")

    @staticmethod
    def _ticker(symbol: str, ticker: FakeTicker) -> FakeTicker:
        if symbol != "AAPL":
            raise AssertionError(f"Unexpected symbol {symbol}")
        return ticker


if __name__ == "__main__":
    unittest.main()
