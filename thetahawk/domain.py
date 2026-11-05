"""Typed records shared by data providers, scanners, storage, and the UI."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any, Mapping


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp for locally observed records."""
    return datetime.now(timezone.utc)


class OptionRight(StrEnum):
    CALL = "call"
    PUT = "put"


class CandidateStatus(StrEnum):
    RESEARCH = "research"
    SUPPRESSED = "suppressed"


@dataclass(frozen=True, slots=True)
class Quote:
    """A point-in-time quote; prices use the provider's stated currency."""

    symbol: str
    observed_at: datetime
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    volume: int | None = None
    open_interest: int | None = None
    instrument_type: str = "equity"
    expiration: date | None = None
    strike: float | None = None
    right: OptionRight | None = None
    implied_volatility: float | None = None
    delta: float | None = None
    source: str = "unknown"
    source_timestamp: datetime | None = None
    currency: str = "USD"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def midpoint(self) -> float | None:
        if self.bid is None or self.ask is None or self.bid < 0 or self.ask < self.bid:
            return None
        return (self.bid + self.ask) / 2

    @property
    def is_stale(self) -> bool:
        """A deliberately conservative basic staleness check (15 minutes)."""
        observed = self.source_timestamp or self.observed_at
        if observed.tzinfo is None:
            return True
        return (utc_now() - observed).total_seconds() > 15 * 60


@dataclass(frozen=True, slots=True)
class EarningsEvent:
    symbol: str
    event_date: date
    timing: str | None = None  # before_open, after_close, or provider-defined
    observed_at: datetime = field(default_factory=utc_now)
    source: str = "unknown"
    source_timestamp: datetime | None = None
    fiscal_period: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Candidate:
    """A screen result with enough provenance to reproduce its analysis."""

    candidate_id: str
    symbol: str
    scanner: str
    score: float | None
    created_at: datetime = field(default_factory=utc_now)
    status: CandidateStatus = CandidateStatus.RESEARCH
    summary: str = ""
    rationale: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    input_ids: tuple[str, ...] = ()
    source_timestamps: tuple[datetime, ...] = ()
    metrics: Mapping[str, Any] = field(default_factory=dict)
    strategy: str | None = None
    max_loss: float | None = None
    invalidation: str | None = None


@dataclass(frozen=True, slots=True)
class RealizedMove:
    symbol: str
    event_date: date
    reference_price: float
    post_event_price: float
    realized_move: float
    source: str = "unknown"
    observed_at: datetime = field(default_factory=utc_now)


def record_payload(record: Any) -> dict[str, Any]:
    """Convert a dataclass to JSON-friendly values without losing enum names."""
    def convert(value: Any) -> Any:
        if isinstance(value, StrEnum):
            return value.value
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, Mapping):
            return {str(k): convert(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [convert(v) for v in value]
        return value

    return convert(asdict(record))
