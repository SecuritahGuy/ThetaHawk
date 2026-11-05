from __future__ import annotations

import unittest
from datetime import date

from thetahawk.yahoo import YahooHistoricalPriceProvider


class FakeFrame:
    empty = False

    def iterrows(self):
        return iter([
            (date(2026, 11, 3), {"Close": 101.25}),
            (date(2026, 11, 4), {"Close": 102.5}),
            (date(2026, 11, 5), {"Close": None}),
        ])


class FakeTicker:
    def __init__(self) -> None:
        self.calls = []

    def history(self, **kwargs):
        self.calls.append(kwargs)
        return FakeFrame()


class YahooHistoricalPriceProviderTests(unittest.TestCase):
    def test_normalizes_daily_close_rows_and_records_fetch_time(self) -> None:
        ticker = FakeTicker()
        provider = YahooHistoricalPriceProvider(lambda symbol: self.assert_symbol(symbol, ticker))

        prices = provider.get_daily_prices("aapl", period="6mo")

        self.assertEqual(len(prices), 2)
        self.assertEqual(prices[0].symbol, "AAPL")
        self.assertEqual(prices[0].session_date, date(2026, 11, 3))
        self.assertEqual(prices[0].close, 101.25)
        self.assertTrue(prices[0].adjusted)
        self.assertEqual(prices[0].source, "yahoo-finance:yfinance:auto-adjusted-daily")
        self.assertIsNotNone(prices[0].observed_at.tzinfo)
        self.assertEqual(ticker.calls, [{
            "period": "6mo",
            "interval": "1d",
            "auto_adjust": True,
            "actions": False,
            "raise_errors": True,
        }])

    @staticmethod
    def assert_symbol(symbol: str, ticker: FakeTicker) -> FakeTicker:
        if symbol != "AAPL":
            raise AssertionError(f"Unexpected ticker {symbol}")
        return ticker


if __name__ == "__main__":
    unittest.main()
