"""Comparable-baseline screen for unusual volume in listed option contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from math import isfinite, sqrt
from statistics import median
from typing import Sequence
from zoneinfo import ZoneInfo

from .domain import Candidate, CandidateStatus, Quote


EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class UnusualOptionsActivityInput:
    """An option snapshot plus prior same-contract, same-time observations."""

    symbol: str
    as_of: datetime
    current: Quote
    history: Sequence[Quote]
    minimum_history_sessions: int = 20
    time_bucket_minutes: int = 15
    minimum_current_volume: int = 50
    minimum_robust_z_score: float = 3.5
    maximum_quote_age_seconds: int = 900


class UnusualOptionsActivityScanner:
    """Flag unusually high cumulative volume versus a comparable baseline.

    Historical samples must be for the exact same option contract, from the
    same provider/feed, on distinct earlier sessions, and within the same
    intraday time bucket. The robust score is a ranking statistic, not a
    probability or prediction.
    """

    name = "unusual_options_activity"

    def scan(self, item: UnusualOptionsActivityInput) -> Candidate:
        current = item.current
        current_time = current.source_timestamp or current.observed_at
        contract_id = current.metadata.get("contract_symbol")
        reasons: list[str] = []
        metrics: dict[str, float | int | str | None] = {}
        status = CandidateStatus.SUPPRESSED
        score = 0.0
        history_matches: list[Quote] = []

        def result() -> Candidate:
            return Candidate(
                candidate_id=(
                    f"{item.symbol.upper()}:{self.name}:"
                    f"{str(contract_id or 'unknown')}:{item.as_of.date().isoformat()}"
                ),
                symbol=item.symbol.upper(),
                scanner=self.name,
                score=score,
                created_at=item.as_of,
                status=status,
                summary=reasons[0] if reasons else "Candidate suppressed before evaluation.",
                rationale=tuple(reasons),
                risks=(
                    "Unusual volume does not identify trade direction or predict returns.",
                    "Displayed volume may include opening, closing, hedging, or multi-leg activity.",
                    "This screen does not estimate executable price, probability, or strategy P&L.",
                ),
                input_ids=tuple(
                    [str(contract_id)] if contract_id else []
                ) + tuple(
                    f"{row.observed_at.astimezone(EASTERN).date().isoformat()}:{row.volume}"
                    for row in history_matches
                ),
                source_timestamps=tuple(dict.fromkeys(
                    [current_time] + [row.observed_at for row in history_matches]
                )),
                metrics=metrics.copy(),
            )

        if not _aware(item.as_of) or not _aware(current.observed_at) or not _aware(current_time):
            reasons.append("Suppressed: scan and quote timestamps must be timezone-aware.")
            return result()
        if (
            not contract_id
            or current.instrument_type.lower() != "option"
            or current.symbol.upper() != item.symbol.upper()
            or current.expiration is None
            or current.strike is None
            or current.right is None
        ):
            reasons.append("Suppressed: current quote lacks matching option contract identity.")
            return result()
        if not current.source or current.source == "unknown":
            reasons.append("Suppressed: current quote has no provider/feed provenance.")
            return result()
        if current.volume is None or current.volume < 0:
            reasons.append("Suppressed: current cumulative volume is missing or invalid.")
            return result()
        age = (item.as_of - current_time).total_seconds()
        metrics["current_quote_age_seconds"] = round(age, 1)
        if age < 0 or age > item.maximum_quote_age_seconds:
            reasons.append("Suppressed: current option quote is future-dated or stale.")
            return result()
        if item.time_bucket_minutes <= 0 or 60 % item.time_bucket_minutes != 0:
            reasons.append("Suppressed: time bucket must be a positive divisor of 60 minutes.")
            return result()
        current_session = current.observed_at.astimezone(EASTERN).date()
        current_bucket = _time_bucket(current.observed_at, item.time_bucket_minutes)
        metrics["time_bucket_minutes"] = item.time_bucket_minutes
        metrics["current_volume"] = current.volume
        metrics["contract_symbol"] = str(contract_id)
        metrics["quote_source"] = current.source

        same_contract_rows = [
            row for row in item.history
            if row.instrument_type.lower() == "option"
            and row.symbol.upper() == item.symbol.upper()
            and row.metadata.get("contract_symbol") == contract_id
        ]
        if any(row.source != current.source for row in same_contract_rows):
            reasons.append("Suppressed: historical baseline mixes provider or feed sources.")
            return result()

        eligible: dict[date, Quote] = {}
        for row in same_contract_rows:
            if (
                not _aware(row.observed_at)
                or row.observed_at >= current.observed_at
                or row.volume is None
                or row.volume < 0
            ):
                continue
            session = row.observed_at.astimezone(EASTERN).date()
            if session >= current_session:
                continue
            if _time_bucket(row.observed_at, item.time_bucket_minutes) != current_bucket:
                continue
            source_time = row.source_timestamp or row.observed_at
            if not _aware(source_time):
                continue
            historical_age = (row.observed_at - source_time).total_seconds()
            if historical_age < 0 or historical_age > item.maximum_quote_age_seconds:
                continue
            previous = eligible.get(session)
            if previous is None or row.observed_at > previous.observed_at:
                eligible[session] = row

        history_matches = [eligible[key] for key in sorted(eligible)]
        required = max(1, item.minimum_history_sessions)
        metrics["baseline_session_count"] = len(history_matches)
        if len(history_matches) < required:
            reasons.append(
                f"Suppressed: only {len(history_matches)} comparable sessions; {required} required."
            )
            return result()

        baseline = [int(row.volume) for row in history_matches if row.volume is not None]
        baseline_median = float(median(baseline))
        mad = float(median([abs(value - baseline_median) for value in baseline]))
        # A Poisson-like floor handles common zero-volume baselines without
        # treating a flat history as infinite statistical evidence.
        scale = max(mad, sqrt(max(baseline_median, 1.0)))
        robust_z = 0.67448975 * (current.volume - baseline_median) / scale
        ratio = current.volume / max(baseline_median, 1.0)
        metrics["baseline_median_volume"] = baseline_median
        metrics["baseline_mad_volume"] = mad
        metrics["volume_ratio_to_median"] = round(ratio, 3)
        metrics["robust_z_score"] = round(robust_z, 3)
        metrics["current_open_interest"] = current.open_interest
        if history_matches[-1].open_interest is not None and current.open_interest is not None:
            metrics["open_interest_change"] = (
                current.open_interest - history_matches[-1].open_interest
            )
        reasons.append(
            f"Contract volume is {current.volume:,}, {ratio:.1f}× its comparable-session median; "
            f"robust z-score is {robust_z:.2f}."
        )
        if (
            current.volume >= item.minimum_current_volume
            and robust_z >= item.minimum_robust_z_score
        ):
            status = CandidateStatus.RESEARCH
            score = min(100.0, robust_z * 10)
            reasons.append("Volume exceeds configured size and baseline thresholds; review contract context.")
        else:
            reasons.append("Volume does not exceed both configured thresholds.")
        return result()


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _time_bucket(value: datetime, bucket_minutes: int) -> int:
    local = value.astimezone(EASTERN)
    return local.hour * 60 + (local.minute // bucket_minutes) * bucket_minutes

