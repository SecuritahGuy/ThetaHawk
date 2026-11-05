from __future__ import annotations

import unittest
from datetime import datetime, timezone

from thetahawk.domain import Candidate, CandidateStatus
from thetahawk.discovery_ranking import rank_discoveries


NOW = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)
CALL = "AAPL261218C00345000"
PUT = "AAPL261218P00345000"


def candidate(
    candidate_id: str,
    symbol: str,
    scanner: str,
    status: CandidateStatus,
    *,
    score: float = 0,
    inputs: tuple[str, ...] = (),
    metrics: dict | None = None,
    created_at: datetime = NOW,
    summary: str | None = None,
) -> Candidate:
    return Candidate(
        candidate_id=candidate_id,
        symbol=symbol,
        scanner=scanner,
        score=score,
        created_at=created_at,
        status=status,
        summary=summary or f"{scanner} result",
        input_ids=inputs,
        metrics=metrics or {},
    )


class DiscoveryRankingTests(unittest.TestCase):
    def test_groups_overlapping_contracts_and_retains_suppressed_context(self) -> None:
        activity = candidate(
            "activity-1",
            "AAPL",
            "unusual_options_activity",
            CandidateStatus.RESEARCH,
            score=75,
            metrics={"contract_symbol": CALL},
        )
        volatility = candidate(
            "volatility-1",
            "AAPL",
            "volatility_divergence",
            CandidateStatus.RESEARCH,
            score=5,
            inputs=(CALL, PUT),
        )
        underlying_context = candidate(
            "equity-1",
            "AAPL",
            "overextended_equity",
            CandidateStatus.SUPPRESSED,
            summary="Not overextended",
        )

        [discovery] = rank_discoveries((activity, volatility, underlying_context))

        self.assertEqual(discovery.contract_symbols, (CALL, PUT))
        self.assertEqual(
            {record.candidate_id for record in discovery.candidates},
            {"activity-1", "volatility-1", "equity-1"},
        )
        self.assertEqual(
            discovery.supporting_screens,
            ("unusual_options_activity", "volatility_divergence"),
        )
        self.assertEqual(discovery.suppressed_screens, ("overextended_equity",))
        self.assertEqual(discovery.research_screen_count, 2)

    def test_separates_distinct_contracts_but_attaches_symbol_context(self) -> None:
        call = candidate(
            "call-activity", "AAPL", "unusual_options_activity",
            CandidateStatus.RESEARCH, metrics={"contract_symbol": CALL},
        )
        put = candidate(
            "other-activity", "AAPL", "unusual_options_activity",
            CandidateStatus.RESEARCH, metrics={"contract_symbol": PUT},
        )
        context = candidate(
            "equity-context", "AAPL", "overextended_equity", CandidateStatus.RESEARCH
        )

        discoveries = rank_discoveries((call, put, context))

        self.assertEqual(len(discoveries), 2)
        self.assertEqual({row.contract_symbols for row in discoveries}, {(CALL,), (PUT,)})
        for discovery in discoveries:
            self.assertIn("equity-context", {record.candidate_id for record in discovery.candidates})

    def test_rank_counts_distinct_research_screens_without_averaging_scores(self) -> None:
        two_screen = [
            candidate(
                "r1", "MSFT", "overextended_equity", CandidateStatus.RESEARCH,
                score=0.1,
            ),
            candidate(
                "r2", "MSFT", "volatility_divergence", CandidateStatus.RESEARCH,
                score=0.2,
            ),
        ]
        high_raw_score = candidate(
            "r3", "NVDA", "unusual_options_activity", CandidateStatus.RESEARCH,
            score=99.9,
        )

        ranked = rank_discoveries([*two_screen, high_raw_score])

        self.assertEqual(ranked[0].symbol, "MSFT")
        self.assertEqual(ranked[0].rank, 1)
        self.assertEqual(ranked[0].research_screen_count, 2)
        self.assertIn("scores remain separate", ranked[0].ranking_basis)
        self.assertEqual(ranked[1].symbol, "NVDA")

    def test_deduplicates_same_candidate_id_to_newest_record(self) -> None:
        earlier = candidate(
            "same-id", "AAPL", "volatility_divergence", CandidateStatus.SUPPRESSED,
            created_at=NOW,
        )
        newer = candidate(
            "same-id", "AAPL", "volatility_divergence", CandidateStatus.RESEARCH,
            created_at=NOW.replace(minute=1),
        )

        [discovery] = rank_discoveries((earlier, newer))

        self.assertEqual(len(discovery.candidates), 1)
        self.assertEqual(discovery.candidates[0].status, CandidateStatus.RESEARCH)

    def test_suppressed_only_symbol_group_is_retained_at_end(self) -> None:
        suppressed = candidate(
            "suppressed", "DEMO", "earnings_implied_move", CandidateStatus.SUPPRESSED
        )

        [discovery] = rank_discoveries((suppressed,))

        self.assertEqual(discovery.rank, 1)
        self.assertEqual(discovery.research_screen_count, 0)
        self.assertEqual(discovery.ranking_basis,
                         "No research screen met its configured threshold; retained for audit.")


if __name__ == "__main__":
    unittest.main()
