"""Market-session-gated, fixed-delay collection runner.

The CLI uses Alpaca's authenticated read-only market clock. The weekday
predicate remains as a lightweight local/test fallback and does not account
for exchange holidays or early closes.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from datetime import datetime, time as local_time, timezone
from enum import StrEnum
from functools import partial
from threading import Event
from typing import Callable
from zoneinfo import ZoneInfo

import requests


NEW_YORK = ZoneInfo("America/New_York")


class RunStatus(StrEnum):
    COMPLETED = "completed"
    SKIPPED_CLOSED = "skipped_closed"
    FAILED_RETRYABLE = "failed_retryable"


@dataclass(frozen=True, slots=True)
class RunResult:
    status: RunStatus
    attempts: int
    value: object | None = None
    error: str | None = None


class RateLimitError(RuntimeError):
    """Provider signals that the request should be retried after a delay."""


def weekday_regular_session(now: datetime) -> bool:
    """Basic US regular-hours gate; inject a holiday-aware predicate in use."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("calendar time must be timezone-aware")
    local = now.astimezone(NEW_YORK)
    return (
        local.weekday() < 5
        and local_time(9, 30) <= local.timetz().replace(tzinfo=None) < local_time(16, 0)
    )


def _is_rate_limited(error: BaseException) -> bool:
    if isinstance(error, RateLimitError):
        return True
    response = getattr(error, "response", None)
    return getattr(response, "status_code", None) == 429


def _is_transient_request_error(error: BaseException) -> bool:
    return _is_rate_limited(error) or isinstance(
        error, (requests.ConnectionError, requests.Timeout)
    )


class ScheduledCollectionRunner:
    """Run one-shot work at fixed delays while the market session is open.

    `job` is normally a zero-argument wrapper around an existing one-shot
    collection/scan function, e.g. ``lambda: run_equity_scan()``. The calendar,
    clock, sleep function, and retry classifier are injectable for tests and
    deployments with a proper exchange calendar.
    """

    def __init__(
        self,
        job: Callable[[], object],
        *,
        session_is_open: Callable[[datetime], bool],
        interval_seconds: float = 300,
        max_retries: int = 3,
        backoff_base_seconds: float = 5,
        backoff_max_seconds: float = 60,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        is_rate_limited: Callable[[BaseException], bool] = _is_transient_request_error,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if backoff_base_seconds < 0 or backoff_max_seconds < 0:
            raise ValueError("backoff values cannot be negative")
        self.job = job
        self.session_is_open = session_is_open
        self.interval_seconds = interval_seconds
        self.max_retries = max_retries
        self.backoff_base_seconds = backoff_base_seconds
        self.backoff_max_seconds = backoff_max_seconds
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep
        self.is_rate_limited = is_rate_limited

    def run_once(self) -> RunResult:
        if not self._session_is_open_with_backoff():
            return RunResult(RunStatus.SKIPPED_CLOSED, attempts=0)

        for attempt in range(1, self.max_retries + 2):
            try:
                return RunResult(RunStatus.COMPLETED, attempts=attempt, value=self.job())
            except Exception as exc:
                if not self.is_rate_limited(exc):
                    raise
                if attempt > self.max_retries:
                    raise
                delay = min(
                    self.backoff_base_seconds * (2 ** (attempt - 1)),
                    self.backoff_max_seconds,
                )
                self.sleep(delay)
                if not self._session_is_open_with_backoff():
                    return RunResult(RunStatus.SKIPPED_CLOSED, attempts=attempt)
        raise AssertionError("retry loop exited unexpectedly")

    def _session_is_open_with_backoff(self) -> bool:
        for attempt in range(self.max_retries + 1):
            try:
                return self.session_is_open(self.now())
            except Exception as exc:
                if not self.is_rate_limited(exc) or attempt >= self.max_retries:
                    raise
                delay = min(
                    self.backoff_base_seconds * (2 ** attempt),
                    self.backoff_max_seconds,
                )
                self.sleep(delay)
        raise AssertionError("calendar retry loop exited unexpectedly")

    def run_forever(
        self,
        *,
        stop_event: Event | None = None,
        max_iterations: int | None = None,
        on_result: Callable[[RunResult], None] | None = None,
    ) -> int:
        """Run until stopped; fixed-delay waits intentionally drop missed slots."""
        if max_iterations is not None and max_iterations < 0:
            raise ValueError("max_iterations cannot be negative")
        iterations = 0
        while stop_event is None or not stop_event.is_set():
            if max_iterations is not None and iterations >= max_iterations:
                break
            try:
                result = self.run_once()
            except Exception as exc:
                if not self.is_rate_limited(exc):
                    raise
                result = RunResult(
                    RunStatus.FAILED_RETRYABLE,
                    attempts=self.max_retries + 1,
                    error=f"{type(exc).__name__}: {exc}",
                )
            iterations += 1
            if on_result:
                on_result(result)
            if max_iterations is not None and iterations >= max_iterations:
                break
            if stop_event is None:
                self.sleep(self.interval_seconds)
            elif stop_event.wait(self.interval_seconds):
                break
        return iterations


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a read-only Thetahawk scan on a fixed market-session interval"
    )
    parser.add_argument(
        "--job",
        choices=("earnings", "equity", "options", "earnings-options"),
        default="equity",
    )
    parser.add_argument("--symbols", help="Comma-separated symbols; defaults to THETAHAWK_SYMBOLS")
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=None,
        help="Delay between runs (default 900 seconds for option jobs, 300 otherwise)",
    )
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--backoff-base-seconds", type=float, default=5)
    parser.add_argument("--backoff-max-seconds", type=float, default=60)
    parser.add_argument("--options-history-min-dte", type=int, default=14)
    parser.add_argument("--options-history-max-dte", type=int, default=75)
    args = parser.parse_args()

    from .providers import AlpacaMarketDataProvider
    from .settings import Settings

    settings = Settings.from_env()
    clock = AlpacaMarketDataProvider(settings)
    symbols = tuple(
        token.strip().upper()
        for token in args.symbols.split(",")
        if token.strip()
    ) if args.symbols else settings.symbols

    if args.job == "earnings":
        from .scan import run_scan

        job = partial(run_scan, use_alpaca=True, symbols=symbols)
    elif args.job == "options":
        from .option_snapshots import collect_option_snapshots

        job = partial(
            collect_option_snapshots,
            symbols,
            settings=settings,
            min_dte=args.options_history_min_dte,
            max_dte=args.options_history_max_dte,
            target_dte=args.options_history_max_dte,
            preserve_expiration=True,
        )
    elif args.job == "earnings-options":
        from .earnings_option_snapshots import collect_earnings_option_snapshots

        job = partial(collect_earnings_option_snapshots, symbols, settings=settings)
    else:
        from .equity_scan import run_equity_scan

        job = partial(run_equity_scan, symbols=symbols)

    runner = ScheduledCollectionRunner(
        job,
        session_is_open=lambda _now: bool(clock.get_market_clock()["is_open"]),
        interval_seconds=(
            args.interval_seconds
            if args.interval_seconds is not None
            else (900 if args.job in {"options", "earnings-options"} else 300)
        ),
        max_retries=args.max_retries,
        backoff_base_seconds=args.backoff_base_seconds,
        backoff_max_seconds=args.backoff_max_seconds,
    )

    def report(result: RunResult) -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        details = []
        value = result.value
        if value is not None:
            for attribute, label in (
                ("saved_quotes", "saved_quotes"),
                ("fetched_quotes", "fetched_quotes"),
                ("symbols_with_expiration", "symbols_with_expiration"),
                ("eligible_events", "eligible_events"),
                ("requested_chains", "requested_chains"),
            ):
                if hasattr(value, attribute):
                    details.append(f"{label}={getattr(value, attribute)}")
            for attribute, label in (
                ("failed_chains", "failed_chains"),
                ("skipped_events", "skipped_events"),
                ("symbols_without_eligible_expiration", "symbols_without_expiration"),
            ):
                if hasattr(value, attribute):
                    details.append(f"{label}={len(getattr(value, attribute))}")
        if result.error:
            details.append(f"error={result.error}")
        suffix = f"; {'; '.join(details)}" if details else ""
        print(
            f"{timestamp} scheduled scan {result.status.value} "
            f"after {result.attempts} attempt(s){suffix}.",
            flush=True,
        )

    try:
        runner.run_forever(on_result=report)
    except KeyboardInterrupt:
        print("Scheduled scan stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
