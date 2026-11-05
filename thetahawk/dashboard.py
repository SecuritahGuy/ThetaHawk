"""Local, read-only Streamlit view for screening and reviewing candidates."""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

import requests
import streamlit as st


_METRIC_KEYS = (
    "option_expiration",
    "option_dte",
    "option_strike",
    "underlying_price",
    "implied_move_fraction",
    "historical_median_move_fraction",
    "current_window_move_fraction",
    "robust_z_score",
    "implied_volatility",
    "realized_volatility_annualized",
    "iv_to_rv_ratio",
    "volume_ratio_to_median",
    "history_sample_count",
    "baseline_session_count",
    "quote_age_seconds",
    "rank",
    "research_screen_count",
    "moneyness_deviation_fraction",
    "contract_symbols",
    "supporting_screens",
    "ranking_basis",
)


def _load_candidates(api_url: str, timeout: float = 4.0) -> tuple[dict[str, Any] | None, str | None]:
    try:
        response = requests.get(f"{api_url.rstrip('/')}/candidates", timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        return None, f"Could not reach the local API at {api_url}: {exc}"
    except ValueError:
        return None, "The local API returned an unreadable response."

    if not isinstance(payload, dict) or not isinstance(payload.get("candidates"), list):
        return None, "The local API response did not contain a candidate list."
    return payload, None


def _candidate_metrics(candidate: dict[str, Any]) -> dict[str, Any]:
    metrics = candidate.get("metrics")
    return metrics if isinstance(metrics, dict) else {}


def _candidate_expiration(candidate: dict[str, Any]) -> str | None:
    metrics = _candidate_metrics(candidate)
    value = metrics.get("option_expiration") or metrics.get("expiration")
    return str(value) if value else None


def _filter_candidates(
    candidates: Iterable[dict[str, Any]],
    *,
    symbol: str = "All symbols",
    scanner: str = "All screens",
    status: str = "All statuses",
    expiration: str = "All expirations",
) -> list[dict[str, Any]]:
    """Apply local discovery-view filters without changing stored candidates."""
    result = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if symbol != "All symbols" and str(candidate.get("symbol", "")).upper() != symbol.upper():
            continue
        if scanner != "All screens" and candidate.get("scanner") != scanner:
            continue
        if status != "All statuses" and candidate.get("status") != status:
            continue
        if expiration != "All expirations" and _candidate_expiration(candidate) != expiration:
            continue
        result.append(candidate)
    return result


def _expiration_options(candidates: Iterable[dict[str, Any]]) -> list[str]:
    expirations = {
        expiration
        for candidate in candidates
        if isinstance(candidate, dict)
        if (expiration := _candidate_expiration(candidate)) is not None
    }
    return ["All expirations", *sorted(expirations)]


def _candidate_table_rows(candidates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten the most useful discovery fields while retaining raw details below."""
    rows = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        metrics = _candidate_metrics(candidate)
        quote_sources = sorted({
            str(value)
            for key, value in metrics.items()
            if "source" in key and isinstance(value, str) and value
        })
        compact_metrics = {
            key: metrics[key]
            for key in _METRIC_KEYS
            if key in metrics and metrics[key] is not None
        }
        risks = candidate.get("risks") or ()
        rows.append({
            "symbol": candidate.get("symbol"),
            "screen": candidate.get("scanner"),
            "status": candidate.get("status"),
            "score": candidate.get("score"),
            "created_at": candidate.get("created_at"),
            "expiration": _candidate_expiration(candidate),
            "summary": candidate.get("summary", ""),
            "key_metrics": compact_metrics,
            "sources": ", ".join(quote_sources) or "See input timestamps",
            "risk_notes": " · ".join(str(risk) for risk in risks[:2]),
        })
    return rows


def main() -> None:
    st.set_page_config(page_title="Thetahawk", page_icon="🪶", layout="wide")
    st.title("Thetahawk")
    st.caption("Local options research · informational candidates only")
    st.warning(
        "Research only. This dashboard does not provide personalized financial advice "
        "or place trades. Verify data freshness, liquidity, and risks independently."
    )

    default_api = os.environ.get("THETAHAWK_API_URL", "http://127.0.0.1:8000")
    with st.sidebar:
        st.header("Research feed")
        api_url = st.text_input("Local API URL", value=default_api)
    payload, error = _load_candidates(api_url)
    if error:
        st.info(error + " Start the API and refresh to view stored scan results.")
        st.code("uvicorn thetahawk.api:app --host 127.0.0.1 --port 8000")
        return

    candidates = [item for item in payload["candidates"] if isinstance(item, dict)]
    st.caption(f"API snapshot: {payload.get('as_of', 'time unavailable')}")
    if not candidates:
        st.info("No candidates are available. Run a scan or load demo observations first.")
        return

    symbols = ["All symbols", *sorted({str(item.get("symbol", "")).upper() for item in candidates if item.get("symbol")})]
    scanners = ["All screens", *sorted({str(item.get("scanner", "")) for item in candidates if item.get("scanner")})]
    statuses = ["All statuses", *sorted({str(item.get("status", "")) for item in candidates if item.get("status")})]
    expirations = _expiration_options(candidates)
    with st.sidebar:
        st.header("Filter candidates")
        selected_symbol = st.selectbox("Symbol", symbols)
        selected_scanner = st.selectbox("Screen", scanners)
        selected_status = st.selectbox("Status", statuses)
        selected_expiration = st.selectbox("Expiration", expirations)

    filtered = _filter_candidates(
        candidates,
        symbol=selected_symbol,
        scanner=selected_scanner,
        status=selected_status,
        expiration=selected_expiration,
    )
    st.subheader(f"Research candidates ({len(filtered)} of {len(candidates)})")
    if not filtered:
        st.info("No candidates match these filters.")
        return

    rows = _candidate_table_rows(filtered)
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.info("The API returned records that do not yet match the dashboard overview fields.")

    labels = [
        f"{item.get('symbol', 'Unknown')} · {item.get('scanner', 'candidate')} · "
        f"{item.get('status', 'unknown')} · #{index + 1}"
        for index, item in enumerate(filtered)
    ]
    selected_index = st.selectbox("Inspect candidate record", range(len(labels)), format_func=labels.__getitem__)
    selected = filtered[selected_index]
    st.markdown(f"**{selected.get('summary', 'No summary available.')}**")
    metrics = _candidate_metrics(selected)
    if metrics:
        st.markdown("**Screen metrics**")
        st.dataframe(
            [{"metric": key, "value": value} for key, value in metrics.items()],
            use_container_width=True,
            hide_index=True,
        )
    with st.expander("Rationale and risks", expanded=True):
        st.write("**Rationale**")
        st.write(selected.get("rationale") or [selected.get("summary", "No rationale recorded.")])
        st.write("**Risks**")
        st.write(selected.get("risks") or ["No risk notes recorded."])
    with st.expander("Sources, timestamps, and full record"):
        st.write("**Input IDs**")
        st.write(selected.get("input_ids") or ["No input IDs recorded."])
        st.write("**Source timestamps**")
        st.write(selected.get("source_timestamps") or ["No source timestamps recorded."])
        st.json(selected, expanded=False)


if __name__ == "__main__":
    main()
