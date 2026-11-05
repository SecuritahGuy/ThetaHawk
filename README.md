# Thetahawk

Thetahawk is a self-hosted options intelligence and research project. The first release focuses on deterministic, auditable screening and evidence-backed research. It does not place trades.

See [ROADMAP.md](ROADMAP.md) for product scope, phases, acceptance criteria, and safety boundaries.
The current Mac model recommendation is documented in [docs/LOCAL_MODEL.md](docs/LOCAL_MODEL.md).
Provider capabilities and the Alpaca read-only adapter are described in [docs/DATA_PROVIDERS.md](docs/DATA_PROVIDERS.md).

## Project status

Phase 1 is in progress. Read-only Alpaca equity/options/clock access, timestamped CSV earnings inputs, the earnings and equity screens, initial IV/RV and unusual-activity screens, scheduled snapshot collection, an integrated ranked discovery command, and the local demo path are in place. Yahoo adjusted daily prices are a temporary historical equity source. Discovery enumerates eligible expirations in the configured universe, selects liquid near-ATM pairs, runs available screens, and stores ranked groups with their supporting evidence. The initial universe is explicit; it does not search every listed stock. The earnings calendar export leaves timing unknown for review; exact scan replay and event-time options history are still open validation items.

## Planned local stack

- Python 3.11+
- DuckDB and Parquet for local research data
- Polars, NumPy, and SciPy for deterministic analysis
- FastAPI and Streamlit for read-only local surfaces
- Optional Ollama integration for sourced narrative research

## Local demo

Install the package and start with explicitly fictional data:

```sh
python -m pip install -e .
python -m thetahawk.demo
uvicorn thetahawk.api:app --host 127.0.0.1 --port 8000
streamlit run thetahawk/dashboard.py
```

The demo writes one synthetic earnings case to the local DuckDB database. It is labeled as fictional in the record and must not be treated as a live market signal.

For optional read-only Alpaca ingestion, copy `.env.example` to `.env`, configure keys and feeds from [docs/DATA_PROVIDERS.md](docs/DATA_PROVIDERS.md), then run `python -m thetahawk.ingest`. To run the earnings screen, fill the timestamped event and outcome CSVs using the schemas in `examples/`, then run `python -m thetahawk.scan --alpaca`. This command fetches current quotes only when `--alpaca` is supplied, records feed provenance, and stores suppressed outcomes when required data is missing. It places no orders.

The overextended-equity screen uses an Alpaca current quote and Yahoo Finance adjusted daily closes. Install its optional dependency with `python -m pip install -e '.[yahoo]'`, then run `python -m thetahawk.equity_scan --symbols AAPL`. See [docs/DATA_PROVIDERS.md](docs/DATA_PROVIDERS.md) for source limitations and data-use notes.

To prepare upcoming earnings dates for review, run `python -m thetahawk.earnings_calendar --symbols AAPL,MSFT --start YYYY-MM-DD --end YYYY-MM-DD`. It exports Yahoo's reported dates to `data/yahoo_earnings_review.csv` without guessing announcement timing or option expiration. Review those fields before copying the rows into the configured earnings input CSV; the earnings scanner suppresses events with unknown timing.

The initial IV/RV screen can be run with `python -m thetahawk.volatility_scan --symbols AAPL`; it uses current Alpaca option/equity snapshots and Yahoo daily history. To seed unusual-activity history, run `python -m thetahawk.option_snapshots --symbols AAPL` once per session or schedule it with `python -m thetahawk.scheduled_collection --job options --symbols AAPL` (default 15-minute delay). After enough same-contract snapshots have accumulated, run `python -m thetahawk.unusual_scan --symbols AAPL`. Equity and earnings jobs default to a five-minute delay. Scheduled jobs use Alpaca's market clock and bounded retry behavior. Inspect stored evidence for a candidate with `python -m thetahawk.replay CANDIDATE_ID`. Scan commands save versioned manifests and normalized inputs. Earnings, equity, IV/RV, unusual-activity, and integrated-discovery candidates can be recomputed when scanner, policy, and code versions match; otherwise the command returns an input audit and explains the mismatch.

To collect prospective option quotes for reviewed earnings events, fill `timing` and `expiration_date` in the configured earnings CSV, then run `python -m thetahawk.earnings_option_snapshots --symbols AAPL` or `thetahawk-earnings-option-snapshots --symbols AAPL`. To collect those chains every 15 minutes during market hours, run `python -m thetahawk.scheduled_collection --job earnings-options --symbols AAPL`. The existing `--job options` remains the separate near-30-DTE baseline collector.

For persistent collection on this Mac, run `python scripts/install_mac_collectors.py`. It installs two per-user LaunchAgents that collect the configured near-30-DTE chains and reviewed earnings expirations every 15 minutes during open market sessions, and restart at login. Logs go to `data/options.log` and `data/earnings-options.log`. Check them with `python scripts/install_mac_collectors.py --status`; remove both services with `python scripts/install_mac_collectors.py --uninstall`. Collection requires an active user login and the configured Alpaca credentials in `.env`.

The initial SEC event-catalyst collector reads filing headers and links only. Set `THETAHAWK_SEC_USER_AGENT` in `.env` to an app name and contact email as described in [docs/DATA_PROVIDERS.md](docs/DATA_PROVIDERS.md), then run `python -m thetahawk.sec_filings --symbols AAPL --days-back 7`. It stores neutral filing candidates for review; it does not analyze filing text or infer market direction.

Run the integrated ranked options-discovery pass with `python -m thetahawk.discover` or `thetahawk-discover`. It defaults to `THETAHAWK_SYMBOLS` and supports `--symbols AAPL,MSFT` plus `--history-period 1y`. Results and the underlying screen records are saved to DuckDB and are available in the dashboard/API. Ranking counts distinct screens that met their own research thresholds; it does not blend their scores or estimate profitability. Unusual-activity evidence will remain suppressed until the database contains enough comparable same-contract history.

Check whether the local database has enough comparable option history with `python -m thetahawk.data_status` or `thetahawk-data-status`. It reports the maximum exact contract/feed/time-bucket session count per symbol, earnings-linked quote snapshots, and historical-price/earnings-outcome coverage. A mature count means a baseline may be possible; the scanner still validates the exact current quote and historical rows.

Review available earnings outcome coverage with `python -m thetahawk.earnings_validation` or `thetahawk-earnings-validation`. The report only matches candidates with a scan date before the event and a later, uniquely linked realized move that has an explicit `post_event_date`. It reports descriptive move error and sample size; it is not a strategy backtest. Existing CSV outcome rows without `post_event_date` are excluded until that measurement date is reviewed and supplied.

## Research-only boundary

All outputs are candidates for human review, not trade instructions. Numeric calculations and risk checks belong in deterministic code. There is no live order execution in the roadmap's first three implementation phases; paper trading is a later, explicitly reviewed phase.
