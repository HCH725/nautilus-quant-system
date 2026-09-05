# Nautilus Quant System

**English** | [繁體中文](README.zh-TW.md)

> [!IMPORTANT]
> This is an independent project built on top of the official [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) runtime. It is not an official NautilusTrader fork, is not affiliated with Nautech Systems, and is not sponsored or endorsed by Nautech Systems. See [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) for full attribution and third-party licensing information.

An independent, deterministic Binance USD-M Futures research, validation, and execution system using NautilusTrader `2.0.0rc2`.

## Canonical Research-to-Execution Workflow

External alpha discovery is staged in [`alpha-strategy-research`](https://github.com/HCH725/alpha-strategy-research). Reviewed knowledge can be ingested into Hermes Wiki Brain, where Hermes forms falsifiable strategy hypotheses and bounded parameter campaigns. From that boundary onward this repository uses **NautilusTrader as the only backtest, accounting, and execution engine**.

```text
External research / alpha-strategy-research
        ↓
ChatGPT review → Hermes Wiki Brain
        ↓
Hermes hypothesis / strategy family
        ↓
Deterministic campaign expansion
        ↓
strategy-candidate-v1 specification
        ↓
Nautilus historical evaluation
(fills / fees / funding / positions / PnL / accounting)
        ↓
Nautilus robustness matrix
(walk-forward / regimes / cost & delay stress)
        ↓
Strategy freeze
        ↓
Nautilus Shadow / Paper
        ↓
Venue qualification / Demo-Testnet when enabled
        ↓
════════ HUMAN / LIVE POLICY BOUNDARY ════════
        ↓
Separately authorized Live
```

### Research control model

- **Hermes research loop:** one thesis or strategy family is the reasoning/Kanban unit. Hermes decides *what* to test and why.
- **Deterministic machine loop:** one frozen campaign expands into bounded parameter combinations without one LLM call per trial. Each trial creates a plain strategy specification and is evaluated by Nautilus.
- **Nautilus historical truth:** canonical bars plus the shared deterministic family kernel drive the same historical evaluator that owns orders, fills, fees, funding, positions, and reconciled account results.
- **Robustness:** survivors enter bounded Nautilus-native windows, parameter neighborhoods, delay/slippage stress, and regime checks. Technical invalidity remains separate from economic rejection.
- **Prospective validation:** a strategy must accumulate new Shadow/Paper evidence before venue qualification. Real funds remain outside automatic promotion.
- **Append-only evidence:** hypotheses, experiments, candidates, verdicts, errors, campaign membership, robustness results, and runtime evidence are content-addressed or hash-verified.

There is no second research backtester and no cross-engine translation/parity handoff in the canonical pipeline. `strategy-candidate-v1` is a plain JSON strategy specification, not another engine's result.

Canonical documents:

- [`docs/architecture/strategy-loop-operating-model.md`](docs/architecture/strategy-loop-operating-model.md)
- [`docs/contracts/strategy-loop-v1.md`](docs/contracts/strategy-loop-v1.md)
- [`docs/contracts/strategy-candidate-v1.md`](docs/contracts/strategy-candidate-v1.md)
- [`docs/contracts/strategy-campaign-v1.md`](docs/contracts/strategy-campaign-v1.md)
- [`docs/contracts/strategy-evidence-envelope-v2.md`](docs/contracts/strategy-evidence-envelope-v2.md)

The root `nautilus-research` CLI owns historical research, campaign execution, immutable evidence, robustness handoff, feedback, reuse, and funnel projection. Runtime evidence remains under ignored `var/` paths and is never canonical market-data input.

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

The current deployment has validated RunAtLoad, the natural `10:15` calendar slot, and immediate reruns. The latest health status must still be determined from `nautilus-data status`, the launchctl exit code, and `var/runs/` readback. The Nautilus research pipeline does not modify or revalidate this OS-level schedule.

See [`ops/RUNBOOK.md`](ops/RUNBOOK.md) for installation/reload procedures, Removable Volumes permissions, failure recovery, and evidence checks.

## Safety Boundaries

The canonical research path is Nautilus-only: deterministic strategy-family specifications are evaluated by Nautilus historical accounting, then robustness and prospective Paper evidence. The repository includes shared strategy/Paper runtime components, but real-capital Live remains separately authorized and venue execution must pass its own bounded qualification. Passing historical or Paper tests does not authorize Live. See [`docs/architecture/strategy-loop-operating-model.md`](docs/architecture/strategy-loop-operating-model.md).

API keys, tokens, credentials, and personal data must never be committed to the repository. They should live in protected environment files or the operating-system Keychain, with programs reading them only through environment variables. The repository uses three layers to reduce accidental disclosure risk: sensitive-file rules in `.gitignore`, versioned pre-commit/pre-push fail-closed scanning, and GitHub secret scanning/push protection. Enable the local hooks after cloning:

```bash
git config core.hooksPath .githooks
```

Hooks report only safe file paths, line numbers, and finding types; sensitive or credential-shaped paths are replaced with irreversible fingerprints instead of printing possible secrets. Hooks scan for known provider keys, sensitive labels, unknown high-entropy strings, common email addresses, Taiwan mobile-phone numbers, and Taiwan national-ID formats. They also reject all binary blobs by default to prevent archives, Office documents, or databases from carrying unscanned data. Do not bypass them with `--no-verify`.

No scanner can fully understand every name, address, unknown identifier, or contextual secret. If a key is suspected to have entered Git history, revoke and rotate it immediately; simply deleting the file or adding another commit is not sufficient. If public binary assets are genuinely needed in the future, first add a narrow allowlist and corresponding content checks rather than weakening binary restrictions globally.
