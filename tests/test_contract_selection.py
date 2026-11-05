from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from thetahawk.contract_selection import select_option_pairs
from thetahawk.domain import OptionRight, Quote


class ContractSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.as_of = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)
        self.underlying = Quote(
            symbol="AAPL",
            observed_at=self.as_of,
            source_timestamp=self.as_of,
            bid=99.9,
            ask=100.1,
            source="alpaca:stocks:iex",
        )

    def pair(self, expiry: date, strike: float, *, stale: bool = False, oi_date: str | None = None):
        timestamp = self.as_of - timedelta(minutes=20) if stale else self.as_of
        open_interest_date = oi_date or self.as_of.date().isoformat()
        return [
            self.quote(expiry, strike, OptionRight.CALL, timestamp, open_interest_date),
            self.quote(expiry, strike, OptionRight.PUT, timestamp, open_interest_date),
        ]

    def quote(
        self,
        expiry: date,
        strike: float,
        right: OptionRight,
        timestamp: datetime,
        oi_date: str | None,
        *,
        spread: float = 0.1,
        volume: int = 10,
        open_interest: int = 100,
    ) -> Quote:
        return Quote(
            symbol="AAPL",
            instrument_type="option",
            expiration=expiry,
            strike=strike,
            right=right,
            observed_at=timestamp,
            source_timestamp=timestamp,
            bid=1.0,
            ask=1.0 + spread,
            volume=volume,
            open_interest=open_interest,
            source="alpaca:options:indicative",
            metadata={"open_interest_date": oi_date} if oi_date else {},
        )

    def test_ranks_target_dte_then_moneyness_deterministically(self) -> None:
        options = [
            *self.pair(date(2026, 12, 11), 100),  # 36 DTE
            *self.pair(date(2026, 12, 4), 101),   # 29 DTE, 1% OTM
            *self.pair(date(2026, 11, 20), 100),  # 15 DTE
            *self.pair(date(2026, 12, 4), 100),   # 29 DTE, ATM
        ]

        pairs = select_option_pairs(options, self.underlying, self.as_of)

        self.assertEqual(
            [(pair.expiration, pair.strike) for pair in pairs],
            [
                (date(2026, 12, 4), 100),
                (date(2026, 12, 4), 101),
                (date(2026, 12, 11), 100),
                (date(2026, 11, 20), 100),
            ],
        )
        self.assertEqual(pairs[0].symbol, "AAPL")
        self.assertEqual(pairs[0].combined_midpoint, 2.1)
        self.assertEqual(pairs[0].dte, 29)

    def test_filters_stale_markets_missing_oi_and_wide_spreads(self) -> None:
        valid = self.pair(date(2026, 12, 4), 100)
        stale = self.pair(date(2026, 11, 20), 100, stale=True)
        missing_oi = [
            self.quote(date(2026, 12, 11), 100, right, self.as_of, None)
            for right in (OptionRight.CALL, OptionRight.PUT)
        ]
        wide_spread = [
            self.quote(date(2026, 12, 18), 100, right, self.as_of,
                       self.as_of.date().isoformat(), spread=0.5)
            for right in (OptionRight.CALL, OptionRight.PUT)
        ]

        pairs = select_option_pairs(
            [*valid, *stale, *missing_oi, *wide_spread],
            self.underlying,
            self.as_of,
        )

        self.assertEqual([(pair.expiration, pair.strike) for pair in pairs], [
            (date(2026, 12, 4), 100),
        ])

    def test_filters_mismatched_source_and_bounds_results(self) -> None:
        valid = self.pair(date(2026, 12, 4), 100)
        mismatched_call, mismatched_put = self.pair(date(2026, 12, 18), 100)
        mismatched_put = replace(mismatched_put, source="alpaca:options:opra")
        options = [
            *valid,
            mismatched_call,
            mismatched_put,
            *self.pair(date(2026, 11, 20), 100),
            *self.pair(date(2026, 12, 11), 100),
        ]

        pairs = select_option_pairs(
            options, self.underlying, self.as_of, max_pairs=1
        )

        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].expiration, date(2026, 12, 4))

    def test_rejects_invalid_policy_and_naive_scan_time(self) -> None:
        with self.assertRaisesRegex(ValueError, "DTE policy"):
            select_option_pairs(
                [], self.underlying, self.as_of, min_dte=30, max_dte=20, target_dte=25
            )
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            select_option_pairs(
                [], self.underlying, datetime(2026, 11, 5, 15, 0)
            )


if __name__ == "__main__":
    unittest.main()
