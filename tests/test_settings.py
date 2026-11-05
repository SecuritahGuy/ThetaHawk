from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from thetahawk.settings import Settings


class SettingsTests(unittest.TestCase):
    def test_reads_dotenv_and_does_not_expose_keys_in_repr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text(
                "APCA_API_KEY_ID=local-key\n"
                "APCA_API_SECRET_KEY=local-secret\n"
                "THETAHAWK_ALPACA_OPTIONS_FEED=opra\n"
                "THETAHAWK_SEC_USER_AGENT=Thetahawk research contact@example.com\n",
                encoding="utf-8",
            )
            with patch("pathlib.Path.cwd", return_value=root), patch.dict(os.environ, {}, clear=True):
                settings = Settings.from_env()

        self.assertEqual(settings.alpaca_key_id, "local-key")
        self.assertEqual(settings.alpaca_options_feed, "opra")
        self.assertEqual(settings.sec_user_agent, "Thetahawk research contact@example.com")
        self.assertNotIn("local-secret", repr(settings))
        self.assertNotIn("contact@example.com", repr(settings))


if __name__ == "__main__":
    unittest.main()
