from __future__ import annotations

import tempfile
import unittest
import json
from datetime import date, datetime, timezone
from pathlib import Path

from thetahawk.domain import HistoricalPrice
from thetahawk.storage import ResearchStore


class HistoricalPriceStorageTests(unittest.TestCase):
    def test_persists_and_replaces_same_symbol_session_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with ResearchStore(Path(tmp) / "research.duckdb") as store:
                first = HistoricalPrice(
                    symbol="AAPL",
                    session_date=date(2026, 11, 4),
                    close=250.0,
                    source="yahoo-fixture",
                    observed_at=datetime(2026, 11, 5, tzinfo=timezone.utc),
                    adjusted=True,
                )
                revised = HistoricalPrice(
                    symbol="AAPL",
                    session_date=date(2026, 11, 4),
                    close=251.0,
                    source="yahoo-fixture",
                    observed_at=datetime(2026, 11, 6, tzinfo=timezone.utc),
                    adjusted=True,
                )
                self.assertEqual(store.save_historical_prices([first]), 1)
                self.assertEqual(store.save_historical_prices([revised]), 1)
                rows = store._connection.execute(
                    "SELECT payload FROM historical_prices"
                ).fetchall()

        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0][0])["close"], 251.0)


if __name__ == "__main__":
    unittest.main()
