"""Read-only local HTTP surface for research candidates.

The API intentionally has no order or account endpoints. Candidate data is
provided by application wiring through ``create_app``; an unconfigured app
returns an empty list rather than fabricating market observations.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder

from .settings import Settings
from .storage import ResearchStore


CandidateLoader = Callable[[], Sequence[Any]]


def load_persisted_candidates() -> Sequence[Mapping[str, Any]]:
    """Load recent local scan records, or return empty until a database exists."""
    settings = Settings.from_env()
    if not Path(settings.database_path).exists():
        return ()
    with ResearchStore(settings.database_path) as store:
        return store.recent_candidates(limit=500)


def _to_payload(candidate: Any) -> dict[str, Any]:
    """Convert domain records to JSON-safe dictionaries without recalculating."""
    if hasattr(candidate, "model_dump"):
        value = candidate.model_dump(mode="json")
    elif hasattr(candidate, "dict") and callable(candidate.dict):
        value = candidate.dict()
    elif is_dataclass(candidate):
        value = asdict(candidate)
    elif isinstance(candidate, Mapping):
        value = dict(candidate)
    else:
        raise TypeError(f"Unsupported candidate record: {type(candidate).__name__}")

    if not isinstance(value, dict):
        raise TypeError("Candidate serialization must produce an object")
    return jsonable_encoder(value)


def create_app(candidate_loader: CandidateLoader | None = None) -> FastAPI:
    """Create the local research API, optionally wired to a candidate source."""
    app = FastAPI(
        title="Thetahawk Research API",
        description="Read-only, research-only market intelligence. No trade execution.",
        version="0.1.0",
    )
    app.state.candidate_loader = candidate_loader

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "research_only": True}

    @app.get("/candidates")
    def candidates(
        request: Request,
        symbol: str | None = Query(default=None, min_length=1, max_length=16),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        loader: CandidateLoader | None = request.app.state.candidate_loader
        if loader is None:
            rows: Sequence[Any] = ()
        else:
            try:
                rows = loader()
            except Exception as exc:
                raise HTTPException(status_code=503, detail="Candidate source unavailable") from exc

        try:
            items = [_to_payload(row) for row in rows]
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=500, detail="Candidate source returned an invalid record") from exc

        if symbol:
            wanted = symbol.strip().upper()
            items = [item for item in items if str(item.get("symbol", "")).upper() == wanted]
        return {
            "research_only": True,
            "as_of": datetime.now().astimezone().isoformat(timespec="seconds"),
            "count": min(len(items), limit),
            "candidates": items[:limit],
        }

    return app


app = create_app(load_persisted_candidates)


def run() -> None:
    """Start the local API on loopback only."""
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
