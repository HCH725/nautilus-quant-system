# Strategy Loop Operating Model

> **Status — accepted architecture decision, 2026-08-14.**
> This document records the long-term operating model agreed with the operator. It distinguishes accepted direction from implemented capability: the historical two-generation v1 loop is real and verified; shared live signal execution, Paper, Binance Demo/Testnet, robustness promotion, and automated campaign control are not yet implemented.

Related implemented contracts and evidence:

- [`../contracts/strategy-loop-v1.md`](../contracts/strategy-loop-v1.md)
- [`../plans/2026-08-14-strategy-loop-v1.md`](../plans/2026-08-14-strategy-loop-v1.md)
- [`hybrid-pybroker-nautilus.md`](hybrid-pybroker-nautilus.md)

## Decision summary

1. Xiaoqian/Hermes may autonomously invent and implement new indicator families and formulas. New families must be tracked source with executable tests and independent audit; they do not require per-family operator approval.
2. The model may propose, implement, interpret, mutate, kill, and advance strategies. Deterministic code remains the sole producer of indicator values, signals, order intent, metrics, and evidence identities.
3. Historical backtesting is necessary but insufficient. Every strategy seeking `trading-eligible` status must pass prospective production-data Shadow/Paper evidence and Binance simulated-environment order-lifecycle validation.
4. For new Binance Futures simulated setups, use the adapter's Demo environment unless live inspection proves a different current requirement; legacy Testnet remains a distinct supported environment, and its market data/liquidity must not be treated as production-equivalent.[1]
5. Real-funds Live deployment remains a separate authorization and risk contract. Paper or Demo/Testnet success never grants Live permission.
6. Strategy trials live in the immutable research ledger. Kanban manages engineering changes, policy changes, and bounded audits—not one card per hypothesis or funnel stage.

## One truth owner per concern

| Concern | Owner |
|---|---|
| Thesis, falsification, new family/formula, bounded parameter campaign | Xiaoqian/Hermes |
| Indicator and signal computation | Tracked deterministic family kernel |
| Provisional research screening | Isolated PyBroker frontend |
| Historical fills, fees, Funding, sizing, positions, PnL, accounting | NautilusTrader |
| Prospective live-data strategy behavior | Nautilus Shadow/Paper runtime |
| Venue authentication, filters, order lifecycle, user stream, reconciliation | Nautilus Binance Demo/Testnet adapter |
| Strategy versions, experiments, errors, verdicts, lineage | Append-only SQLite ledger plus hashed artifacts |
| Funnel, dashboard, and summaries | Read-only ledger/artifact projection |
| Real-funds admission and capital limits | Separate operator-approved Live policy |

Canonical market-data ingestion remains independent of Hermes, PyBroker, Paper, Demo/Testnet, and dashboard availability.

## Funnel forward path and loop return paths

The funnel answers **which strategy advances**. Loop engineering answers **what happens after each verdict**.

```text
Hermes new family / hypothesis
        ↓
Formula tests + historical/live signal parity
        ↓
PyBroker provisional screen
        ↓
Nautilus historical evaluation
        ↓
Walk-forward / regime / cost robustness
        ↓
Production-data Shadow / Paper
        ↓
Binance Demo / Testnet order lifecycle
        ↓
Promotion eligible
        ↓
Bounded Live (separately authorized)
```

Every completed tier emits a canonical verdict and one bounded next action:

```text
MUTATE          create an interpretable child strategy
NEW_FAMILY      tracked formula/family replacement or extension
KILL            preserve evidence and stop the lineage
ADVANCE         enter the next evidence tier
FIX_TECHNICAL   repair implementation/runtime without counting a bad strategy
```

The return paths are:

```text
PyBroker / historical / robustness economic failure
    → MUTATE, NEW_FAMILY, or KILL
    → restart at the appropriate research tier

Paper technical failure
    → FIX_TECHNICAL
    → rerun parity and restart prospective evidence for the same strategy logic

Paper economic failure
    → create a child or new family
    → rerun the complete historical and robustness path
    → begin a new prospective evidence window

Demo/Testnet execution failure
    → fix adapter, order mapping, reconciliation, or risk wiring
    → repeat the bounded order-lifecycle suite

Demo/Testnet signal mismatch
    → return to the shared family kernel
    → rerun historical parity, Paper, and Demo/Testnet
```

Previously inspected Paper data cannot be relabeled as a fresh prospective holdout after strategy mutation. A strategy change starts a new prospective cohort.

## Three nested feedback loops

### 1. Strategy evolution loop

```text
Hermes ↔ PyBroker ↔ Nautilus historical ↔ robustness verdict
```

Hermes may autonomously change or create:

- indicator family and formula;
- parameters and regime/trigger composition;
- instrument and bar type within the approved data universe;
- thesis and falsification condition.

A child should change one interpretable dimension when attribution matters. A broad initial campaign may use a pinned deterministic grid, but generation budget, duplicate suppression, data cohort, and policy identity must be fixed before execution.

### 2. Prospective Paper loop

```text
Production live market data
    → shared live Strategy
    → Shadow/Paper verdict
    → Hermes next action
```

Paper is not a historical rerun. It verifies future-arriving closed bars, warm-up, data continuity, timestamp parity, signal identity, restart idempotency, risk/sizing behavior, and long-running strategy state. Nautilus exposes the same strategy and core engines across backtest and live-node operation, but that capability still requires a project-owned shared signal implementation and executable parity checks.[2]

Paper has two explicit modes:

- **Shadow:** calculate canonical signal/order intent but submit no order.
- **Sandbox Paper:** consume production market data while a sandbox execution client simulates orders, fills, positions, and accounting.

Paper PnL remains simulated and is not evidence of venue execution quality.

### 3. Execution engineering loop

```text
Nautilus Strategy
    → Binance Demo/Testnet execution
    → venue order/fill/position reports
    → execution verdict
```

This loop validates credentials and environment binding, server time, instrument filters, order accept/reject, cancel/replace, conditional and reduce-only behavior, user streams, reconnects, unknown-order outcomes, restart reconciliation, and duplicate-order prevention. It primarily repairs execution engineering; it must not silently optimize alpha formulas against simulated venue fills.

Binance currently publishes dedicated USD-M simulated REST and WebSocket endpoints, while warning through the separate environment model that production and simulated-market behavior are not interchangeable.[3]

## Shared strategy-family kernel

The current v1 candidate contains historical signal timestamps. It cannot compute tomorrow's signal by itself. Paper and Demo/Testnet therefore require one tracked deterministic family kernel shared across environments:

```text
closed bars + family parameters
    → deterministic family kernel
    → signal_id + score + target intent + reason
```

The same kernel must drive:

- PyBroker historical batch research;
- Nautilus historical event replay;
- production-data Shadow/Paper;
- Binance Demo/Testnet order intent;
- future authorized Live execution.

For the same canonical bars, parameters, and family version, environments must agree on signal timestamp, `signal_id`, direction, score, target intent, and reason. They are not expected to produce identical fills.

A new family may be implemented autonomously, but it cannot carry credentials, quantity, leverage, fees, Funding policy, order type, or accounting truth. Those remain versioned evaluator/risk/execution policy.

## Agent invocation discipline

Hermes does not run inside every bar or order event.

```text
Market-event frequency
    deterministic Nautilus code only

Experiment / Paper-window / Demo-lifecycle boundary
    canonical verdict written to ledger
    → Hermes reads bounded evidence
    → Hermes emits the next structured action

Campaign boundary
    Hermes reviews cohort summary
    → selects the next generation, stops lineages, or advances survivors
```

Large campaigns must not invoke one LLM call per strategy. A deterministic campaign runner expands pinned grids, deduplicates content-addressed strategy IDs, executes the cohort, and presents a bounded generation summary to Hermes.

## Ledger requirements for the extended loop

The durable evidence model must preserve at least:

- parent strategy and source verdict;
- family implementation/version and canonical parameters;
- evaluation tier and environment;
- data cohort, data-as-of, policy, and runtime identity;
- technical status separately from strategy outcome;
- reason codes and bounded metrics;
- the changed dimension and next action;
- artifact path plus content hash;
- Paper prospective cohort boundary;
- Demo/Testnet order and reconciliation evidence without credentials.

The dashboard and funnel remain read-only projections. Missing evidence renders as `N/A`, never fabricated zero or model-written performance.

## Autonomy boundary

### Standing autonomous authority

Xiaoqian/Hermes may, without per-trial approval:

- design and implement new families/formulas;
- add tests and family-registry entries;
- run bounded historical campaigns;
- mutate, kill, or advance research lineages;
- operate Shadow/Paper for eligible survivors;
- operate bounded Binance Demo/Testnet lifecycle validation once dedicated simulated credentials and quotas are configured;
- produce ledger-derived summaries and notifications.

### Separate policy or operator decision required

- real-funds Live activation or material capital/risk increase;
- changing canonical data truth or historical split semantics;
- weakening costs, Funding, slippage, sizing, risk, or promotion rules after seeing results;
- introducing a new credential surface or external capital venue;
- treating Paper/Demo/Testnet metrics as production performance.

## Current implementation truth

Implemented and independently verified at `2e424a38fcf9993d142cb31a53960066534f84a1`:

- canonical `strategy-hypothesis-v1` trust boundary;
- isolated parameterized PyBroker execution;
- formal candidate-to-Nautilus historical consumer;
- Nautilus-owned fills, fees, Funding, positions, accounting, and verdict;
- append-only SQLite ledger, failures, lineage, artifact hashes, and funnel projection;
- one real H0 → verdict → H1 two-generation feedback proof;
- deterministic rerun reuse;
- stages through `Nautilus replayed` derived from evidence.

Accepted direction but not yet implemented:

- multi-family registry and shared historical/live signal kernel;
- deterministic campaign expander/controller;
- substantive PyBroker provisional ranking/rejection policy;
- walk-forward/regime/cost robustness tier;
- Shadow/Paper runtime and prospective verdict;
- Binance Demo/Testnet execution contract and verdict;
- automated feedback routing across those later tiers;
- promotion policy, compact operator projection, and bounded Live contract.

This separation is load-bearing: an accepted architecture decision is not implementation evidence.

## Recommended implementation order

This is an ordering record, not execution authorization:

1. Shared deterministic family kernel plus cross-environment parity tests.
2. Substantive provisional PyBroker screen and deterministic campaign expansion.
3. Historical robustness verdict and feedback routing.
4. Production-data Shadow and Sandbox Paper prospective evidence.
5. Binance Demo/Testnet order-lifecycle and reconciliation suite.
6. Compact ledger-derived operator projection.
7. Separately authorized bounded Live contract.

## Sources

[1] https://nautilustrader.io/docs/latest/integrations/binance — NautilusTrader Binance integration
[2] https://nautilustrader.io/docs/latest/concepts/live — NautilusTrader Live Trading
[3] https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info — Binance USD-M General Info
