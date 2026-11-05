from datetime import date, datetime, timedelta, timezone

import pytest

from thetahawk.domain import Candidate, CandidateStatus, RealizedMove, record_payload
from thetahawk.earnings_validation import validate_database, validate_records
from thetahawk.storage import ResearchStore


def _candidate(day: str, *, created: str, implied: float = 0.10) -> Candidate:
    return Candidate(
        candidate_id=f"AAPL:earnings_implied_move:{day}",
        symbol="AAPL",
        scanner="earnings_implied_move",
        score=1.0,
        created_at=datetime.fromisoformat(created),
        status=CandidateStatus.RESEARCH,
        metrics={"implied_move_fraction": implied},
    )


def _move(
    day: str,
    *,
    observed: str,
    source: str = "fixture",
    post: float = 112.0,
    post_event_date: str | None = None,
):
    reference = 100.0
    measurement_date = (
        date.fromisoformat(post_event_date)
        if post_event_date else date.fromisoformat(day) + timedelta(days=1)
    )
    return RealizedMove(
        symbol="AAPL",
        event_date=date.fromisoformat(day),
        reference_price=reference,
        post_event_price=post,
        realized_move=(post - reference) / reference,
        post_event_date=measurement_date,
        source=source,
        observed_at=datetime.fromisoformat(observed),
    )


def test_validation_matches_only_pre_event_candidates_to_later_observed_outcomes():
    report = validate_records(
        [
            _candidate("2026-03-10", created="2026-03-01T12:00:00+00:00"),
            record_payload(_candidate("2026-03-12", created="2026-03-12T08:00:00+00:00")),
        ],
        [
            _move("2026-03-10", observed="2026-03-11T12:00:00+00:00"),
            record_payload(_move("2026-03-12", observed="2026-03-12T22:00:00+00:00")),
            record_payload(_move("2026-03-10", observed="2026-03-10T22:00:00+00:00", source="same-day")),
        ],
        as_of=datetime(2026, 3, 20, tzinfo=timezone.utc),
    )

    assert report["sample_size"] == 1
    assert report["matches"][0]["event_date"] == "2026-03-10"
    assert report["matches"][0]["realized_move_magnitude"] == 0.12
    assert report["summary"]["mean_absolute_error_fraction"] == pytest.approx(0.02)
    assert report["excluded_outcome_counts"]["outcome_not_observed_after_event_date"] == 2
    assert report["excluded_candidate_counts"]["candidate_not_proven_pre_event"] == 1
    assert report["exact_replay_possible"] is False


def test_ambiguous_multiple_outcomes_are_excluded():
    report = validate_records(
        [record_payload(_candidate("2026-03-10", created="2026-03-01T12:00:00+00:00"))],
        [
            record_payload(_move("2026-03-10", observed="2026-03-11T12:00:00+00:00", source="a")),
            record_payload(_move("2026-03-10", observed="2026-03-12T12:00:00+00:00", source="b", post=110.0)),
        ],
        as_of=datetime(2026, 3, 20, tzinfo=timezone.utc),
    )

    assert report["sample_size"] == 0
    assert report["ambiguous_events"] == ["AAPL:2026-03-10"]
    assert report["excluded_candidate_counts"]["multiple_outcome_rows_for_event"] == 1


def test_outcome_without_explicit_measurement_date_is_excluded():
    outcome = record_payload(_move(
        "2026-03-10", observed="2026-03-11T12:00:00+00:00"
    ))
    outcome["post_event_date"] = None

    report = validate_records(
        [record_payload(_candidate("2026-03-10", created="2026-03-01T12:00:00+00:00"))],
        [outcome],
        as_of=datetime(2026, 3, 20, tzinfo=timezone.utc),
    )

    assert report["sample_size"] == 0
    assert report["excluded_outcome_counts"]["missing_or_invalid_post_event_date"] == 1


def test_validation_reads_candidate_and_outcome_rows_from_database(tmp_path):
    database = tmp_path / "research.duckdb"
    with ResearchStore(database) as store:
        store.save_candidates([_candidate(
            "2026-03-10", created="2026-03-01T12:00:00+00:00"
        )])
        store.save_realized_moves([_move(
            "2026-03-10", observed="2026-03-11T12:00:00+00:00"
        )])

    report = validate_database(
        database,
        as_of=datetime(2026, 3, 20, tzinfo=timezone.utc),
    )

    assert report["sample_size"] == 1
    assert report["matches"][0]["outcome_source"] == "fixture"
