"""Local, environment-based configuration with safe demo defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _symbols(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(s.strip().upper() for s in value.split(",") if s.strip()))


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path = Path("./data")
    database_path: Path = Path("./data/thetahawk.duckdb")
    symbols: tuple[str, ...] = ("AAPL", "AMZN", "ARM", "GOOG", "MSFT", "NVDA")
    timezone: str = "America/Chicago"
    stale_quote_seconds: int = 900
    demo_mode: bool = True
    mlx_server_url: str = "http://127.0.0.1:8080/v1"
    mlx_model: str = "Qwen/Qwen3-8B-MLX-4bit"

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        data_dir = Path(env.get("THETAHAWK_DATA_DIR", "./data")).expanduser()
        db_path = Path(env.get("THETAHAWK_DATABASE_PATH", str(data_dir / "thetahawk.duckdb"))).expanduser()
        symbols = _symbols(env.get("THETAHAWK_SYMBOLS", "AAPL,AMZN,ARM,GOOG,MSFT,NVDA"))
        try:
            stale = max(1, int(env.get("THETAHAWK_STALE_QUOTE_SECONDS", "900")))
        except ValueError:
            stale = 900
        demo = env.get("THETAHAWK_DEMO_MODE", "true").strip().lower() not in {"0", "false", "no"}
        return cls(
            data_dir=data_dir,
            database_path=db_path,
            symbols=symbols or ("AAPL", "AMZN", "ARM", "GOOG", "MSFT", "NVDA"),
            timezone=env.get("THETAHAWK_TIMEZONE", "America/Chicago"),
            stale_quote_seconds=stale,
            demo_mode=demo,
            mlx_server_url=env.get("THETAHAWK_MLX_SERVER_URL", "http://127.0.0.1:8080/v1"),
            mlx_model=env.get("THETAHAWK_MLX_MODEL", "Qwen/Qwen3-8B-MLX-4bit"),
        )
