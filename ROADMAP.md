# Thetahawk roadmap

## Product goal

Build a self-hosted options research platform that continuously screens a defined universe, explains evidence behind candidates, and records outcomes. Deterministic code owns market calculations, filters, risk, and scenario P&L. A local language model may summarize sourced evidence and challenge a thesis; it never supplies authoritative calculations or places trades.

The initial product is research only. Every candidate is an idea for a person to review, not a recommendation or order. Live execution is out of scope.

## Phase 1 — Market intelligence MVP

### Implementation status

- In place: local domain/storage, fictional demo path, earnings and overextended-equity screens, Yahoo adjusted daily-close ingestion, Alpaca read-only equity/options/market-clock adapters, CSV event/outcome inputs, Yahoo earnings-date review export, scheduled option-snapshot collection, IV/RV and unusual-activity scan commands, and a replay input-audit command.
- In progress: build enough same-contract options snapshots for a meaningful unusual-activity baseline, and complete end-to-end historical validation for the initial screens. Alpaca equity, indicative options, and market-clock endpoints have passed read-only live checks.
- In progress: validate the integrated discovery command against live provider data; mature the same-contract unusual-activity history; review dashboard/report behavior. Reviewed earnings timing and realized event-history data, exact scan recomputation from versioned run manifests, and the event-catalyst screen remain open.
- Replay currently audits matching stored inputs and explicitly reports that exact recomputation is not possible without scan-run and policy metadata.
- Current commands can enumerate and filter contracts for a supplied/configured symbol, but they run individual screens. They do not yet discover and rank options opportunities across the universe in one pass.
- No paid data subscription is assumed, and no live order connection is configured.

### Deliverable

A local Python application that ingests or loads market/event data, stores snapshots, runs deterministic screens on a configurable universe, and presents ranked research candidates with timestamps, source provenance, liquidity context, and risk notes. Include an earnings-focused screen first and interfaces for the other screens.

### Initial screens

1. Earnings implied-move divergence: compare the option-implied move with a defined history of realized earnings moves, keeping event dates and data availability explicit.
2. Unusual options activity: flag abnormal volume/open-interest observations only where comparable historical baselines exist. **Same-contract, same-source, same-time-bucket screen and stored-snapshot CLI implemented; requires at least 20 comparable prior sessions.**
3. Overextended equities: flag statistically unusual moves against a configurable history. **Initial median/MAD implementation and optional Yahoo history ingestion in place.**
4. Volatility divergence: compare implied and realized volatility with contract/liquidity checks. **Initial IV/RV screen and one-shot CLI implemented and exercised on AAPL with Alpaca snapshots and Yahoo history.**
5. Event catalyst: associate earnings, filings, revisions, and news with candidates; every item carries provider and observed-at timestamps.

### Options-discovery workflow

The Phase 1 discovery target is a configurable 50–100-symbol universe, not every listed stock or option contract. For each symbol, the scan should enumerate eligible expirations and contracts, apply quote freshness, spread, volume, open-interest, moneyness, and event-window checks, run the relevant research screens, and return a deduplicated ranked list with source records and reasons. A screen may identify a symbol first and then choose the contracts used to measure that screen. The result is a research candidate for review; it is never an order.

The current option commands remain useful building blocks: the IV/RV scan selects a near-ATM pair near 30 DTE, and snapshot collection records one expiration near 30 DTE per supplied symbol. The initial integrated pass is implemented in `thetahawk.discover`: it selects liquid near-ATM pairs, runs the available screens over the configured universe, groups evidence by OCC contract, and persists an explainable ranked view. Ranking counts distinct research screens and keeps screen-specific scores separate. The initial universe remains explicit and configurable; dynamic market-wide symbol discovery can come later. Live-feed behavior, sustained history collection, and the dashboard's presentation of ranked groups still need review.

### Phase 1 boundaries

- Start with a configurable 50–100 symbol universe, not a presumed comprehensive market feed.
- Provide provider interfaces and a local/demo input path before committing to a paid feed. Keep provider-specific behavior and licensing constraints explicit.
- Persist raw/normalized observations and scan results locally (DuckDB/Parquet); never silently treat missing or delayed data as current.
- Use a scheduler that can respect market calendars, provider limits, and market-hours configuration. Begin with on-demand and scheduled scans; continuous real-time monitoring is not an MVP requirement.
- Use the local MLX model through an optional OpenAI-compatible loopback endpoint only after deterministic candidates exist. Structured reports cite their input records; no model-generated numeric output is trusted.
- Dashboard is local and read-only for this phase. Alerts can be added after candidate quality and noise are understood.

### Acceptance criteria

- A new checkout can be configured from documented environment/settings files and run with demo data without credentials.
- A scan is reproducible from stored inputs and produces ranked candidates with calculation inputs, timestamps, and provenance.
- The earnings screen handles missing/illiquid options, stale quotes, and absent history by suppressing or clearly marking a candidate rather than inventing values.
- The UI identifies the app as research-only and shows maximum loss / key risks where a defined options strategy is evaluated.
- Provider access is behind interfaces; no paid provider is required for the demo path.

## Phase 2 — Historical validation

- Build an event-time research dataset of pre-event options quotes and post-event outcomes.
- Simulate executable entries and exits using bid/ask, fees, slippage, and realistic data availability; document contract selection and corporate-action handling.
- Compare with relevant benchmarks, use walk-forward and out-of-sample periods, and report sample sizes, drawdowns, and sensitivity to assumptions.
- Retain skipped candidates and rejected hypotheses so selection bias is visible. Do not promote a screen based on a small, cherry-picked sample.

## Phase 3 — Paper-trading evaluation

- Add an Alpaca paper-trading adapter only after Phase 2 data and risk rules are reviewed.
- Keep order submission explicitly human-approved; enforce position limits and a kill switch outside any model.
- Track proposed, approved, rejected, filled, and exited ideas plus realized/unrealized P&L and drawdowns.
- Evaluate across a meaningful number of trades and market regimes before discussing real-money execution.

## Phase 4 — Advanced research

- Add sourced SEC filing, transcript, analyst-revision, and news research with retrieval and audit trails.
- Compare local models on fixed historical cases; measure factual grounding and forecast calibration rather than subjective confidence.
- Explore strategy-specific probability estimates and model ensembles only when sufficient labeled data exists.

## Initial architecture

- Python application with typed domain models and provider interfaces.
- Polars/NumPy/SciPy for deterministic analytics; DuckDB and Parquet for local research storage.
- FastAPI for a local read-only API and Streamlit for the first dashboard.
- Fixed-delay scheduled collection for configurable jobs, with Alpaca market-clock and provider-rate-limit controls. Revisit APScheduler if orchestration needs grow.
- Optional Apple MLX LM server for evidence-grounded narrative generation; default model selection is documented in `docs/LOCAL_MODEL.md`.
- Provider adapters selected only after verifying field coverage, historical depth, latency, retention, usage rights, and cost for the intended workflow.

## First implementation workstreams

1. Domain/data foundation: settings, schemas, local persistence, provider protocols, and demo fixtures.
2. Quantitative screens: expected-move calculations and earnings candidate ranking, with explicit quote quality checks.
3. Read-only application surface: candidate API and local dashboard using the domain interfaces.
4. Integration: wire modules together, document setup and operating limits, then inspect the complete diff before the first commit.

Workstreams should remain modular and research-only. No provider credentials, paid subscriptions, real-money connectivity, or trade execution are required to complete this initial build.
