from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from thetahawk.scheduled_collection import (
    RunStatus,
    ScheduledCollectionRunner,
    weekday_regular_session,
)


class FakeResponse:
    status_code = 429


class RateLimitedFailure(Exception):
    response = FakeResponse()


class ScheduledCollectionTests(unittest.TestCase):
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
