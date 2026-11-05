"""Minimal local, read-only Streamlit view of Thetahawk candidates."""

from __future__ import annotations

import os
from typing import Any

import requests
import streamlit as st


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
        refresh = st.button("Refresh candidates", type="primary")

    # A button makes refresh behavior explicit; the initial view also loads once.
    del refresh
    payload, error = _load_candidates(api_url)
    if error:
        st.info(error + " Start the API and refresh to view stored scan results.")
        st.code("uvicorn thetahawk.api:app --host 127.0.0.1 --port 8000")
        return

    candidates = payload["candidates"]
    st.caption(f"API snapshot: {payload.get('as_of', 'time unavailable')}")
    if not candidates:
        st.info("No candidates are available. Run a scan or load demo observations first.")
        return

    # Keep the overview compact while retaining the full source record for review.
    preferred_columns = (
        "symbol",
        "scanner",
        "score",
        "created_at",
        "status",
        "summary",
        "risks",
    )
    rows = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        rows.append({key: candidate.get(key) for key in preferred_columns if key in candidate})

    st.subheader(f"Research candidates ({len(rows)})")
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.info("The API returned records that do not yet match the dashboard overview fields.")

    labels = [
        f"{item.get('symbol', 'Unknown')} · {item.get('scanner', 'candidate')} · #{index + 1}"
        for index, item in enumerate(candidates)
    ]
    selected_index = st.selectbox("Inspect candidate record", range(len(labels)), format_func=labels.__getitem__)
    st.json(candidates[selected_index], expanded=True)


if __name__ == "__main__":
    main()
