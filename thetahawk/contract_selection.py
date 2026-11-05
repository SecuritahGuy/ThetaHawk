"""Conservative selection of liquid, near-the-money option pairs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from math import isfinite
from typing import Sequence
from zoneinfo import ZoneInfo

from .domain import OptionRight, Quote


EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class OptionPairSelection:
    """One same-strike call/put pair that passed discovery quality gates."""

    symbol: str
    expiration: date
    strike: float
    dte: int
    moneyness_deviation_fraction: float
    call: Quote
    put: Quote
    call_spread_fraction: float
    put_spread_fraction: float
    combined_midpoint: float


def select_option_pairs(
    options: Sequence[Quote],
    underlying_quote: Quote,
    as_of: datetime,
    *,
    min_dte: int = 14,
    max_dte: int = 45,
    target_dte: int = 30,
    max_strike_deviation_fraction: float = 0.05,
    max_leg_spread_fraction: float = 0.20,
    min_option_volume: int = 1,
    min_open_interest: int = 1,
    max_quote_age_seconds: int = 900,
    max_open_interest_age_days: int = 5,
    max_pairs: int = 10,
) -> list[OptionPairSelection]:
    """Filter and rank bounded same-strike option pairs for one underlying.

    The result is a research input shortlist, not a trade recommendation. A
    pair must have fresh call and put quotes, non-crossed markets, acceptable
    spreads, volume and open interest, and a fresh open-interest date.
    Results sort by distance from target DTE, then moneyness, expiration, and
    strike so repeated runs with the same inputs are deterministic.
    """
    _validate_policy(
        min_dte=min_dte,
        max_dte=max_dte,
        target_dte=target_dte,
        max_strike_deviation_fraction=max_strike_deviation_fraction,
        max_leg_spread_fraction=max_leg_spread_fraction,
        max_quote_age_seconds=max_quote_age_seconds,
        max_open_interest_age_days=max_open_interest_age_days,
        max_pairs=max_pairs,
    )
    if not _aware(as_of):
        raise ValueError("as_of must be timezone-aware")
    if min_option_volume < 1 or min_open_interest < 1:
        raise ValueError("minimum option volume and open interest must be positive")
    symbol = underlying_quote.symbol.strip().upper()
    underlying_time = _effective_time(underlying_quote)
    spot = underlying_quote.midpoint
    if (
        underlying_quote.instrument_type.lower() != "equity"
        or not symbol
        or not _aware(underlying_time)
        or not _fresh(underlying_time, as_of, max_quote_age_seconds)
        or not _valid_market(underlying_quote)
        or spot is None
        or not isfinite(spot)
        or spot <= 0
    ):
        return []

    today = as_of.astimezone(EASTERN).date()
    legs: dict[tuple[date, float], dict[OptionRight, Quote]] = {}
    for quote in options:
        if not _eligible_leg(
            quote,
            symbol=symbol,
            spot=spot,
            today=today,
            as_of=as_of,
            min_dte=min_dte,
            max_dte=max_dte,
            max_strike_deviation_fraction=max_strike_deviation_fraction,
            max_leg_spread_fraction=max_leg_spread_fraction,
            min_option_volume=min_option_volume,
            min_open_interest=min_open_interest,
            max_quote_age_seconds=max_quote_age_seconds,
            max_open_interest_age_days=max_open_interest_age_days,
        ):
            continue
        assert quote.expiration is not None and quote.strike is not None
        pair_legs = legs.setdefault((quote.expiration, quote.strike), {})
        existing = pair_legs.get(quote.right)
        if existing is None or _effective_time(quote) > _effective_time(existing):
            pair_legs[quote.right] = quote

    results: list[OptionPairSelection] = []
    for (expiration, strike), pair_legs in legs.items():
        call = pair_legs.get(OptionRight.CALL)
        put = pair_legs.get(OptionRight.PUT)
        if call is None or put is None or call.source != put.source:
            continue
        call_spread = _spread_fraction(call)
        put_spread = _spread_fraction(put)
        # Both legs must be drawn from the same observed snapshot. Small
        # differences are expected for sequentially serialized records.
        if abs((call.observed_at - put.observed_at).total_seconds()) > max_quote_age_seconds:
            continue
        call_mid = call.midpoint
        put_mid = put.midpoint
        if call_mid is None or put_mid is None:
            continue
        dte = (expiration - today).days
        results.append(OptionPairSelection(
            symbol=symbol,
            expiration=expiration,
            strike=strike,
            dte=dte,
            moneyness_deviation_fraction=abs(strike - spot) / spot,
            call=call,
            put=put,
            call_spread_fraction=call_spread,
            put_spread_fraction=put_spread,
            combined_midpoint=call_mid + put_mid,
        ))

    results.sort(key=lambda pair: (
        abs(pair.dte - target_dte),
        pair.moneyness_deviation_fraction,
        pair.expiration,
        pair.strike,
    ))
    return results[:max_pairs]


def _eligible_leg(
    quote: Quote,
    *,
    symbol: str,
    spot: float,
    today: date,
    as_of: datetime,
    min_dte: int,
    max_dte: int,
    max_strike_deviation_fraction: float,
    max_leg_spread_fraction: float,
    min_option_volume: int,
    min_open_interest: int,
    max_quote_age_seconds: int,
    max_open_interest_age_days: int,
) -> bool:
    if (
        quote.instrument_type.lower() != "option"
        or quote.symbol.strip().upper() != symbol
        or not _aware(quote.observed_at)
        or quote.right not in {OptionRight.CALL, OptionRight.PUT}
        or quote.expiration is None
        or quote.strike is None
        or not isfinite(quote.strike)
        or quote.strike <= 0
        or quote.expiration.weekday() > 4
        or not min_dte <= (quote.expiration - today).days <= max_dte
        or abs(quote.strike - spot) / spot > max_strike_deviation_fraction
        or not _valid_market(quote)
        or _spread_fraction(quote) > max_leg_spread_fraction
        or quote.volume is None
        or quote.volume < min_option_volume
        or quote.open_interest is None
        or quote.open_interest < min_open_interest
        or not quote.source
        or quote.source == "unknown"
    ):
        return False

    source_time = _effective_time(quote)
    if not _aware(source_time) or not _fresh(source_time, as_of, max_quote_age_seconds):
        return False
    oi_date = _open_interest_date(quote)
    if oi_date is None or oi_date > as_of.astimezone(EASTERN).date():
        return False
    if (as_of.astimezone(EASTERN).date() - oi_date).days > max_open_interest_age_days:
        return False
    return True


def _validate_policy(
    *,
    min_dte: int,
    max_dte: int,
    target_dte: int,
    max_strike_deviation_fraction: float,
    max_leg_spread_fraction: float,
    max_quote_age_seconds: int,
    max_open_interest_age_days: int,
    max_pairs: int,
) -> None:
    if min_dte < 0 or max_dte < min_dte or not min_dte <= target_dte <= max_dte:
        raise ValueError("DTE policy must satisfy 0 <= min_dte <= target_dte <= max_dte")
    if not 0 <= max_strike_deviation_fraction <= 1:
        raise ValueError("max_strike_deviation_fraction must be between 0 and 1")
    if not 0 <= max_leg_spread_fraction <= 1:
        raise ValueError("max_leg_spread_fraction must be between 0 and 1")
    if max_quote_age_seconds < 0 or max_open_interest_age_days < 0:
        raise ValueError("age limits cannot be negative")
    if max_pairs < 1:
        raise ValueError("max_pairs must be positive")


def _valid_market(quote: Quote) -> bool:
    return (
        quote.bid is not None
        and quote.ask is not None
        and isfinite(quote.bid)
        and isfinite(quote.ask)
        and quote.bid >= 0
        and quote.ask >= quote.bid
    )


def _spread_fraction(quote: Quote) -> float:
    midpoint = quote.midpoint
    if midpoint is None or midpoint <= 0 or quote.bid is None or quote.ask is None:
        return float("inf")
    return (quote.ask - quote.bid) / midpoint


def _effective_time(quote: Quote) -> datetime:
    return quote.source_timestamp or quote.observed_at


def _fresh(timestamp: datetime, as_of: datetime, max_age_seconds: int) -> bool:
    age = (as_of - timestamp).total_seconds()
    return 0 <= age <= max_age_seconds


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _open_interest_date(quote: Quote) -> date | None:
    value = quote.metadata.get("open_interest_date")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
