"""Versioned provenance for one deterministic scan invocation."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .domain import record_payload


@dataclass(frozen=True, slots=True)
class ScanRunManifest:
    run_id: str
    scanner: str
    scanner_version: str
    policy_version: str
    code_version: str
    as_of: datetime
    candidate_ids: tuple[str, ...]
    inputs: Mapping[str, Any]


def create_scan_run_manifest(
    *,
    scanner: str,
    scanner_version: str,
    policy_version: str,
    as_of: datetime,
    candidate_ids: tuple[str, ...],
    inputs: Mapping[str, Any],
    code_version: str | None = None,
    run_id: str | None = None,
) -> ScanRunManifest:
    """Create a manifest, normalizing records to JSON-safe exact input values."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    if not scanner.strip() or not scanner_version.strip() or not policy_version.strip():
        raise ValueError("scanner, scanner_version, and policy_version are required")
    return ScanRunManifest(
        run_id=run_id or str(uuid4()),
        scanner=scanner,
        scanner_version=scanner_version,
        policy_version=policy_version,
        code_version=code_version or current_code_version(),
        as_of=as_of,
        candidate_ids=tuple(candidate_ids),
        inputs={key: _json_value(value) for key, value in inputs.items()},
    )


def current_code_version() -> str:
    """Return commit and dirty status without making git a runtime dependency."""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
        diff = subprocess.run(
            ["git", "diff", "HEAD", "--binary"],
            check=True,
            capture_output=True,
            timeout=2,
        ).stdout
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
        if not diff and not untracked:
            return f"git:{commit}"
        tree = hashlib.sha256()
        tree.update(diff)
        repo_root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
        for relative_path in sorted(untracked.splitlines()):
            path = Path(repo_root) / relative_path
            if path.is_file():
                tree.update(relative_path.encode("utf-8"))
                tree.update(path.read_bytes())
        return f"git:{commit}:tree={tree.hexdigest()}"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _json_value(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return record_payload(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if hasattr(value, "isoformat") and callable(value.isoformat):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"Unsupported manifest input value: {type(value).__name__}")
