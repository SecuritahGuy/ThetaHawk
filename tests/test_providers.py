from __future__ import annotations

import unittest
from datetime import date
from types import MethodType

from thetahawk.domain import OptionRight
from thetahawk.providers import AlpacaMarketDataProvider, _parse_occ_symbol
from thetahawk.settings import Settings


class AlpacaProviderTests(unittest.TestCase):
    def provider(self) -> AlpacaMarketDataProvider:
        return AlpacaMarketDataProvider(Settings(
            alpaca_key_id="key",
            alpaca_secret_key="secret",
        ))

    def test_equity_quotes_keep_feed_and_provider_timestamp(self) -> None:
        provider = self.provider()
        provider._get_json = MethodType(
            lambda self, base, path, params: {
                "quotes": {
                    "AAPL": {
                        "bp": 199.9,
                        "ap": 200.1,
                        "t": "2026-11-04T15:00:00.123456Z",
                    }
                }
            },
            provider,
        )

        quote, = provider.get_equity_quotes(["aapl"])

        self.assertEqual(quote.symbol, "AAPL")
        self.assertEqual(quote.source, "alpaca:stocks:iex")
        self.assertEqual(quote.midpoint, 200.0)
        self.assertEqual(quote.source_timestamp.isoformat(), "2026-11-04T15:00:00.123456+00:00")

    def test_option_chain_maps_quote_and_contract_metadata(self) -> None:
        provider = self.provider()
        provider._get_option_contracts = MethodType(
            lambda self, symbol, expiration: [{
                "symbol": "AAPL261120C00200000",
                "open_interest": "1400",
                "open_interest_date": "2026-11-04",
            }],
            provider,
        )
        provider._get_json = MethodType(
            lambda self, base, path, params: {
                "snapshots": {
                    "AAPL261120C00200000": {
                        "latestQuote": {
                            "bp": 2.1,
                            "ap": 2.2,
                            "bs": 10,
                            "as": 12,
                            "t": "2026-11-05T14:30:00Z",
                        },
                        "latestTrade": {"p": 2.15, "t": "2026-11-05T14:29:00Z"},
                        "dailyBar": {"v": 250},
                        "greeks": {"delta": 0.51},
                        "impliedVolatility": 0.32,
                    }
                }
            },
            provider,
        )

        quote, = provider.get_option_quotes("AAPL", date(2026, 11, 20))

        self.assertEqual(quote.symbol, "AAPL")
        self.assertEqual(quote.right, OptionRight.CALL)
        self.assertEqual(quote.expiration, date(2026, 11, 20))
        self.assertEqual(quote.strike, 200.0)
        self.assertEqual(quote.open_interest, 1400)
        self.assertEqual(quote.metadata["open_interest_date"], "2026-11-04")
        self.assertEqual(quote.source, "alpaca:options:indicative")

    def test_requires_credentials_and_parses_occ_contracts(self) -> None:
        with self.assertRaisesRegex(ValueError, "APCA_API_KEY_ID"):
            AlpacaMarketDataProvider(Settings())

        self.assertEqual(
            _parse_occ_symbol("AAPL261120P00195000"),
            (date(2026, 11, 20), OptionRight.PUT, 195.0),
        )
        self.assertIsNone(_parse_occ_symbol("not-an-option"))


if __name__ == "__main__":
    unittest.main()
