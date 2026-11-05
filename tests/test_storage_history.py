from __future__ import annotations

import tempfile
import unittest
import json
from datetime import date, datetime, timezone
from pathlib import Path

from thetahawk.domain import HistoricalPrice, OptionRight, Quote
from thetahawk.storage import ResearchStore


class HistoricalPriceStorageTests(unittest.TestCase):
    def test_persists_stable_option_snapshot_expiration_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with ResearchStore(Path(tmp) / "research.duckdb") as store:
                self.assertIsNone(store.option_snapshot_expiration("aapl"))
                store.save_option_snapshot_expiration(
                    "aapl",
                    date(2027, 1, 15),
                    updated_at=datetime(2026, 11, 5, tzinfo=timezone.utc),
                )
                self.assertEqual(
                    store.option_snapshot_expiration("AAPL"), date(2027, 1, 15)
                )

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

    def test_loads_option_quotes_with_contract_and_feed_provenance(self) -> None:
        observed_at = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)
        quote = Quote(
            symbol="AAPL",
            instrument_type="option",
            expiration=date(2026, 12, 4),
            strike=345.0,
            right=OptionRight.CALL,
            observed_at=observed_at,
            source_timestamp=observed_at,
            bid=4.9,
            ask=5.1,
            volume=125,
            open_interest=850,
            source="alpaca:options:indicative",
            metadata={"contract_symbol": "AAPL261204C00345000"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            with ResearchStore(Path(tmp) / "research.duckdb") as store:
                store.save_quotes([quote])
                [loaded] = store.option_quotes("aapl")

        self.assertEqual(loaded.right, OptionRight.CALL)
        self.assertEqual(loaded.expiration, date(2026, 12, 4))
        self.assertEqual(loaded.source, quote.source)
        self.assertEqual(loaded.metadata["contract_symbol"], "AAPL261204C00345000")


if __name__ == "__main__":
    unittest.main()
