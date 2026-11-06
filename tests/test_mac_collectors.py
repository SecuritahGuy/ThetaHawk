from __future__ import annotations

import unittest

from scripts.install_mac_collectors import JOBS, _agent_payload


class MacCollectorInstallerTests(unittest.TestCase):
    def test_installed_service_runs_both_option_jobs_as_one_writer(self) -> None:
        job, label = next(iter(JOBS.items()))
        payload = _agent_payload(job, label)

        self.assertEqual(job, "options-and-earnings")
        self.assertEqual(payload["Label"], "com.thetahawk.options-collector")
        self.assertEqual(payload["ProgramArguments"][5], job)
        self.assertIn("--options-history-min-dte", payload["ProgramArguments"])
        self.assertEqual(payload["StandardOutPath"].split("/")[-1], "options.log")
        self.assertTrue(payload["KeepAlive"])


if __name__ == "__main__":
    unittest.main()
