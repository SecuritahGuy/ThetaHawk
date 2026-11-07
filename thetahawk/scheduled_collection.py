"""Market-session-gated, fixed-delay collection runner.

The CLI uses Alpaca's authenticated read-only market clock. The weekday
predicate remains as a lightweight local/test fallback and does not account
for exchange holidays or early closes.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, time as local_time, timezone
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


@dataclass(frozen=True, slots=True)
class CombinedSnapshotResult:
    options_history: object
    earnings_events: object
    discovery: object | None = None
    earnings_screen: object | None = None

    @property
    def saved_quotes(self) -> int:
        return sum(
            int(getattr(result, "saved_quotes", 0))
            for result in (self.options_history, self.earnings_events)
        )

    @property
    def fetched_quotes(self) -> int:
        return sum(
            int(getattr(result, "fetched_quotes", 0))
            for result in (self.options_history, self.earnings_events)
        )

    @property
    def saved_events(self) -> int:
        return int(getattr(self.earnings_events, "saved_events", 0))

    @property
    def symbols_with_expiration(self) -> int:
        return int(getattr(self.options_history, "symbols_with_expiration", 0))

    @property
    def symbols_without_eligible_expiration(self) -> tuple[str, ...]:
        return tuple(getattr(self.options_history, "symbols_without_eligible_expiration", ()))

    @property
    def eligible_events(self) -> int:
        return int(getattr(self.earnings_events, "eligible_events", 0))

    @property
    def requested_chains(self) -> int:
        return int(getattr(self.earnings_events, "requested_chains", 0))

    @property
    def failed_chains(self) -> tuple[str, ...]:
        return tuple(getattr(self.earnings_events, "failed_chains", ()))

    @property
    def skipped_events(self) -> tuple[str, ...]:
        return tuple(getattr(self.earnings_events, "skipped_events", ()))

    @property
    def selected_pairs(self) -> int | None:
        pairs = getattr(self.discovery, "selected_pairs", None)
        return len(pairs) if pairs is not None else None

    @property
    def discovery_groups(self) -> int | None:
        discoveries = getattr(self.discovery, "discoveries", None)
        return len(discoveries) if discoveries is not None else None

    @property
    def discovery_errors(self) -> int | None:
        errors = getattr(self.discovery, "errors", None)
        return len(errors) if errors is not None else None

    @property
    def earnings_screen_symbols(self) -> int | None:
        count = getattr(self.earnings_screen, "symbol_count", None)
        return int(count) if count is not None else None

    @property
    def earnings_screen_status(self) -> str | None:
        status = getattr(self.earnings_screen, "status", None)
        return str(status) if status is not None else None

    @property
    def earnings_screen_error(self) -> str | None:
        error = getattr(self.earnings_screen, "error", None)
        return str(error) if error is not None else None


def collect_serially(
    options_history_job: Callable[[], object],
    earnings_snapshot_job: Callable[[], object],
    discovery_job: Callable[[], object] | None = None,
    earnings_screen_job: Callable[[], object] | None = None,
) -> CombinedSnapshotResult:
    """Run collectors and optional discovery sequentially to avoid DuckDB writer conflicts."""
    history = options_history_job()
    earnings = earnings_snapshot_job()
    discovery = discovery_job() if discovery_job is not None else None
    earnings_screen = earnings_screen_job() if earnings_screen_job is not None else None
    return CombinedSnapshotResult(history, earnings, discovery, earnings_screen)


@dataclass(frozen=True, slots=True)
class EarningsScreenCycleResult:
    status: str
    symbols: tuple[str, ...] = ()
    error: str | None = None

    @property
    def symbol_count(self) -> int:
        return len(self.symbols)


def reviewed_upcoming_event_symbols(
    settings,
    symbols: tuple[str, ...],
    *,
    as_of: datetime,
    days_ahead: int = 45,
) -> tuple[str, ...]:
    """Return symbols with reviewed, future events and usable expirations."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    if days_ahead < 0:
        raise ValueError("days_ahead cannot be negative")

    from .csv_inputs import CsvResearchInputs

    today = as_of.astimezone(NEW_YORK).date()
    events = CsvResearchInputs(settings).get_earnings_events(
        symbols,
        start=today,
        end=today + timedelta(days=days_ahead),
    )
    event_times = {
        "before_open": local_time(8, 15),
        "after_close": local_time(16, 15),
    }
    eligible: set[str] = set()
    for event in events:
        event_time = event_times.get((event.timing or "").strip().lower())
        if event_time is None:
            continue
        event_at = datetime.combine(event.event_date, event_time, tzinfo=NEW_YORK)
        event_observed_at = event.source_timestamp or event.observed_at
        if event_observed_at > as_of or event_at <= as_of:
            continue
        expiration_raw = event.metadata.get("expiration_date")
        if not expiration_raw:
            continue
        try:
            expiration = date.fromisoformat(str(expiration_raw))
        except ValueError:
            continue
        expiration_at = datetime.combine(expiration, local_time(16, 0), tzinfo=NEW_YORK)
        if expiration_at < event_at:
            continue
        eligible.add(event.symbol.upper())
    return tuple(symbol for symbol in dict.fromkeys(symbols) if symbol.upper() in eligible)


class DailyEarningsScreenJob:
    """Run the earnings screen at most once per local market day when reviewed events exist."""

    def __init__(
        self,
        settings,
        symbols: tuple[str, ...],
        scan: Callable[..., int],
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.symbols = symbols
        self.scan = scan
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.last_successful_day: date | None = None

    def __call__(self) -> EarningsScreenCycleResult:
        now = self.now()
        if now.tzinfo is None or now.utcoffset() is None:
            return EarningsScreenCycleResult(
                "earnings_scan_failed", error="scheduler time must be timezone-aware"
            )
        today = now.astimezone(NEW_YORK).date()
        if self.last_successful_day == today:
            return EarningsScreenCycleResult("already_scanned_today")
        try:
            event_symbols = reviewed_upcoming_event_symbols(
                self.settings, self.symbols, as_of=now
            )
        except Exception as exc:
            return EarningsScreenCycleResult(
                "failed_to_read_reviewed_events",
                error=f"{type(exc).__name__}: {exc}",
            )
        if not event_symbols:
            return EarningsScreenCycleResult("no_reviewed_upcoming_events")
        try:
            result = self.scan(use_alpaca=True, symbols=event_symbols)
        except Exception as exc:
            return EarningsScreenCycleResult(
                "earnings_scan_failed",
                symbols=event_symbols,
                error=f"{type(exc).__name__}: {exc}",
            )
        if result != 0:
            return EarningsScreenCycleResult(
                "earnings_scan_failed",
                symbols=event_symbols,
                error=f"earnings scan returned exit code {result}",
            )
        self.last_successful_day = today
        return EarningsScreenCycleResult("scanned", symbols=event_symbols)


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
        choices=(
            "earnings", "equity", "options", "earnings-options",
            "options-and-earnings", "research-cycle",
        ),
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
    elif args.job in {"options-and-earnings", "research-cycle"}:
        from .earnings_option_snapshots import collect_earnings_option_snapshots
        from .option_snapshots import collect_option_snapshots

        discovery_job = None
        earnings_screen_job = None
        if args.job == "research-cycle":
            from .discover import run_discovery
            from .scan import run_scan

            last_discovery_at: list[datetime | None] = [None]

            def run_hourly_discovery():
                now = datetime.now(timezone.utc)
                previous = last_discovery_at[0]
                if previous is not None and (now - previous).total_seconds() < 3600:
                    return None
                result = run_discovery(symbols=symbols, settings=settings)
                last_discovery_at[0] = now
                return result

            discovery_job = run_hourly_discovery
            earnings_screen_job = DailyEarningsScreenJob(settings, symbols, run_scan)

        job = partial(
            collect_serially,
            partial(
                collect_option_snapshots,
                symbols,
                settings=settings,
                min_dte=args.options_history_min_dte,
                max_dte=args.options_history_max_dte,
                target_dte=args.options_history_max_dte,
                preserve_expiration=True,
            ),
            partial(collect_earnings_option_snapshots, symbols, settings=settings),
            discovery_job,
            earnings_screen_job,
        )
    else:
        from .equity_scan import run_equity_scan

        job = partial(run_equity_scan, symbols=symbols)

    runner = ScheduledCollectionRunner(
        job,
        session_is_open=lambda _now: bool(clock.get_market_clock()["is_open"]),
        interval_seconds=(
            args.interval_seconds
            if args.interval_seconds is not None
            else (
                900 if args.job in {
                    "options", "earnings-options", "options-and-earnings", "research-cycle"
                } else 300
            )
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
                ("saved_events", "saved_events"),
                ("symbols_with_expiration", "symbols_with_expiration"),
                ("eligible_events", "eligible_events"),
                ("requested_chains", "requested_chains"),
                ("selected_pairs", "selected_pairs"),
                ("discovery_groups", "discovery_groups"),
                ("discovery_errors", "discovery_errors"),
                ("earnings_screen_symbols", "earnings_screen_symbols"),
                ("earnings_screen_status", "earnings_screen_status"),
            ):
                if hasattr(value, attribute):
                    field_value = getattr(value, attribute)
                    if field_value is not None:
                        details.append(f"{label}={field_value}")
            for attribute, label in (
                ("failed_chains", "failed_chains"),
                ("skipped_events", "skipped_events"),
                ("symbols_without_eligible_expiration", "symbols_without_expiration"),
            ):
                if hasattr(value, attribute):
                    details.append(f"{label}={len(getattr(value, attribute))}")
        if result.error:
            details.append(f"error={result.error}")
        if value is not None and getattr(value, "earnings_screen_error", None):
            details.append(f"earnings_screen_error={value.earnings_screen_error}")
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
