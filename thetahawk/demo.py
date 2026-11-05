"""Create one explicitly fictional earnings-screen case for a local demo.

Run with ``python -m thetahawk.demo``. The generated candidate is useful for
reviewing the UI and data path only; it is not a live market observation.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from .domain import EarningsEvent, OptionRight, Quote, RealizedMove
from .scanners import EarningsImpliedMoveScanner, EarningsScanInput
from .settings import Settings
from .storage import ResearchStore


def run_demo_scan() -> int:
    settings = Settings.from_env()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    event_at = now + timedelta(days=7)
    expiration_at = now + timedelta(days=10)
    symbol = "DEMO"
    source = "demo-fictional"

    call = Quote(
        symbol=symbol,
        instrument_type="option",
        observed_at=now,
        source_timestamp=now,
        source=source,
        bid=1.95,
        ask=2.05,
        volume=250,
        open_interest=1200,
        expiration=expiration_at.date(),
        strike=100.0,
        right=OptionRight.CALL,
        metadata={"fictional": True, "open_interest_date": now.date().isoformat()},
    )
    put = replace(call, right=OptionRight.PUT)
    underlying = Quote(
        symbol=symbol,
        instrument_type="equity",
        observed_at=now,
        source_timestamp=now,
        bid=99.9,
        ask=100.1,
        last=100.0,
        volume=1_000_000,
        source=source,
        metadata={"fictional": True},
    )
    history = tuple(
        RealizedMove(
            symbol=symbol,
            event_date=(now - timedelta(days=30 * (index + 1))).date(),
            reference_price=100.0,
            post_event_price=100.0 * (1 + move),
            realized_move=move,
            source=source,
            observed_at=now,
        )
        for index, move in enumerate((0.06, -0.08, 0.09, -0.10, 0.07))
    )
    event = EarningsEvent(
        symbol=symbol,
        event_date=event_at.date(),
        timing="after_close",
        observed_at=now,
        source=source,
        metadata={"fictional": True},
    )
    candidate = EarningsImpliedMoveScanner().scan(
        EarningsScanInput(
            symbol=symbol,
            as_of=now,
            underlying_quote=underlying,
            event_observed_at=event.observed_at,
            event_source=event.source,
            event_id=str(event.metadata["event_id"]),
            event_at=event_at,
            expiration_at=expiration_at,
            call=call,
            put=put,
            historical_realized_moves=history,
        )
    )
    candidate = replace(
        candidate,
        summary="FICTIONAL DEMO CASE — " + candidate.summary,
        risks=candidate.risks + ("All prices, events, and history in this record are fictional.",),
        metrics={**candidate.metrics, "data_mode": "fictional_demo"},
    )

    with ResearchStore(settings.database_path) as store:
        store.save_quotes((call, put))
        store.save_earnings_events((event,))
        store.save_candidates((candidate,))
    print(f"Saved fictional demo candidate {candidate.candidate_id} to {settings.database_path}")
    print("This record is synthetic and must not be treated as a market signal.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_demo_scan())
