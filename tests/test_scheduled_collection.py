from __future__ import annotations

import unittest
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from thetahawk.scheduled_collection import (
    CombinedSnapshotResult,
    DailyEarningsScreenJob,
    RunStatus,
    ScheduledCollectionRunner,
    collect_serially,
    reviewed_upcoming_event_symbols,
    weekday_regular_session,
)
from thetahawk.settings import Settings


class FakeResponse:
    status_code = 429


class RateLimitedFailure(Exception):
    response = FakeResponse()


class ScheduledCollectionTests(unittest.TestCase):
    def test_option_collectors_run_serially_and_aggregate_summary(self) -> None:
        calls = []

        class HistoryResult:
            fetched_quotes = 10
            saved_quotes = 9
            symbols_with_expiration = 2
            symbols_without_eligible_expiration = ("NOOPT",)

        class EarningsResult:
            fetched_quotes = 4
            saved_quotes = 4
            saved_events = 1
            eligible_events = 1
            requested_chains = 1
            failed_chains = ()
            skipped_events = ("unreviewed",)

        class DiscoveryResult:
            selected_pairs = ("call-put-pair",)
            discoveries = ("ranked-group", "second-group")
            errors = ()

        class EarningsScreenResult:
            status = "scanned"
            symbols = ("NVDA",)
            error = None

            @property
            def symbol_count(self):
                return len(self.symbols)

        result = collect_serially(
            lambda: calls.append("history") or HistoryResult(),
            lambda: calls.append("earnings") or EarningsResult(),
            lambda: calls.append("discovery") or DiscoveryResult(),
            lambda: calls.append("earnings-screen") or EarningsScreenResult(),
        )

        self.assertIsInstance(result, CombinedSnapshotResult)
        self.assertEqual(calls, ["history", "earnings", "discovery", "earnings-screen"])
        self.assertEqual(result.saved_quotes, 13)
        self.assertEqual(result.fetched_quotes, 14)
        self.assertEqual(result.saved_events, 1)
        self.assertEqual(result.symbols_with_expiration, 2)
        self.assertEqual(result.symbols_without_eligible_expiration, ("NOOPT",))
        self.assertEqual(result.eligible_events, 1)
        self.assertEqual(result.requested_chains, 1)
        self.assertEqual(result.failed_chains, ())
        self.assertEqual(result.skipped_events, ("unreviewed",))
        self.assertEqual(result.selected_pairs, 1)
        self.assertEqual(result.discovery_groups, 2)
        self.assertEqual(result.discovery_errors, 0)
        self.assertEqual(result.earnings_screen_symbols, 1)
        self.assertEqual(result.earnings_screen_status, "scanned")

    def test_reviewed_upcoming_event_filter_requires_timing_and_expiration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "earnings.csv"
            path.write_text(
                "symbol,event_date,timing,expiration_date,observed_at,source\n"
                "NVDA,2026-11-17,after_close,2026-11-20,2026-11-05T12:00:00Z,official\n"
                "AAPL,2026-11-17,,2026-11-20,2026-11-05T12:00:00Z,unreviewed\n"
                "MSFT,2026-11-17,after_close,2026-11-17,2026-11-05T12:00:00Z,too-early-expiry\n"
                "GOOG,2026-11-17,after_close,2026-11-20,2026-11-10T12:00:00Z,not-yet-known\n"
                "AMZN,2026-11-04,after_close,2026-11-20,2026-11-01T12:00:00Z,past-event\n",
                encoding="utf-8",
            )
            settings = Settings(earnings_csv_path=path)

            selected = reviewed_upcoming_event_symbols(
                settings,
                ("NVDA", "AAPL", "MSFT", "GOOG", "AMZN"),
                as_of=datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(selected, ("NVDA",))

    def test_daily_earnings_screen_runs_only_for_reviewed_symbols_once_per_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "earnings.csv"
            event_path.write_text(
                "symbol,event_date,timing,expiration_date,observed_at,source\n"
                "NVDA,2026-11-17,after_close,2026-11-20,2026-11-05T12:00:00Z,official\n"
                "AAPL,2026-11-17,,2026-11-20,2026-11-05T12:00:00Z,unreviewed\n",
                encoding="utf-8",
            )
            settings = Settings(earnings_csv_path=event_path)
            current = [datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)]
            calls = []

            def scan(**kwargs):
                calls.append(kwargs)
                return 0

            job = DailyEarningsScreenJob(
                settings, ("NVDA", "AAPL", "MSFT"), scan, now=lambda: current[0]
            )

            first = job()
            second = job()
            current[0] = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)
            third = job()

        self.assertEqual(first.status, "scanned")
        self.assertEqual(first.symbols, ("NVDA",))
        self.assertEqual(second.status, "already_scanned_today")
        self.assertEqual(third.status, "scanned")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], {"use_alpaca": True, "symbols": ("NVDA",)})

    def test_daily_earnings_screen_reports_scan_failure_and_can_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "earnings.csv"
            event_path.write_text(
                "symbol,event_date,timing,expiration_date,observed_at,source\n"
                "NVDA,2026-11-17,after_close,2026-11-20,2026-11-05T12:00:00Z,official\n",
                encoding="utf-8",
            )
            settings = Settings(earnings_csv_path=event_path)
            job = DailyEarningsScreenJob(
                settings,
                ("NVDA",),
                lambda **_kwargs: (_ for _ in ()).throw(ConnectionError("offline")),
                now=lambda: datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc),
            )

            result = job()

        self.assertEqual(result.status, "earnings_scan_failed")
        self.assertIn("offline", result.error)
        self.assertIsNone(job.last_successful_day)

    def test_market_calendar_rate_limit_uses_bounded_retry(self) -> None:
        calendar_calls = []
        delays = []

        def calendar(_now):
            calendar_calls.append(1)
            if len(calendar_calls) == 1:
                raise RateLimitedFailure()
            return True

        runner = ScheduledCollectionRunner(
            lambda: "ok",
            session_is_open=calendar,
            max_retries=2,
            backoff_base_seconds=3,
            backoff_max_seconds=5,
            sleep=delays.append,
        )

        result = runner.run_once()

        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.value, "ok")
        self.assertEqual(calendar_calls, [1, 1])
        self.assertEqual(delays, [3])

    def test_weekday_regular_hours_predicate(self) -> None:
        def instant(hour: int, minute: int, day: int = 2) -> datetime:
            return datetime(2026, 11, day, hour, minute, tzinfo=timezone.utc)

        # 2026-11-02 is Monday, and UTC-5 in New York after DST ends.
        self.assertFalse(weekday_regular_session(instant(14, 29)))
        self.assertTrue(weekday_regular_session(instant(14, 30)))
        self.assertTrue(weekday_regular_session(instant(20, 59)))
        self.assertFalse(weekday_regular_session(instant(21, 0)))
        self.assertFalse(weekday_regular_session(instant(15, 0, day=7)))

    def test_closed_session_skips_without_invoking_job(self) -> None:
        calls = []
        runner = ScheduledCollectionRunner(
            lambda: calls.append("called"),
            session_is_open=lambda now: False,
            now=lambda: datetime.now(timezone.utc),
        )

        result = runner.run_once()

        self.assertEqual(result.status, RunStatus.SKIPPED_CLOSED)
        self.assertEqual(result.attempts, 0)
        self.assertEqual(calls, [])

    def test_rate_limit_retries_use_bounded_exponential_backoff(self) -> None:
        calls = []
        delays = []

        def job():
            calls.append(1)
            if len(calls) < 4:
                raise RateLimitedFailure()
            return "ok"

        runner = ScheduledCollectionRunner(
            job,
            session_is_open=lambda now: True,
            max_retries=3,
            backoff_base_seconds=2,
            backoff_max_seconds=5,
            sleep=delays.append,
        )

        result = runner.run_once()

        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.attempts, 4)
        self.assertEqual(result.value, "ok")
        self.assertEqual(delays, [2, 4, 5])

    def test_non_rate_limit_exception_is_not_retried(self) -> None:
        calls = []

        def job():
            calls.append(1)
            raise ValueError("bad input")

        runner = ScheduledCollectionRunner(
            job,
            session_is_open=lambda now: True,
            sleep=lambda _: self.fail("unexpected retry sleep"),
        )

        with self.assertRaisesRegex(ValueError, "bad input"):
            runner.run_once()
        self.assertEqual(len(calls), 1)

    def test_connection_failure_retries_then_completes(self) -> None:
        calls = []
        delays = []

        def job():
            calls.append(1)
            if len(calls) == 1:
                raise requests.ConnectionError("temporary disconnect")
            return "ok"

        runner = ScheduledCollectionRunner(
            job,
            session_is_open=lambda _now: True,
            max_retries=2,
            backoff_base_seconds=3,
            sleep=delays.append,
        )

        result = runner.run_once()

        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.value, "ok")
        self.assertEqual(delays, [3])

    def test_run_forever_reports_repeated_transient_failure_and_keeps_running(self) -> None:
        reports = []
        runner = ScheduledCollectionRunner(
            lambda: (_ for _ in ()).throw(requests.Timeout("clock timed out")),
            session_is_open=lambda _now: True,
            max_retries=1,
            backoff_base_seconds=0,
            interval_seconds=0.01,
            sleep=lambda _seconds: None,
        )

        iterations = runner.run_forever(max_iterations=2, on_result=reports.append)

        self.assertEqual(iterations, 2)
        self.assertEqual([row.status for row in reports], [
            RunStatus.FAILED_RETRYABLE,
            RunStatus.FAILED_RETRYABLE,
        ])
        self.assertTrue(all(row.attempts == 2 for row in reports))
        self.assertTrue(all("clock timed out" in row.error for row in reports))

    def test_rechecks_calendar_after_backoff_and_skips_when_session_ends(self) -> None:
        now = [datetime(2026, 11, 2, 20, 59, tzinfo=timezone.utc)]
        calls = []

        def job():
            calls.append(1)
            raise RateLimitedFailure()

        def sleep(seconds: float) -> None:
            now[0] += timedelta(seconds=seconds)

        runner = ScheduledCollectionRunner(
            job,
            session_is_open=lambda current: current < datetime(
                2026, 11, 2, 21, 0, tzinfo=timezone.utc
            ),
            max_retries=3,
            backoff_base_seconds=60,
            backoff_max_seconds=60,
            now=lambda: now[0],
            sleep=sleep,
        )

        result = runner.run_once()

        self.assertEqual(result.status, RunStatus.SKIPPED_CLOSED)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(len(calls), 1)

    def test_fixed_delay_drops_missed_slots_instead_of_catching_up(self) -> None:
        current = [datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc)]
        starts = []

        def job():
            starts.append(current[0])
            current[0] += timedelta(seconds=12)  # Work took longer than interval.

        def sleep(seconds: float) -> None:
            current[0] += timedelta(seconds=seconds)

        runner = ScheduledCollectionRunner(
            job,
            session_is_open=lambda now: True,
            interval_seconds=10,
            now=lambda: current[0],
            sleep=sleep,
        )

        iterations = runner.run_forever(max_iterations=3)

        self.assertEqual(iterations, 3)
        self.assertEqual(
            [int((start - starts[0]).total_seconds()) for start in starts],
            [0, 22, 44],
        )


if __name__ == "__main__":
    unittest.main()
