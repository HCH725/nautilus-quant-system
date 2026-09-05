# Strategy Loop Operating Model

> **Canonical architecture — Nautilus-only research and execution.**
> Hermes owns research reasoning; deterministic project code owns strategy specifications and evidence identities; NautilusTrader is the only backtest, accounting, portfolio, risk, and execution engine.

## Canonical pipeline

```text
Reviewed knowledge / Wiki Brain
        ↓
Hermes falsifiable thesis + strategy family
        ↓
Frozen deterministic campaign / parameter grid
        ↓
strategy-candidate-v1
        ↓
Nautilus historical evaluation
        ↓
Nautilus robustness
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

There is one market/accounting/execution truth engine. Research throughput is achieved by bounded deterministic campaign expansion and Nautilus-native batch evaluation, not by inserting another backtester.

## Two control loops

### 1. Hermes research loop

The outer loop reasons about one thesis or strategy family at a time:

```text
thesis → evidence → interpret → stop / mutate / new family / advance
```

Hermes may propose and implement tracked family formulas, hypotheses, parameter ranges, and falsification conditions. It must not create executable payloads inside hypothesis JSON or bypass independent audit for new strategy-family code. Kanban is an engineering/research control plane; one machine backtest is not one Kanban card.

### 2. Deterministic Nautilus experiment loop

A frozen campaign expands into bounded trials without an LLM call per trial:

```text
frozen campaign
  → deterministic parameter expansion
  → candidate specification
  → Nautilus historical evaluation
  → immutable trial census / verdict
```

Every attempt is retained as generated, duplicate-suppressed, technical-invalid, economically rejected, or surviving. Success-only evidence is forbidden.

## One truth owner per concern

| Concern | Owner |
|---|---|
| Thesis, falsification, family selection, bounded search space | Hermes |
| Indicator and signal computation | tracked deterministic strategy-family kernel |
| Strategy specification | `strategy-candidate-v1` |
| Historical fills, fees, funding, positions, PnL, accounting | NautilusTrader |
| Robustness historical evaluation | NautilusTrader + frozen robustness policy |
| Position/risk/order mapping | versioned Risk & Execution Policy + Nautilus engines |
| Shadow/Paper behavior | shared Nautilus strategy/Paper runtime |
| Venue order lifecycle and reconciliation | Nautilus venue adapter when enabled |
| Experiment/verdict/error/lineage truth | append-only SQLite + hashed artifacts |
| Real-capital admission | separately authorized Live policy |

Canonical historical market-data ingestion is independent from Hermes, Paper, venue adapters, and dashboards. The D-1 operating-system data runner remains the sole canonical historical-store writer. Runtime feeds may create runtime evidence but must not become a second canonical historical writer.

## Strategy-family kernel

One tracked deterministic kernel is shared across historical, robustness, Paper, and future authorized Live execution:

```text
closed bars + family parameters
    → deterministic family kernel
    → signal_id + score + target intent + reason
```

For identical canonical bars, parameters, family version, and kernel identity, historical and prospective runtimes must agree on signal timestamp, signal ID, direction, score, target intent, and reason. Fills need not be identical because execution environments differ.

A family may define formulas and ordinary parameters only. It cannot carry credentials, leverage, quantity, fee schedules, funding policy, order types, or accounting truth. Those belong to evaluator/risk/execution policies.

## Candidate specification boundary

`strategy-candidate-v1` is a canonical JSON strategy specification. It binds:

- instrument and bar type;
- family ID/version and kernel ID;
- ordinary strategy parameters;
- canonical source digest, row count, bounds, and data-as-of;
- evaluation context;
- Nautilus/Python runtime versions.

It deliberately contains no signal list, fills, metrics, PnL, framework object, cache, code, pickle, credential, or order instruction. Nautilus derives decisions from canonical bars through the shared family kernel. Candidate identity is the SHA-256 of its canonical bytes.

## Historical evaluation

Nautilus owns the formal historical path from the first evaluation onward. A run is fail-closed when candidate identity, canonical source, code/runtime/policy identity, or immutable evidence changes.

Historical verdicts include the applicable:

- execution/fill evidence;
- fee source and amounts;
- funding observations and truth status;
- position/account reconciliation;
- net account delta and drawdown;
- source/runtime/code identities;
- reason codes and research decision.

Modeled funding, missing official settlement marks, or unmodeled baseline slippage prevent performance from being labeled fully claimable.

## Robustness

A historical survivor may enter a frozen robustness matrix. The policy may vary time windows, parameter neighborhoods, order delay, and supported cost/slippage stress. Every formal cell still calls the Nautilus evaluator.

Technical invalidity never becomes economic rejection. Robustness actions are bounded:

```text
ADVANCE | HOLD | MUTATE | NEW_FAMILY | KILL | FIX_TECHNICAL
```

A mutation must create an interpretable child and immutable lineage. An effective strategy change starts a new prospective cohort.

## Prospective Paper

Paper is future-arriving evidence, not a historical rerun. The shared strategy/runtime verifies closed-bar normalization, warm-up, timestamp/signal identity, restart idempotency, data continuity, sizing/risk behavior, and terminal-flat handling.

- **Shadow:** produces strategy intent/evidence and submits no orders.
- **Sandbox Paper:** uses a sandbox execution client while consuming prospective market data.

Paper PnL is simulated and is not venue execution-quality evidence.

## Venue qualification and Live boundary

Before any separately authorized Live deployment, the venue execution path must validate environment binding, server time, instrument/filter metadata, quantity/tick constraints, fee schedule identity, order lifecycle, user stream/reconnect behavior, reconciliation, restart, and duplicate-order prevention.

Paper or simulated-environment success never grants real-capital permission. Live requires a separate operator-approved policy and explicit authorization.

## Evidence plane

Required identities are content-addressed or frozen as applicable:

```text
strategy_id
hypothesis_id
family_id + family_version
kernel_version + kernel_hash
data_snapshot_id + data_as_of
code_commit
policy IDs
evaluation_context_id
runtime_id
experiment_id
candidate_id
artifact hashes
```

The ledger is append-only. Read-only projections such as funnels or dashboards cannot create truth. Missing evidence is `N/A`, `BLOCKED`, or technical invalidity—never fabricated PASS evidence.

## Seven-stage funnel

The canonical projection labels are:

1. Proposed
2. Contract valid
3. Candidate specified
4. Research screened
5. Nautilus evaluated
6. Robustness passed
7. Promotion eligible

These labels are ledger projections, not independent engines or services.

## Current boundary

Implemented repository surfaces include deterministic strategy families, Nautilus-native candidates, historical Nautilus evaluation, campaign ledger/census, robustness evidence, shared strategy/Paper components, immutable runtime evidence, and fail-closed identity checks. Venue Demo/Testnet and real-capital Live remain separately gated by configuration, credentials, qualification evidence, and explicit authorization.
