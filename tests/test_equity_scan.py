from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

from thetahawk.domain import HistoricalPrice, Quote
from thetahawk.equity_scan import run_equity_scan
from thetahawk.replay import audit_candidate
from thetahawk.settings import Settings
from thetahawk.storage import ResearchStore


class FakeMarketProvider:
    name = "fake-market"

    def __init__(self, quote: Quote) -> None:
        self.quote = quote

    def get_equity_quotes(self, symbols):
        return [self.quote]


class FakeHistoryProvider:
    def __init__(self, prices) -> None:
        self.prices = prices

    def get_daily_prices(self, symbol, *, period):
        return self.prices


class EquityScanPipelineTests(unittest.TestCase):
    def test_persists_live_quote_history_and_screen_result(self) -> None:
        as_of = datetime.now(timezone.utc)
        quote = Quote(
            symbol="DEMO",
            observed_at=as_of,
            source_timestamp=as_of,
            bid=119.9,
            ask=120.1,
            source="fixture:quote",
        )
        prices = [
            HistoricalPrice(
                symbol="DEMO",
                session_date=(as_of - timedelta(days=65 - index + 1)).date(),
                close=100 + index * 0.03 + ((index * 7) % 11) * 0.07,
                source="fixture:history",
                observed_at=as_of,
                adjusted=True,
            )
            for index in range(65)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(database_path=Path(tmp) / "research.duckdb")
            with redirect_stdout(StringIO()):
                self.assertEqual(run_equity_scan(
                    symbols=("DEMO",),
                    settings=settings,
                    market_provider=FakeMarketProvider(quote),
                    history_provider=FakeHistoryProvider(prices),
                ), 0)
            with ResearchStore(settings.database_path) as store:
                candidates = store.recent_candidates(1)
                [manifest] = store.scan_runs_for_candidate(candidates[0]["candidate_id"])
                history_count = store._connection.execute(
                    "SELECT COUNT(*) FROM historical_prices"
                ).fetchone()[0]
            audit = audit_candidate(settings.database_path, candidates[0]["candidate_id"])

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["scanner"], "overextended_equity")
        self.assertEqual(history_count, 65)
        self.assertEqual(manifest["scanner"], "overextended_equity")
        self.assertEqual(manifest["policy_version"], "overextended-equity-policy-v1")
        [screen_input] = manifest["inputs"]["screen_inputs"]
        self.assertEqual(screen_input["quote"]["source"], "fixture:quote")
        self.assertEqual(len(screen_input["historical_prices"]), 65)
        self.assertEqual(screen_input["window_sessions"], 20)
        self.assertTrue(audit["exact_replay_possible"])
        self.assertTrue(audit["recomputation"]["matches_stored_candidate"])


if __name__ == "__main__":
    unittest.main()
