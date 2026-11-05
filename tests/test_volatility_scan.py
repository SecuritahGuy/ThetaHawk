from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

from thetahawk.domain import HistoricalPrice, OptionRight, Quote
from thetahawk.replay import audit_candidate
from thetahawk.settings import Settings
from thetahawk.storage import ResearchStore
from thetahawk.volatility_scan import run_volatility_scan


class FakeMarketProvider:
    name = "fixture-market"

    def __init__(self) -> None:
        self.now = datetime.now(timezone.utc)
        self.expiration = self.now.astimezone(ZoneInfo("America/New_York")).date() + timedelta(days=30)

    def get_equity_quotes(self, symbols):
        return [Quote(
            symbol=symbol,
            observed_at=self.now,
            source_timestamp=self.now,
            bid=99.9,
            ask=100.1,
            source="fixture:stocks",
        ) for symbol in symbols]

    def get_option_expirations(self, symbol, *, start=None, end=None):
        return [self.expiration]

    def get_option_quotes(self, symbol, expiration):
        return [Quote(
            symbol=symbol,
            instrument_type="option",
            expiration=expiration,
            strike=100,
            right=OptionRight.CALL,
            observed_at=self.now,
            source_timestamp=self.now,
            bid=1.9,
            ask=2.1,
            volume=5,
            open_interest=10,
            implied_volatility=0.3,
            source="fixture:options",
            metadata={"contract_symbol": f"{symbol}261204C00100000"},
        )]


class FakeHistoryProvider:
    name = "fixture-history"

    def __init__(self) -> None:
        now = datetime.now(timezone.utc)
        self.prices = [HistoricalPrice(
            symbol="DEMO",
            session_date=(now - timedelta(days=1)).date(),
            close=100.0,
            source="fixture:daily",
            observed_at=now,
            adjusted=True,
        )]

    def get_daily_prices(self, symbol, *, period):
        return self.prices


class VolatilityScanPipelineTests(unittest.TestCase):
    def test_persists_manifest_with_exact_quote_option_and_history_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(database_path=Path(tmp) / "research.duckdb")
            with redirect_stdout(StringIO()):
                self.assertEqual(run_volatility_scan(
                    symbols=("DEMO",),
                    settings=settings,
                    market_provider=FakeMarketProvider(),
                    history_provider=FakeHistoryProvider(),
                ), 0)
            with ResearchStore(settings.database_path) as store:
                [candidate] = store.recent_candidates(1)
                [manifest] = store.scan_runs_for_candidate(candidate["candidate_id"])
            audit = audit_candidate(settings.database_path, candidate["candidate_id"])

        self.assertEqual(candidate["scanner"], "volatility_divergence")
        self.assertEqual(manifest["scanner"], "volatility_divergence")
        self.assertEqual(manifest["policy_version"], "volatility-divergence-policy-v1")
        self.assertEqual(manifest["inputs"]["history_period"], "1y")
        [screen_input] = manifest["inputs"]["screen_inputs"]
        self.assertEqual(screen_input["underlying_quote"]["source"], "fixture:stocks")
        self.assertEqual(len(screen_input["option_quotes"]), 1)
        self.assertEqual(screen_input["option_quotes"][0]["source"], "fixture:options")
        self.assertEqual(screen_input["historical_prices"][0]["source"], "fixture:daily")
        self.assertEqual(screen_input["realized_window_sessions"], 20)
        self.assertTrue(audit["exact_replay_possible"])
        self.assertTrue(audit["recomputation"]["matches_stored_candidate"])


if __name__ == "__main__":
    unittest.main()
