from __future__ import annotations

import unittest

from thetahawk.dashboard import (
    _candidate_table_rows,
    _expiration_options,
    _filter_candidates,
)


class DashboardCandidateViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.candidates = [
            {
                "symbol": "AAPL",
                "scanner": "volatility_divergence",
                "status": "research",
                "score": 37.5,
                "created_at": "2026-11-05T15:00:00+00:00",
                "summary": "IV is elevated versus recent realized volatility.",
                "risks": ["Backward-looking comparison."],
                "input_ids": ["AAPL261204C00345000"],
                "source_timestamps": ["2026-11-05T14:59:00+00:00"],
                "metrics": {
                    "option_expiration": "2026-12-04",
                    "option_dte": 29,
                    "iv_to_rv_ratio": 1.35,
                    "call_quote_source": "alpaca:options:indicative",
                },
            },
            {
                "symbol": "MSFT",
                "scanner": "overextended_equity",
                "status": "suppressed",
                "score": 0.0,
                "summary": "Move is within the configured threshold.",
                "risks": [],
                "metrics": {"robust_z_score": 0.4},
            },
        ]

    def test_filters_by_symbol_screen_status_and_expiration(self) -> None:
        results = _filter_candidates(
            self.candidates,
            symbol="aapl",
            scanner="volatility_divergence",
            status="research",
            expiration="2026-12-04",
        )

        self.assertEqual(results, [self.candidates[0]])

    def test_expiration_choices_only_include_candidates_with_expiration_metrics(self) -> None:
        self.assertEqual(
            _expiration_options(self.candidates),
            ["All expirations", "2026-12-04"],
        )

    def test_table_includes_metrics_provenance_and_risk_context(self) -> None:
        [row] = _candidate_table_rows(self.candidates[:1])

        self.assertEqual(row["expiration"], "2026-12-04")
        self.assertEqual(row["key_metrics"]["iv_to_rv_ratio"], 1.35)
        self.assertIn("alpaca:options:indicative", row["sources"])
        self.assertIn("Backward-looking", row["risk_notes"])

    def test_filter_without_expiration_keeps_screen_candidates(self) -> None:
        results = _filter_candidates(self.candidates, status="suppressed")

        self.assertEqual(results, [self.candidates[1]])

    def test_table_surfaces_sec_filing_context_and_link(self) -> None:
        candidate = {
            "symbol": "AAPL",
            "scanner": "event_catalyst",
            "status": "research",
            "summary": "SEC 8-K filing dated 2026-11-05; review source document.",
            "metrics": {
                "form_type": "8-K",
                "filing_date": "2026-11-05",
                "acceptance_datetime": "2026-11-05T16:30:00+00:00",
                "report_date": "2026-11-04",
                "accession_number": "0000320193-26-000001",
                "filing_url": "https://www.sec.gov/Archives/edgar/data/320193/example.htm",
                "items": ["2.02"],
                "filing_source": "sec-edgar:company-submissions",
            },
        }

        [row] = _candidate_table_rows([candidate])

        self.assertEqual(row["key_metrics"]["form_type"], "8-K")
        self.assertEqual(row["key_metrics"]["filing_date"], "2026-11-05")
        self.assertEqual(row["key_metrics"]["items"], ["2.02"])
        self.assertIn("https://www.sec.gov/Archives/edgar/data/320193/example.htm", row["key_metrics"]["filing_url"])
        self.assertIn("sec-edgar:company-submissions", row["sources"])


if __name__ == "__main__":
    unittest.main()
