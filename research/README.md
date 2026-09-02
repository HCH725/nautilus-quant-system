# PyBroker research frontend

This directory is an isolated, read-only research environment (Loop B — PyBroker Experiment & Attrition Loop: high-throughput, deterministic; no LLM per candidate; survivors only → Gate → Nautilus High-Fidelity). It is not part
of the root Nautilus runtime and has no credentials, order routing, Testnet, or
live-trading integration.

From the repository root, rebuild the pinned environment and run the single
BTCUSDT 1H long/flat candidate generator:

```bash
uv venv --clear --python 3.12.13 research/.venv
uv pip sync research/requirements.lock --python research/.venv/bin/python
research/.venv/bin/python research/pybroker_research.py \
  --catalog data/catalog \
  --output var/runs/pybroker/candidate.json
```

The output is ignored by Git. It follows
`docs/contracts/pybroker-candidate-v1.md` and remains provisional until a
later NautilusTrader validation. Its `source.sha256` covers each sorted
canonical Parquet relative path, a NUL separator, and that file's exact bytes.

Run the narrow research checks with:

```bash
research/.venv/bin/python -m unittest discover -s research -p 'test_pybroker_research.py' -v
```