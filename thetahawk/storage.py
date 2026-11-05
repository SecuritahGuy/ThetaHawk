"""DuckDB-backed local persistence for quotes, events, and scan candidates."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from .domain import Candidate, EarningsEvent, HistoricalPrice, OptionRight, Quote, RealizedMove, record_payload
from .run_manifest import ScanRunManifest


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
        self._connection.execute("""
            CREATE TABLE IF NOT EXISTS option_snapshot_plans (
                symbol VARCHAR PRIMARY KEY,
                expiration_date DATE NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            )
        """)
        self._connection.execute("""
            CREATE TABLE IF NOT EXISTS scan_runs (
                run_id VARCHAR PRIMARY KEY,
                scanner VARCHAR NOT NULL,
                scanner_version VARCHAR NOT NULL,
                policy_version VARCHAR NOT NULL,
                code_version VARCHAR NOT NULL,
                as_of TIMESTAMPTZ NOT NULL,
                candidate_ids JSON NOT NULL,
                payload JSON NOT NULL
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

    def option_snapshot_expiration(self, symbol: str) -> date | None:
        """Return the active stable expiration selected for session history."""
        row = self._connection.execute(
            "SELECT expiration_date FROM option_snapshot_plans WHERE symbol = ?",
            [symbol.strip().upper()],
        ).fetchone()
        return row[0] if row else None

    def save_option_snapshot_expiration(
        self, symbol: str, expiration: date, *, updated_at: datetime
    ) -> None:
        """Pin a stable expiration until it ages out of the collection window."""
        self._connection.execute(
            "INSERT OR REPLACE INTO option_snapshot_plans VALUES (?, ?, ?)",
            [symbol.strip().upper(), expiration, updated_at],
        )

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

    def option_quotes(
        self,
        symbol: str,
        *,
        from_observed_at: datetime | None = None,
        before_observed_at: datetime | None = None,
    ) -> list[Quote]:
        """Load stored option snapshots for scanner baselines and review."""
        predicates = ["symbol = ?", "instrument_type = 'option'"]
        parameters: list[Any] = [symbol.strip().upper()]
        if from_observed_at is not None:
            predicates.append("observed_at >= ?")
            parameters.append(from_observed_at)
        if before_observed_at is not None:
            predicates.append("observed_at < ?")
            parameters.append(before_observed_at)
        rows = self._connection.execute(
            "SELECT payload FROM quotes WHERE " + " AND ".join(predicates)
            + " ORDER BY observed_at",
            parameters,
        ).fetchall()
        return [_quote_from_payload(json.loads(row[0])) for row in rows]

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

    def save_scan_run(self, manifest: ScanRunManifest) -> None:
        """Persist immutable scan provenance and its exact normalized inputs."""
        payload = record_payload(manifest)
        self._connection.execute(
            "INSERT OR REPLACE INTO scan_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                manifest.run_id,
                manifest.scanner,
                manifest.scanner_version,
                manifest.policy_version,
                manifest.code_version,
                manifest.as_of,
                json.dumps(list(manifest.candidate_ids)),
                json.dumps(payload),
            ],
        )

    def scan_runs_for_candidate(self, candidate_id: str) -> list[dict[str, Any]]:
        """Return matching manifests newest first, tolerating legacy schemas."""
        exists = self._connection.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_name = 'scan_runs'"
        ).fetchone()[0]
        if not exists:
            return []
        rows = self._connection.execute(
            "SELECT payload FROM scan_runs ORDER BY as_of DESC"
        ).fetchall()
        return [
            json.loads(row[0]) for row in rows
            if candidate_id in json.loads(row[0]).get("candidate_ids", [])
        ]

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


def _quote_from_payload(payload: dict[str, Any]) -> Quote:
    def parse_datetime(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None

    expiration = payload.get("expiration")
    right = payload.get("right")
    return Quote(
        symbol=payload["symbol"],
        observed_at=parse_datetime(payload["observed_at"]),
        bid=payload.get("bid"),
        ask=payload.get("ask"),
        last=payload.get("last"),
        volume=payload.get("volume"),
        open_interest=payload.get("open_interest"),
        instrument_type=payload.get("instrument_type", "equity"),
        expiration=date.fromisoformat(expiration) if expiration else None,
        strike=payload.get("strike"),
        right=OptionRight(right) if right else None,
        implied_volatility=payload.get("implied_volatility"),
        delta=payload.get("delta"),
        source=payload.get("source", "unknown"),
        source_timestamp=parse_datetime(payload.get("source_timestamp")),
        currency=payload.get("currency", "USD"),
        metadata=payload.get("metadata") or {},
    )
