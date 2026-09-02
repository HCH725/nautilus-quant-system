# Nautilus Quant System

**English** | [繁體中文](README.zh-TW.md)

> [!IMPORTANT]
> This is an independent project built on top of the official [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) runtime. It is not an official NautilusTrader fork, is not affiliated with Nautech Systems, and is not sponsored or endorsed by Nautech Systems. See [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) for full attribution and third-party licensing information.

An independent, deterministic Binance USD-M Futures data core using NautilusTrader `2.0.0rc2`.

## Research-to-Validation Workflow

This repository is the downstream strategy-validation and execution layer of a broader research workflow. External alpha discovery is staged in [`alpha-strategy-research`](https://github.com/HCH725/alpha-strategy-research), where public-source ideas are normalized into Wiki Brain-ready research records for ChatGPT review and direct ingestion into Hermes Wiki Brain.

Accepted knowledge can then be synthesized by Hermes into testable hypotheses and passed into this repository for structured research and validation:

```text
External public sources
        ↓
Antigravity research
        ↓
alpha-strategy-research
(normalized, source-backed research records)
        ↓
ChatGPT review
        ↓
Hermes Wiki Brain
        ↓
Hermes hypothesis / synthesis
        ↓
PyBroker research candidate
        ↓
NautilusTrader historical verdict
        ↓
feedback / lineage / reuse
        ↓
later gated Paper → Binance Demo/Testnet → Live progression
```

Quant Research Pipeline — Three Layers, Two Loops, One Gate:

- **Data foundation:** Nautilus canonical market-data truth (bars / Funding / D-1).
- **Loop A — Hermes Research Loop (low-frequency, theory/evidence-driven):** Wiki Brain / reviewed strategy intake → falsifiable research thesis or strategy family → bounded meaningful hypothesis branches → experiment specification. The outer-loop iteration unit is **one research thesis / strategy family**, not one parameter set; Hermes may generate a bounded set of meaningful branches (LLM tokens decide *what* to test).
- **Loop B — PyBroker Experiment & Attrition Loop (high-throughput, deterministic):** experiment specification → deterministic campaign expansion → N provisional candidates → batch backtests/screens → dedupe/invalid/reject/pass accounting → attrition funnel. **No LLM call per candidate**; machine compute runs high-volume experiments. Rejected candidates do not enter Nautilus.
- **Gate — Formal signal parity / promotion gate (fail-closed):** independently recompute against canonical data and require parity before promotion.
- **Nautilus High-Fidelity Validation (authoritative, scarce):** parity-passed survivors only → historical accounting including fills/fees/funding/PnL → walk-forward/regime/cost robustness → strategy freeze → Shadow/Paper → Demo/Testnet → human/live boundary.
- **Outer feedback:** Hermes reviews survivor summaries, failure taxonomy and information gain, then decides whether to stop, refine or open a new experiment batch. Continuation is evidence-based; do not encode a fixed number of machine backtests. Kanban/reasoning iteration ≠ individual backtest run.

The two repositories therefore serve different responsibilities: `alpha-strategy-research` is the public-safe upstream research and knowledge-handoff layer, while `nautilus-quant-system` is the controlled validation, accounting, execution-research, and eventual deployment layer. An idea appearing upstream is **research material only** and is not automatically considered validated or trading-eligible here.

## PyBroker Strategy Incubator

PyBroker is an isolated upstream strategy-research frontend. It reads existing market data in read-only mode, executes research strategies, and emits data-only candidates. NautilusTrader remains the single source of truth for canonical data, formal backtests, fills, fees, funding, positions, PnL, and accounting.

- PyBroker exists only inside the isolated `research/` environment and is not added to the formal root runtime.
- Research does not rewrite the canonical catalog or funding data, and it has no credentials or order permissions.
- Candidates are canonical JSON and contain no framework objects, caches, pickle payloads, or executable payloads.
- PyBroker results are always provisional; Shadow, Paper, Binance Demo/Testnet, and live trading are outside the v1 implementation scope.
- V1 has already executed the full **Two Loops + One Gate** research loop (**Loop A Hermes thesis/branches low-frequency → Loop B PyBroker N-candidate deterministic attrition high-throughput, no LLM per candidate → Gate signal-parity fail-closed → Nautilus historical verdict for survivors only**), with lineage, successes, and failures recorded in an append-only SQLite ledger. A single Hermes reasoning iteration maps to **one research thesis / strategy family**, which Loop B expands deterministically into N machine experiments; Kanban iteration ≠ individual backtest run.
- The long-term operating model allows Hermes to autonomously add strategy families and formulas, while requiring trading-eligible candidates to pass Paper and Binance Demo/Testnet validation. Live trading remains separately authorized.

Plans, responsibility boundaries, and contracts:

- [`docs/plans/pybroker-nautilus-adoption.md`](docs/plans/pybroker-nautilus-adoption.md)
- [`docs/plans/2026-08-14-strategy-loop-v1.md`](docs/plans/2026-08-14-strategy-loop-v1.md)
- [`docs/architecture/hybrid-pybroker-nautilus.md`](docs/architecture/hybrid-pybroker-nautilus.md)
- [`docs/architecture/strategy-loop-operating-model.md`](docs/architecture/strategy-loop-operating-model.md)
- [`docs/contracts/pybroker-candidate-v1.md`](docs/contracts/pybroker-candidate-v1.md)
- [`docs/contracts/strategy-loop-v1.md`](docs/contracts/strategy-loop-v1.md)

The isolated runner and lock-rebuild commands are documented in [`research/README.md`](research/README.md). Runtime candidates, verdicts, and ledger state remain under the ignored `var/` directory; the root `nautilus-research` CLI owns formal historical Nautilus evaluation, feedback, reuse, and funnel projection.

## Scope

- Symbols: `BTCUSDT`, `ETHUSDT`, `BNBUSDT`, `SOLUSDT`
- Data download start: `2022-01-01T00:00:00Z`
- Backtest start: `2022-07-01T00:00:00Z`; `2022-01-01 → 2022-06-30` is used only for strategy-indicator warm-up and is excluded from backtest performance
- Intervals: `5m`, `15m`, `30m`, `1h`, `4h`, `1d`, `1w`
- Data: trade klines and funding rates; mark, index, and premium index klines are not downloaded
- Boundary: sync only through the previous complete UTC day; weekly bars stop at the latest complete Monday boundary
- Open interest is not included

## Attribution and Licensing

- Original code in this repository is licensed under the [MIT License](LICENSE).
- [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) is maintained by [Nautech Systems](https://www.nautechsystems.io/) and licensed under `LGPL-3.0-or-later`.
- NautilusTrader is used only as an independent, unmodified official PyPI runtime dependency, with version, source, and hashes pinned by `uv.lock`; this repository does not include or redistribute NautilusTrader source code or binary wheels.
- Market data comes from the public Binance USD-M Futures HTTP API; this repository does not contain downloaded market data, API keys, or trading credentials.

See [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) for licensing boundaries, official links, and the non-affiliation statement.

## Data Model

| Source | Nautilus representation |
|---|---|
| trade klines | `Bar/LAST` for the perpetual instrument |
| funding | versioned canonical `FundingObservation` v1 store |

`ParquetDataCatalog` stores instruments and bars. Funding data is stored using versioned generations, a ready pointer, and canonical JSONL; each row preserves its own funding time, rate, truth status, and settlement mark price when official data is available. Legacy `FundingRateUpdate` JSONL is retained only as short-term rollback evidence and is no longer the formal reader/writer format.

## Installation and Testing

```bash
uv sync --dev
.venv/bin/python -m unittest discover -s tests -v
```

Run these commands from the repository root after cloning. `ops/RUNBOOK.md` and `ops/ai.nautilus.quant.data-sync.plist` document the current maintainer's fixed deployment topology on a macOS removable volume; they are not generic installation paths. The maintainer machine is already installed and validated under that topology, including RunAtLoad activation. Other environments must first adjust absolute paths and repeat acceptance validation.

## Sync and Status

```bash
.venv/bin/nautilus-data sync --config config/market_data.json
.venv/bin/nautilus-data status --config config/market_data.json
```

The sync pipeline provides:

- public REST pagination with bounded retry for `418/429/5xx`
- sequence validation per `BarType` from the configured start through the current tail before resume
- UTC D-1 and complete weekly-bar boundaries
- fail-closed validation for duplicates, gaps, tail completeness, and precision
- reconstruction of internal gaps in higher-timeframe Binance bars only when complete, continuous official `1m` REST data is available; each successful stream reports both the `reconstructed` count and `reconstructed_open_ms`, while later failed runs still retain already-read-back `reconstructed_chunks` in run evidence
- fail-closed behavior when `1m` data itself is incomplete, a gap lies at the head or tail, or a single reconstruction chunk exceeds one day; no synthetic data is fabricated
- catalog readback after writes
- cross-process single-writer enforcement via `flock`
- JSON run evidence for every success or failure under `var/runs/`

## Bounded Full-Matrix Acceptance

`config/bounded_matrix.json` pins validation to `2026-07-27 → 2026-08-10 UTC`, with data written only to the ignored `.local/` directory:

```bash
.venv/bin/nautilus-data sync \
  --config config/bounded_matrix.json \
  --now 2026-08-11T00:00:00Z
.venv/bin/python scripts/verify_smoke.py \
  --config config/bounded_matrix.json \
  --now 2026-08-11T00:00:00Z
```

The acceptance surface is 4 symbols × 7 intervals × 1 bar dataset = 28 bar streams, plus one funding stream per symbol, for a total of 32 streams and 4 instruments. The 2026-08-11 execution read back 27,788 bars and 180 funding events, with no Mark/Index/Premium streams. On a second run over the same window, instrument, bar, and funding writes were all 0. This proves only the bounded full matrix; it does not prove that `2022-01-01 → D-1` has been fully backfilled.

## OS-native Scheduling

The maintainer machine has the `ai.nautilus.quant.data-sync` LaunchAgent installed. It runs the formal `config/market_data.json` configuration at `RunAtLoad` and every day at local `10:15`, so core data synchronization does not depend on Hermes being alive. Launchd stdout/stderr is written under `~/Library/Logs/NautilusQuant/`, while domain run evidence is still atomically written to the ignored `var/runs/` directory.

The current deployment has validated RunAtLoad, the natural `10:15` calendar slot, and immediate reruns. The latest health status must still be determined from `nautilus-data status`, the launchctl exit code, and `var/runs/` readback. The PyBroker research frontend does not modify or revalidate this OS-level schedule.

See [`ops/RUNBOOK.md`](ops/RUNBOOK.md) for installation/reload procedures, Removable Volumes permissions, failure recovery, and evidence checks.

## Safety Boundaries

The system already contains an isolated **Two Loops + One Gate** historical research loop (Loop B PyBroker attrition is high-throughput deterministic; only parity-passed survivors reach Nautilus high-fidelity accounting), but it still has no shared live Strategy, Paper/Demo execution client, API key, or real-capital path. Passing data smoke tests, the historical loop, or a simulated environment does not mean the system is production-trading ready. See [`docs/architecture/strategy-loop-operating-model.md`](docs/architecture/strategy-loop-operating-model.md) for long-term validation and autonomy boundaries.

API keys, tokens, credentials, and personal data must never be committed to the repository. They should live in protected environment files or the operating-system Keychain, with programs reading them only through environment variables. The repository uses three layers to reduce accidental disclosure risk: sensitive-file rules in `.gitignore`, versioned pre-commit/pre-push fail-closed scanning, and GitHub secret scanning/push protection. Enable the local hooks after cloning:

```bash
git config core.hooksPath .githooks
```

Hooks report only safe file paths, line numbers, and finding types; sensitive or credential-shaped paths are replaced with irreversible fingerprints instead of printing possible secrets. Hooks scan for known provider keys, sensitive labels, unknown high-entropy strings, common email addresses, Taiwan mobile-phone numbers, and Taiwan national-ID formats. They also reject all binary blobs by default to prevent archives, Office documents, or databases from carrying unscanned data. Do not bypass them with `--no-verify`.

No scanner can fully understand every name, address, unknown identifier, or contextual secret. If a key is suspected to have entered Git history, revoke and rotate it immediately; simply deleting the file or adding another commit is not sufficient. If public binary assets are genuinely needed in the future, first add a narrow allowlist and corresponding content checks rather than weakening binary restrictions globally.
