from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from thetahawk.domain import OptionRight, Quote
from thetahawk.data_status import inspect_data_coverage
from thetahawk.earnings_option_snapshots import collect_earnings_option_snapshots
from thetahawk.settings import Settings


class FakeProvider:
    name = "fixture-options"

    def __init__(self, quotes_by_key):
        self.quotes_by_key = quotes_by_key
        self.calls = []

    def get_option_quotes(self, symbol, expiration=None):
        self.calls.append((symbol, expiration))
        return self.quotes_by_key.get((symbol, expiration), [])


class FakeStore:
    def __init__(self):
        self.quotes = []
        self.events = []

    def save_quotes(self, quotes):
        self.quotes.extend(quotes)
        return len(quotes)

    def save_earnings_events(self, events):
        self.events.extend(events)
        return len(events)


def option_quote(symbol, expiration, right):
    observed = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)
    return Quote(
        symbol=symbol,
        instrument_type="option",
        expiration=expiration,
        strike=100,
        right=right,
        observed_at=observed,
        source_timestamp=observed,
        bid=1.0,
        ask=1.1,
        source="fixture:options",
        metadata={"contract_symbol": f"{symbol}261204{'C' if right == OptionRight.CALL else 'P'}00100000"},
    )


class EarningsOptionSnapshotTests(unittest.TestCase):
    def test_persists_reviewed_event_inputs_for_later_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            event_path = root / "earnings.csv"
            event_path.write_text(
                "symbol,event_date,timing,expiration_date,observed_at,source\n"
                "AAPL,2026-11-17,after_close,2026-12-18,"
                "2026-11-01T12:00:00Z,reviewed-calendar\n",
                encoding="utf-8",
            )
            settings = Settings(
                database_path=root / "research.duckdb",
                earnings_csv_path=event_path,
                symbols=("AAPL",),
            )

            result = collect_earnings_option_snapshots(
                settings=settings,
                provider=FakeProvider({}),
                as_of=datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc),
            )

            [row] = inspect_data_coverage(settings.database_path).symbols

        self.assertEqual(result.saved_events, 1)
        self.assertEqual(row.earnings_event_rows, 1)
        self.assertEqual(row.earnings_events_with_reviewed_timing, 1)

    def test_requires_reviewed_upcoming_event_and_tags_deduplicated_chains(self):
        as_of = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)  # 10:00 ET
        aapl_expiry = date(2026, 12, 4)
        msft_expiry = date(2026, 11, 12)
        events = (
            "symbol,event_date,timing,expiration_date,observed_at,source\n"
            "AAPL,2026-11-12,after_close,2026-12-04,2026-11-01T12:00:00Z,reviewed-calendar\n"
            "AAPL,2026-11-20,after_close,2026-12-04,2026-11-01T12:00:00Z,reviewed-calendar\n"
            "MSFT,2026-11-12,before_open,2026-11-12,2026-11-01T12:00:00Z,reviewed-calendar\n"
            "NVDA,2026-11-12,,2026-12-04,2026-11-01T12:00:00Z,unreviewed-calendar\n"
            "GOOG,2026-11-12,after_close,2026-11-12,2026-11-01T12:00:00Z,reviewed-calendar\n"
            "AMZN,2026-11-04,after_close,2026-12-04,2026-11-01T12:00:00Z,reviewed-calendar\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "earnings.csv"
            event_path.write_text(events, encoding="utf-8")
            settings = Settings(earnings_csv_path=event_path, symbols=("AAPL", "MSFT", "NVDA", "GOOG", "AMZN"))
            provider = FakeProvider({
                ("AAPL", aapl_expiry): [
                    option_quote("AAPL", aapl_expiry, OptionRight.CALL),
                    option_quote("AAPL", aapl_expiry, OptionRight.PUT),
                    option_quote("MSFT", aapl_expiry, OptionRight.CALL),
                ],
                ("MSFT", msft_expiry): [
                    option_quote("MSFT", msft_expiry, OptionRight.CALL),
                ],
            })
            store = FakeStore()

            result = collect_earnings_option_snapshots(
                settings=settings,
                provider=provider,
                store=store,
                as_of=as_of,
            )

        self.assertEqual(result.reviewed_events, 5)
        self.assertEqual(result.saved_events, 5)
        self.assertEqual(len(store.events), 5)
        self.assertEqual(store.events[0].source, "reviewed-calendar")
        self.assertEqual(result.eligible_events, 3)
        self.assertEqual(result.requested_chains, 2)
        self.assertEqual(provider.calls, [("AAPL", aapl_expiry), ("MSFT", msft_expiry)])
        self.assertEqual(result.fetched_quotes, 3)
        self.assertEqual(result.saved_quotes, 3)
        self.assertEqual(len(result.skipped_events), 2)
        aapl_refs = store.quotes[0].metadata["earnings_events"]
        self.assertEqual(len(aapl_refs), 2)
        self.assertEqual(store.quotes[0].metadata["snapshot_purpose"], "reviewed_earnings_event")
        self.assertIn("contract_symbol", store.quotes[0].metadata)

    def test_requires_timezone_aware_scan_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "earnings.csv"
            event_path.write_text("symbol,event_date,timing,expiration_date,observed_at,source\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "timezone-aware"):
                collect_earnings_option_snapshots(
                    ("AAPL",),
                    settings=Settings(earnings_csv_path=event_path),
                    provider=FakeProvider({}),
                    store=FakeStore(),
                    as_of=datetime(2026, 11, 5),
                )


if __name__ == "__main__":
    unittest.main()
