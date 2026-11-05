from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from thetahawk.domain import OptionRight, Quote
from thetahawk.option_snapshots import (
    collect_option_snapshots,
    select_expiration,
)
from thetahawk.settings import Settings


class FakeProvider:
    def __init__(self, expirations_by_symbol, quotes_by_key):
        self.expirations_by_symbol = expirations_by_symbol
        self.quotes_by_key = quotes_by_key
        self.expiration_calls = []
        self.quote_calls = []

    def get_option_expirations(self, symbol, *, start=None, end=None):
        self.expiration_calls.append((symbol, start, end))
        return self.expirations_by_symbol.get(symbol, [])

    def get_option_quotes(self, symbol, expiration=None):
        self.quote_calls.append((symbol, expiration))
        return self.quotes_by_key.get((symbol, expiration), [])


class FakeStore:
    def __init__(self):
        self.quotes = []

    def save_quotes(self, quotes):
        self.quotes.extend(quotes)
        return len(quotes)


def option_quote(symbol: str, expiration: date, right: OptionRight) -> Quote:
    now = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)
    return Quote(
        symbol=symbol,
        instrument_type="option",
        expiration=expiration,
        strike=100,
        right=right,
        observed_at=now,
        source_timestamp=now,
        bid=1.0,
        ask=1.1,
        source="fixture:options",
    )


class OptionSnapshotCollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.as_of = date(2026, 11, 5)

    def test_selects_nearest_listed_expiration_and_ties_to_earlier(self) -> None:
        expirations = [
            self.as_of.replace(day=20),  # 15 DTE
            date(2026, 12, 4),  # 29 DTE
            date(2026, 12, 6),  # 31 DTE
            date(2027, 1, 1),  # Outside the configured window
        ]

        selected = select_expiration(expirations, as_of=self.as_of)

        self.assertEqual(selected, date(2026, 12, 4))

    def test_fetches_only_selected_expiration_and_persists_matching_quotes(self) -> None:
        chosen = date(2026, 12, 4)
        other = date(2026, 12, 6)
        provider = FakeProvider(
            {"AAPL": [chosen, other], "EMPTY": []},
            {
                ("AAPL", chosen): [
                    option_quote("AAPL", chosen, OptionRight.CALL),
                    option_quote("AAPL", chosen, OptionRight.PUT),
                    option_quote("AAPL", other, OptionRight.CALL),
                    option_quote("MSFT", chosen, OptionRight.CALL),
                ],
            },
        )
        store = FakeStore()
        settings = Settings(symbols=("AAPL", "EMPTY"))

        with self.assertLogs("thetahawk.option_snapshots", level="WARNING") as logs:
            result = collect_option_snapshots(
                settings=settings,
                provider=provider,
                store=store,
                as_of=self.as_of,
            )

        self.assertEqual(result.requested_symbols, 2)
        self.assertEqual(result.symbols_with_expiration, 1)
        self.assertEqual(result.symbols_without_eligible_expiration, ("EMPTY",))
        self.assertEqual(result.selected_expirations, {"AAPL": chosen})
        self.assertEqual(result.fetched_quotes, 2)
        self.assertEqual(result.saved_quotes, 2)
        self.assertEqual(len(store.quotes), 2)
        self.assertEqual(provider.quote_calls, [("AAPL", chosen)])
        self.assertIn("Skipping EMPTY", logs.output[0])

    def test_passes_configured_window_to_expiration_lookup(self) -> None:
        start = date(2026, 11, 20)
        end = date(2026, 12, 20)
        provider = FakeProvider({"AAPL": []}, {})

        collect_option_snapshots(
            ("AAPL",),
            provider=provider,
            store=FakeStore(),
            as_of=self.as_of,
            min_dte=15,
            max_dte=45,
            target_dte=30,
        )

        self.assertEqual(provider.expiration_calls, [("AAPL", start, date(2026, 12, 20))])

    def test_rejects_target_outside_window(self) -> None:
        with self.assertRaisesRegex(ValueError, "target_dte"):
            select_expiration(
                [date(2026, 12, 5)],
                as_of=self.as_of,
                min_dte=14,
                max_dte=45,
                target_dte=60,
            )


if __name__ == "__main__":
    unittest.main()
