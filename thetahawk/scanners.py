"""Deterministic, research-only opportunity screens.

Calculations and filters live here; language models are not used to produce
numbers, forecasts, or trading instructions. Providers should normalize into
the shared records in :mod:`thetahawk.domain` before invoking a screen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from math import isfinite
from statistics import median
from typing import Protocol, Sequence

from .domain import Candidate, CandidateStatus, HistoricalPrice, OptionRight, Quote, RealizedMove


@dataclass(frozen=True, slots=True)
class EarningsScanInput:
    """Inputs for an earnings implied-move comparison.

    `historical_realized_moves` contains comparable past event records. The
    scanner filters out events on/after the upcoming event and records not
    observed by `as_of`; callers remain responsible for event comparability.
    """

    symbol: str
    as_of: datetime
    underlying_quote: Quote
    event_observed_at: datetime
    event_source: str
    event_id: str
    event_at: datetime
    expiration_at: datetime
    call: Quote
    put: Quote
    historical_realized_moves: Sequence[RealizedMove]
    min_history: int = 4


@dataclass(frozen=True, slots=True)
class OverextendedEquityInput:
    """Current equity quote and chronological, sourced daily closing prices."""

    symbol: str
    as_of: datetime
    quote: Quote
    historical_prices: Sequence[HistoricalPrice]
    window_sessions: int = 20
    min_history_prices: int = 60
    minimum_move_fraction: float = 0.05
    minimum_robust_z_score: float = 3.5


@dataclass(frozen=True, slots=True)
class ScreenPolicy:
    """Conservative input-quality policy; tune per provider and universe."""

    max_quote_age_seconds: int = 900
    max_leg_spread_fraction: float = 0.20
    max_straddle_fraction_of_underlying: float = 0.50
    min_option_volume: int = 1
    min_open_interest: int = 1
    min_history: int = 4
    min_divergence_fraction: float = 0.10
    max_strike_deviation_fraction: float = 0.05
    max_open_interest_age_days: int = 5


class Scanner(Protocol):
    """Common shape for deterministic scanner implementations."""

    @property
    def name(self) -> str: ...

    def scan(self, item: object) -> Candidate: ...


class EarningsImpliedMoveScanner:
    """Compare an ATM straddle's approximate implied move with event history.

    Implied move is (call midpoint + put midpoint) / underlying price. It is a
    rough, expiration-specific market-implied magnitude, not a precise
    probability forecast. A lower implied move than historical median creates
    a research candidate; the comparison does not infer price direction.
    """

    name = "earnings_implied_move"

    def __init__(self, policy: ScreenPolicy | None = None) -> None:
        self.policy = policy or ScreenPolicy()

    def scan(self, item: EarningsScanInput) -> Candidate:
        reasons: list[str] = []
        risks = (
            "Historical event moves do not establish future returns.",
            "A realized move can be smaller than the premium-implied move.",
            "This comparison gives no price direction or contract-level P&L.",
        )
        metrics: dict[str, float | int | str | None] = {}
        status = CandidateStatus.SUPPRESSED

        def result(score: float | None = 0.0) -> Candidate:
            return Candidate(
                candidate_id=f"{item.symbol.upper()}:{self.name}:{item.event_at.date().isoformat()}",
                symbol=item.symbol.upper(),
                scanner=self.name,
                score=score,
                created_at=item.as_of,
                status=status,
                summary=reasons[0] if reasons else "Candidate suppressed before evaluation.",
                rationale=tuple(reasons),
                risks=risks,
                input_ids=tuple(
                    [item.event_id]
                    + [
                        str(quote.metadata["contract_symbol"])
                        for quote in (item.call, item.put)
                        if quote.metadata.get("contract_symbol")
                    ]
                ),
                source_timestamps=(
                    item.event_observed_at,
                    item.underlying_quote.source_timestamp or item.underlying_quote.observed_at,
                    item.call.source_timestamp or item.call.observed_at,
                    item.put.source_timestamp or item.put.observed_at,
                ),
                metrics=metrics.copy(),
            )

        underlying_price = item.underlying_quote.midpoint
        timestamps = (
            item.as_of,
            item.event_observed_at,
            item.event_at,
            item.expiration_at,
            item.underlying_quote.source_timestamp or item.underlying_quote.observed_at,
            item.call.source_timestamp or item.call.observed_at,
            item.put.source_timestamp or item.put.observed_at,
        )
        if not all(_is_aware(value) for value in timestamps):
            reasons.append("Suppressed: all timestamps must be timezone-aware.")
            return result()
        if item.event_observed_at > item.as_of:
            reasons.append("Suppressed: earnings event record was observed after the scan time.")
            return result()
        if (
            item.underlying_quote.instrument_type.lower() != "equity"
            or item.underlying_quote.symbol.upper() != item.symbol.upper()
            or not _valid_quote(item.underlying_quote)
        ):
            reasons.append("Suppressed: underlying quote is missing, invalid, or for another symbol.")
            return result()
        if underlying_price is None or not isfinite(underlying_price) or underlying_price <= 0:
            reasons.append("Suppressed: underlying price must be positive and finite.")
            return result()
        if item.event_at <= item.as_of:
            reasons.append("Suppressed: earnings event is not in the future at scan time.")
            return result()
        if item.expiration_at < item.event_at:
            reasons.append("Suppressed: option expiration precedes the earnings event.")
            return result()

        quote_age = max(
            (item.as_of - (item.underlying_quote.source_timestamp or item.underlying_quote.observed_at)).total_seconds(),
            (item.as_of - (item.call.source_timestamp or item.call.observed_at)).total_seconds(),
            (item.as_of - (item.put.source_timestamp or item.put.observed_at)).total_seconds(),
        )
        metrics["quote_age_seconds"] = round(quote_age, 1)
        if quote_age < 0 or quote_age > self.policy.max_quote_age_seconds:
            reasons.append("Suppressed: one or more underlying/option quotes are future-dated or stale.")
            return result()
        metrics["underlying_quote_source"] = item.underlying_quote.source
        metrics["call_quote_source"] = item.call.source
        metrics["put_quote_source"] = item.put.source
        metrics["event_source"] = item.event_source

        mids: dict[str, float] = {}
        for label, quote, expected_right in (
            ("call", item.call, OptionRight.CALL),
            ("put", item.put, OptionRight.PUT),
        ):
            if not _valid_quote(quote):
                reasons.append(f"Suppressed: {label} quote has invalid or crossed markets.")
                return result()
            if quote.instrument_type.lower() != "option" or quote.right != expected_right:
                reasons.append(f"Suppressed: {label} input is not the expected option contract.")
                return result()
            if quote.symbol.upper() != item.symbol.upper():
                reasons.append(f"Suppressed: {label} contract symbol does not match the underlying.")
                return result()
            if quote.expiration != item.expiration_at.date():
                reasons.append("Suppressed: option legs do not match the selected expiration.")
                return result()
            if quote.strike is None or not isfinite(quote.strike) or quote.strike <= 0:
                reasons.append(f"Suppressed: {label} strike is missing or invalid.")
                return result()
            strike_deviation = abs(quote.strike - underlying_price) / underlying_price
            metrics[f"{label}_strike_deviation_fraction"] = round(strike_deviation, 6)
            if strike_deviation > self.policy.max_strike_deviation_fraction:
                reasons.append(f"Suppressed: {label} strike is not sufficiently near the money.")
                return result()
            assert quote.bid is not None and quote.ask is not None
            mid = (quote.bid + quote.ask) / 2
            spread_fraction = (quote.ask - quote.bid) / mid if mid > 0 else float("inf")
            metrics[f"{label}_mid"] = mid
            metrics[f"{label}_spread_fraction"] = round(spread_fraction, 6)
            if spread_fraction > self.policy.max_leg_spread_fraction:
                reasons.append(f"Suppressed: {label} bid/ask spread exceeds policy.")
                return result()
            if quote.volume is None or quote.volume < self.policy.min_option_volume:
                reasons.append(f"Suppressed: {label} volume is missing or below policy.")
                return result()
            if quote.open_interest is None or quote.open_interest < self.policy.min_open_interest:
                reasons.append(f"Suppressed: {label} open interest is missing or below policy.")
                return result()
            oi_date = quote.metadata.get("open_interest_date")
            if isinstance(oi_date, datetime):
                oi_date = oi_date.date()
            elif not isinstance(oi_date, date):
                try:
                    oi_date = date.fromisoformat(str(oi_date)) if oi_date else None
                except ValueError:
                    oi_date = None
            if oi_date is None or oi_date > item.as_of.date() or (
                item.as_of.date() - oi_date
            ).days > self.policy.max_open_interest_age_days:
                reasons.append(f"Suppressed: {label} open interest date is missing or stale.")
                return result()
            mids[label] = mid

        if item.call.strike != item.put.strike:
            reasons.append("Suppressed: call and put do not share the same straddle strike.")
            return result()

        implied = (mids["call"] + mids["put"]) / underlying_price
        metrics["underlying_price"] = underlying_price
        metrics["straddle_mid"] = mids["call"] + mids["put"]
        metrics["implied_move_fraction"] = round(implied, 6)
        if implied <= 0 or implied > self.policy.max_straddle_fraction_of_underlying:
            reasons.append("Suppressed: straddle premium is outside configured bounds.")
            return result()

        history = [
            abs(record.realized_move)
            for record in item.historical_realized_moves
            if record.symbol.upper() == item.symbol.upper()
            and record.event_date < item.event_at.date()
            and _is_aware(record.observed_at)
            and record.observed_at <= item.as_of
            and isfinite(record.realized_move)
            and abs(record.realized_move) <= 5
        ]
        metrics["history_sample_count"] = len(history)
        required = max(self.policy.min_history, item.min_history)
        if len(history) < required:
            reasons.append(f"Suppressed: only {len(history)} usable historical events; {required} required.")
            return result()

        historical_median = median(history)
        divergence_fraction = (
            (historical_median - implied) / historical_median
            if historical_median > 0 else 0.0
        )
        metrics["historical_median_move_fraction"] = round(historical_median, 6)
        metrics["implied_minus_historical_fraction"] = round(implied - historical_median, 6)
        metrics["historical_excess_vs_implied_fraction"] = round(divergence_fraction, 6)
        metrics["expiration_days_after_event"] = round(
            (item.expiration_at - item.event_at).total_seconds() / 86400, 2
        )

        reasons.append(
            f"ATM straddle midpoint implies a {implied:.1%} move through expiration; "
            f"the median of {len(history)} historical earnings moves is {historical_median:.1%}."
        )
        if divergence_fraction >= self.policy.min_divergence_fraction:
            status = CandidateStatus.RESEARCH
            reasons.append(
                f"Historical median exceeds implied move by {divergence_fraction:.1%}; "
                "review event comparability and executable prices."
            )
            # Bounded 0–100 ranking value for sorting only, not a probability.
            score = min(100.0, max(0.0, divergence_fraction * 100))
        else:
            status = CandidateStatus.SUPPRESSED
            reasons.append("No material historical-versus-implied divergence under current policy.")
            score = 0.0
        return result(round(score, 2))


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _valid_quote(quote: Quote) -> bool:
    return (
        quote.bid is not None
        and quote.ask is not None
        and isfinite(quote.bid)
        and isfinite(quote.ask)
        and quote.bid >= 0
        and quote.ask >= quote.bid
    )


class UnusualOptionsActivityScanner:
    """Named extension point; activate after historical baselines are wired."""

    name = "unusual_options_activity"

    def scan(self, item: object) -> Candidate:
        return _not_implemented_candidate(self.name, item)


class OverextendedEquityScanner:
    """Flag large multi-session moves that are unusual versus their own history.

    Uses a median/MAD standardized score over overlapping historical returns,
    which is less sensitive to a few large outliers than mean/std. This screen
    identifies unusual movement only; it does not predict a reversal.
    """

    name = "overextended_equity"

    def scan(self, item: object) -> Candidate:
        if not isinstance(item, OverextendedEquityInput):
            return _not_implemented_candidate(self.name, item)

        reasons: list[str] = []
        risks = (
            "An unusual move does not imply that price will reverse.",
            "Historical closes must use consistent split and dividend adjustments.",
            "A current quote may differ from the official daily close used in the baseline.",
        )
        metrics: dict[str, float | int | str | None] = {}
        status = CandidateStatus.SUPPRESSED
        score = 0.0
        quote_time = item.quote.source_timestamp or item.quote.observed_at

        def result() -> Candidate:
            return Candidate(
                candidate_id=f"{item.symbol.upper()}:{self.name}:{item.as_of.date().isoformat()}",
                symbol=item.symbol.upper(),
                scanner=self.name,
                score=score,
                created_at=item.as_of,
                status=status,
                summary=reasons[0] if reasons else "Candidate suppressed before evaluation.",
                rationale=tuple(reasons),
                risks=risks,
                input_ids=tuple(
                    f"{row.symbol.upper()}:{row.timestamp.isoformat()}"
                    for row in item.historical_prices
                ),
                source_timestamps=(quote_time,) + tuple(
                    row.timestamp for row in item.historical_prices
                ),
                metrics=metrics.copy(),
            )

        if not _is_aware(item.as_of) or not _is_aware(quote_time):
            reasons.append("Suppressed: scan and quote timestamps must be timezone-aware.")
            return result()
        if (
            item.quote.instrument_type.lower() != "equity"
            or item.quote.symbol.upper() != item.symbol.upper()
            or not _valid_quote(item.quote)
            or item.quote.midpoint is None
            or item.quote.midpoint <= 0
        ):
            reasons.append("Suppressed: current equity quote is missing, invalid, or mismatched.")
            return result()
        quote_age = (item.as_of - quote_time).total_seconds()
        metrics["quote_age_seconds"] = round(quote_age, 1)
        if quote_age < 0 or quote_age > 900:
            reasons.append("Suppressed: current equity quote is future-dated or stale.")
            return result()

        rows = [
            row for row in item.historical_prices
            if row.symbol.upper() == item.symbol.upper()
            and _is_aware(row.timestamp)
            and row.timestamp < quote_time
            and row.timestamp <= item.as_of
            and isfinite(row.close)
            and row.close > 0
        ]
        rows.sort(key=lambda row: row.timestamp)
        # Keep only one observation for each UTC session date.
        daily: dict[date, HistoricalPrice] = {}
        for row in rows:
            daily[row.timestamp.astimezone(timezone.utc).date()] = row
        closes = [daily[key] for key in sorted(daily)]
        window = max(2, item.window_sessions)
        required = max(window + 2, item.min_history_prices)
        metrics["history_price_count"] = len(closes)
        metrics["history_source"] = closes[-1].source if closes else None
        metrics["adjusted_price_history"] = (
            all(row.adjusted for row in closes) if closes else None
        )
        if len(closes) < required:
            reasons.append(
                f"Suppressed: only {len(closes)} usable historical closes; {required} required."
            )
            return result()

        price = item.quote.midpoint
        reference = closes[-window].close
        current_move = price / reference - 1
        baseline = [
            closes[index].close / closes[index - window].close - 1
            for index in range(window, len(closes))
        ]
        baseline_median = median(baseline)
        mad = median([abs(value - baseline_median) for value in baseline])
        metrics["window_sessions"] = window
        metrics["current_window_move_fraction"] = round(current_move, 6)
        metrics["baseline_median_move_fraction"] = round(baseline_median, 6)
        metrics["baseline_mad_fraction"] = round(mad, 6)
        if mad <= 0 or not isfinite(mad):
            reasons.append("Suppressed: historical move baseline has no usable dispersion.")
            return result()
        robust_z = 0.67448975 * (current_move - baseline_median) / mad
        metrics["robust_z_score"] = round(robust_z, 3)
        reasons.append(
            f"{window}-session move is {current_move:+.1%}; its historical robust z-score is {robust_z:+.2f}."
        )
        if (
            abs(current_move) >= item.minimum_move_fraction
            and abs(robust_z) >= item.minimum_robust_z_score
        ):
            status = CandidateStatus.RESEARCH
            score = min(100.0, abs(robust_z) * 10)
            reasons.append("Move exceeds configured size and historical-extremeness thresholds; review context.")
        else:
            reasons.append("Move does not exceed both configured thresholds.")
        return result()


class VolatilityDivergenceScanner:
    """Named extension point for IV/RV comparisons with contract checks."""

    name = "volatility_divergence"

    def scan(self, item: object) -> Candidate:
        return _not_implemented_candidate(self.name, item)


class EventCatalystScanner:
    """Named extension point for timestamped catalyst aggregation."""

    name = "event_catalyst"

    def scan(self, item: object) -> Candidate:
        return _not_implemented_candidate(self.name, item)


def _not_implemented_candidate(screen: str, item: object) -> Candidate:
    symbol = getattr(item, "symbol", "UNKNOWN")
    as_of = getattr(item, "as_of", datetime.now(timezone.utc))
    return Candidate(
        candidate_id=f"{str(symbol).upper()}:{screen}:not-evaluated",
        symbol=str(symbol).upper(),
        scanner=screen,
        score=0.0,
        created_at=as_of,
        status=CandidateStatus.SUPPRESSED,
        summary="Screen is not implemented yet.",
        rationale=("No candidate was evaluated.",),
    )
