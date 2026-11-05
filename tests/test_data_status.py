from datetime import date, datetime, time, timedelta, timezone

from thetahawk.data_status import inspect_data_coverage
from thetahawk.domain import EarningsEvent, HistoricalPrice, OptionRight, Quote, RealizedMove
from thetahawk.storage import ResearchStore


def test_reports_comparable_contract_history_and_research_input_coverage(tmp_path):
    database = tmp_path / "coverage.duckdb"
    eastern = timezone(timedelta(hours=-5))
    quotes = []
    day = date(2026, 10, 1)
    sessions = 0
    while sessions < 20:
        if day.weekday() < 5:
            observed = datetime.combine(day, time(15, 30), tzinfo=eastern)
            quotes.append(Quote(
                symbol="AAPL",
                instrument_type="option",
                expiration=date(2026, 12, 18),
                strike=345,
                right=OptionRight.CALL,
                observed_at=observed,
                source_timestamp=observed,
                volume=100,
                source="alpaca:options:indicative",
                metadata={"contract_symbol": "AAPL261218C00345000"},
            ))
            sessions += 1
        day += timedelta(days=1)

    with ResearchStore(database) as store:
        store.save_quotes(quotes)
        store.save_historical_prices((HistoricalPrice(
            symbol="AAPL",
            session_date=date(2026, 11, 3),
            close=270,
        ),))
        store.save_earnings_events((
            EarningsEvent(symbol="AAPL", event_date=date(2026, 11, 10), timing="after_close"),
            EarningsEvent(symbol="AAPL", event_date=date(2026, 11, 12), timing=None),
        ))
        store.save_realized_moves((RealizedMove(
            symbol="AAPL",
            event_date=date(2026, 8, 1),
            reference_price=250,
            post_event_price=260,
            realized_move=0.04,
            post_event_date=date(2026, 8, 3),
        ),))

    report = inspect_data_coverage(database)

    [row] = report.symbols
    assert row.symbol == "AAPL"
    assert row.option_snapshot_count == 20
    assert row.option_contract_count == 1
    assert row.option_session_count == 20
    assert row.max_comparable_sessions == 20
    assert row.unusual_activity_baseline_possible is True
    assert row.historical_price_rows == 1
    assert row.earnings_event_rows == 2
    assert row.earnings_events_with_reviewed_timing == 1
    assert row.realized_move_rows == 1
    assert row.realized_moves_with_post_event_date == 1


def test_does_not_claim_baseline_when_contract_feed_or_time_bucket_differs(tmp_path):
    database = tmp_path / "coverage.duckdb"
    with ResearchStore(database) as store:
        quotes = []
        for index in range(20):
            observed = datetime(2026, 10, 1, 15, 30, tzinfo=timezone.utc) + timedelta(days=index)
            # Alternating contract identities means no exact contract reaches 20 samples.
            contract = f"AAPL261218C0034500{index % 2}"
            quotes.append(Quote(
                symbol="AAPL",
                instrument_type="option",
                expiration=date(2026, 12, 18),
                strike=345,
                right=OptionRight.CALL,
                observed_at=observed,
                source_timestamp=observed,
                volume=100,
                source="alpaca:options:indicative",
                metadata={"contract_symbol": contract},
            ))
        store.save_quotes(quotes)

    [row] = inspect_data_coverage(database).symbols

    assert row.option_snapshot_count == 20
    assert row.option_contract_count == 2
    assert row.max_comparable_sessions == 10
    assert row.unusual_activity_baseline_possible is False


def test_missing_database_is_reported_without_creating_it(tmp_path):
    database = tmp_path / "missing.duckdb"

    report = inspect_data_coverage(database)

    assert report.database_exists is False
    assert report.symbols == ()
    assert not database.exists()
