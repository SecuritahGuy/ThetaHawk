# Thetahawk

Thetahawk is a self-hosted options intelligence and research project. The first release focuses on deterministic, auditable screening and evidence-backed research. It does not place trades.

See [ROADMAP.md](ROADMAP.md) for product scope, phases, acceptance criteria, and safety boundaries.
The current Mac model recommendation is documented in [docs/LOCAL_MODEL.md](docs/LOCAL_MODEL.md).

## Project status

The repository is being bootstrapped from an empty remote. Phase 1 implementation is in progress. Provider selection is intentionally open until field coverage, history, latency, usage rights, and cost are checked against the research needs.

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

The demo writes one synthetic earnings case to the local DuckDB database. It is labeled as fictional in the record and must not be treated as a live market signal. Real provider adapters and historical options data have not been selected or connected yet.

## Research-only boundary

All outputs are candidates for human review, not trade instructions. Numeric calculations and risk checks belong in deterministic code. There is no live order execution in the roadmap's first three implementation phases; paper trading is a later, explicitly reviewed phase.
