from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from thetahawk.scan import run_scan
from thetahawk.settings import Settings
from thetahawk.storage import ResearchStore


class ScanPipelineTests(unittest.TestCase):
    def test_demo_scan_persists_suppressed_record_without_option_quotes(self) -> None:
        now = datetime.now(timezone.utc)
        event_date = now.date() + timedelta(days=5)
        expiration_date = event_date + timedelta(days=7)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            event_path = root / "earnings.csv"
            moves_path = root / "realized_moves.csv"
            event_path.write_text(
                "symbol,event_date,timing,expiration_date,observed_at,source\n"
                f"DEMO,{event_date.isoformat()},after_close,{expiration_date.isoformat()},"
                f"{now.isoformat()},fixture\n",
                encoding="utf-8",
            )
            moves_path.write_text(
                "symbol,event_date,reference_price,post_event_price,observed_at,source\n",
                encoding="utf-8",
            )
            env = {
                "THETAHAWK_DATA_DIR": str(root),
                "THETAHAWK_EARNINGS_CSV": str(event_path),
                "THETAHAWK_REALIZED_MOVES_CSV": str(moves_path),
            }
            with patch.dict(os.environ, env, clear=False), redirect_stdout(StringIO()):
                self.assertEqual(run_scan(use_alpaca=False, symbols=("DEMO",)), 0)

            with ResearchStore(Settings.from_env(env).database_path) as store:
                [candidate] = store.recent_candidates()
            self.assertEqual(candidate["status"], "suppressed")
            self.assertIn("no option-chain fixture", candidate["summary"])


if __name__ == "__main__":
    unittest.main()
