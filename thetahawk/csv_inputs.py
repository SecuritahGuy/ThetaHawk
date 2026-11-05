"""Explicit event and outcome inputs for reproducible earnings scans."""

from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

from .domain import EarningsEvent, RealizedMove
from .settings import Settings


class CsvResearchInputs:
    """Read operator-supplied, provenance-bearing research input CSVs.

    Files can be maintained from any appropriately licensed source. The source
    and the timestamp at which the record became known are required columns.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings.from_env()

    def get_earnings_events(
        self, symbols: Sequence[str], start: date, end: date
    ) -> list[EarningsEvent]:
        wanted = {symbol.upper() for symbol in symbols}
        rows = _read_rows(self.settings.earnings_csv_path)
        events = []
        for line, row in rows:
            try:
                symbol = _required(row, "symbol").upper()
                event_date = date.fromisoformat(_required(row, "event_date"))
                observed_at = _parse_aware_datetime(_required(row, "observed_at"))
                source = _required(row, "source")
            except (KeyError, ValueError) as exc:
                raise ValueError(f"Invalid earnings CSV row {line}: {exc}") from exc
            if symbol not in wanted or not start <= event_date <= end:
                continue
            expiration_raw = (row.get("expiration_date") or "").strip()
            try:
                expiration_date = date.fromisoformat(expiration_raw) if expiration_raw else None
            except ValueError as exc:
                raise ValueError(f"Invalid earnings CSV row {line}: invalid expiration_date") from exc
            events.append(EarningsEvent(
                symbol=symbol,
                event_date=event_date,
                timing=(row.get("timing") or "").strip().lower() or None,
                observed_at=observed_at,
                source=source,
                source_timestamp=observed_at,
                fiscal_period=(row.get("fiscal_period") or "").strip() or None,
                metadata={
                    "expiration_date": expiration_date.isoformat() if expiration_date else None,
                    "event_id": f"{symbol}:{event_date.isoformat()}:{source}",
                    "input_file": str(self.settings.earnings_csv_path),
                },
            ))
        return events

    def get_realized_moves(self, symbols: Sequence[str]) -> list[RealizedMove]:
        wanted = {symbol.upper() for symbol in symbols}
        rows = _read_rows(self.settings.realized_moves_csv_path)
        moves = []
        for line, row in rows:
            try:
                symbol = _required(row, "symbol").upper()
                event_date = date.fromisoformat(_required(row, "event_date"))
                post_event_date_raw = (row.get("post_event_date") or "").strip()
                post_event_date = (
                    date.fromisoformat(post_event_date_raw) if post_event_date_raw else None
                )
                reference_price = float(_required(row, "reference_price"))
                post_event_price = float(_required(row, "post_event_price"))
                observed_at = _parse_aware_datetime(_required(row, "observed_at"))
                source = _required(row, "source")
            except (KeyError, ValueError) as exc:
                raise ValueError(f"Invalid realized-moves CSV row {line}: {exc}") from exc
            if symbol not in wanted:
                continue
            if reference_price <= 0 or post_event_price <= 0:
                raise ValueError(f"Invalid realized-moves CSV row {line}: prices must be positive")
            moves.append(RealizedMove(
                symbol=symbol,
                event_date=event_date,
                reference_price=reference_price,
                post_event_price=post_event_price,
                realized_move=(post_event_price - reference_price) / reference_price,
                post_event_date=post_event_date,
                source=source,
                observed_at=observed_at,
            ))
        return moves


def _read_rows(path: Path) -> list[tuple[int, dict[str, str]]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        return [(line, {key: value or "" for key, value in row.items() if key})
                for line, row in enumerate(reader, start=2)]


def _required(row: dict[str, str], field: str) -> str:
    value = (row.get(field) or "").strip()
    if not value:
        raise ValueError(f"{field} is required")
    return value


def _parse_aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("observed_at must include a timezone")
    return parsed
