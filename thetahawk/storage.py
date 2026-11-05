"""DuckDB-backed local persistence for quotes, events, and scan candidates."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .domain import Candidate, EarningsEvent, HistoricalPrice, Quote, RealizedMove, record_payload


class ResearchStore:
    """Small local store; DuckDB is imported lazily for demo-friendly imports."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import duckdb
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError("DuckDB is required for persistence; install project dependencies") from exc
        self._connection = duckdb.connect(str(self.database_path))
        self._initialize()

    def _initialize(self) -> None:
        self._connection.execute("""
            CREATE TABLE IF NOT EXISTS quotes (
                symbol VARCHAR NOT NULL,
                instrument_type VARCHAR NOT NULL,
                observed_at TIMESTAMPTZ NOT NULL,
                source VARCHAR NOT NULL,
                payload JSON NOT NULL
            )
        """)
        self._connection.execute("""
            CREATE TABLE IF NOT EXISTS earnings_events (
                symbol VARCHAR NOT NULL,
                event_date DATE NOT NULL,
                observed_at TIMESTAMPTZ NOT NULL,
                source VARCHAR NOT NULL,
                payload JSON NOT NULL,
                PRIMARY KEY (symbol, event_date, observed_at, source)
            )
        """)
        self._connection.execute("""
            CREATE TABLE IF NOT EXISTS candidates (
                candidate_id VARCHAR PRIMARY KEY,
                symbol VARCHAR NOT NULL,
                scanner VARCHAR NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                score DOUBLE,
                status VARCHAR NOT NULL,
                payload JSON NOT NULL
            )
        """)
        self._connection.execute("""
            CREATE TABLE IF NOT EXISTS realized_moves (
                symbol VARCHAR NOT NULL,
                event_date DATE NOT NULL,
                observed_at TIMESTAMPTZ NOT NULL,
                source VARCHAR NOT NULL,
                payload JSON NOT NULL,
                PRIMARY KEY (symbol, event_date, observed_at, source)
            )
        """)
        self._connection.execute("""
            CREATE TABLE IF NOT EXISTS historical_prices (
                symbol VARCHAR NOT NULL,
                session_date DATE NOT NULL,
                source VARCHAR NOT NULL,
                observed_at TIMESTAMPTZ NOT NULL,
                payload JSON NOT NULL,
                PRIMARY KEY (symbol, session_date, source)
            )
        """)

    def save_quotes(self, quotes: Iterable[Quote]) -> int:
        rows = [
            (q.symbol, q.instrument_type, q.observed_at, q.source, json.dumps(record_payload(q)))
            for q in quotes
        ]
        if rows:
            self._connection.executemany("INSERT INTO quotes VALUES (?, ?, ?, ?, ?)", rows)
        return len(rows)

    def save_earnings_events(self, events: Iterable[EarningsEvent]) -> int:
        rows = [
            (e.symbol, e.event_date, e.observed_at, e.source, json.dumps(record_payload(e)))
            for e in events
        ]
        if rows:
            self._connection.executemany(
                "INSERT OR REPLACE INTO earnings_events VALUES (?, ?, ?, ?, ?)", rows
            )
        return len(rows)

    def save_realized_moves(self, moves: Iterable[RealizedMove]) -> int:
        rows = [
            (move.symbol, move.event_date, move.observed_at, move.source,
             json.dumps(record_payload(move)))
            for move in moves
        ]
        if rows:
            self._connection.executemany(
                "INSERT OR REPLACE INTO realized_moves VALUES (?, ?, ?, ?, ?)", rows
            )
        return len(rows)

    def save_historical_prices(self, prices: Iterable[HistoricalPrice]) -> int:
        rows = [
            (price.symbol, price.session_date, price.source, price.observed_at,
             json.dumps(record_payload(price)))
            for price in prices
        ]
        if rows:
            self._connection.executemany(
                "INSERT OR REPLACE INTO historical_prices VALUES (?, ?, ?, ?, ?)", rows
            )
        return len(rows)

    def save_candidates(self, candidates: Iterable[Candidate]) -> int:
        rows = [
            (c.candidate_id, c.symbol, c.scanner, c.created_at, c.score,
             c.status.value, json.dumps(record_payload(c)))
            for c in candidates
        ]
        if rows:
            self._connection.executemany(
                "INSERT OR REPLACE INTO candidates VALUES (?, ?, ?, ?, ?, ?, ?)", rows
            )
        return len(rows)

    def recent_candidates(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT payload FROM candidates ORDER BY created_at DESC LIMIT ?", [max(0, limit)]
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "ResearchStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
