# Thetahawk

Thetahawk is a self-hosted options intelligence and research project. The first release focuses on deterministic, auditable screening and evidence-backed research. It does not place trades.

See [ROADMAP.md](ROADMAP.md) for product scope, phases, acceptance criteria, and safety boundaries.
The current Mac model recommendation is documented in [docs/LOCAL_MODEL.md](docs/LOCAL_MODEL.md).
Provider capabilities and the Alpaca read-only adapter are described in [docs/DATA_PROVIDERS.md](docs/DATA_PROVIDERS.md).

## Project status

Phase 1 is in progress. A read-only Alpaca adapter, timestamped CSV event/history inputs, one-shot earnings scanner, and local demo path are in place. The adapter defaults to IEX equities and Alpaca's indicative options feed. A longer-history provider choice remains open until quote fields, entitlements, rights, and costs are matched to the validation plan.

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

## Research-only boundary

All outputs are candidates for human review, not trade instructions. Numeric calculations and risk checks belong in deterministic code. There is no live order execution in the roadmap's first three implementation phases; paper trading is a later, explicitly reviewed phase.
