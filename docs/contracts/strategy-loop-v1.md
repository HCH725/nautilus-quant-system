# Strategy Loop v1 contracts

## Scope and trust boundary

This contract defines canonical hypothesis identity, isolated PyBroker handoff, Nautilus-owned evaluation, lineage, immutable evidence, and the derived funnel. It never accepts executable strategy payloads or writes canonical market data.

Pipeline placement: this contract captures the **Loop A → Loop B entry** (one hypothesis = one Loop-A thesis/family branch) and the **Gate → Nautilus** step for that hypothesis. In the full Two Loops model the same hypothesis is expanded by `strategy-campaign-v1` (Loop B) into N deterministic candidates with attrition; isolated single-hypothesis runs remain valid but do not imply one-to-one Hermes → PyBroker → Nautilus without inner Loop B attrition and Gate.

## `strategy-hypothesis-v1`

A hypothesis is a UTF-8 JSON object encoded with lexicographically sorted keys, compact `,` and `:` separators, no NaN or Infinity, and exactly one trailing LF. Duplicate keys and any other byte encoding are rejected.

The exact top-level shape is:

```json
{
  "bar_type": "BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",
  "based_on_verdict_id": null,
  "falsification": "No fills, non-positive net result after fees and Funding, or unstable official-window result",
  "instrument_id": "BTCUSDT-PERP.BINANCE",
  "parameters": {
    "entry_threshold": 0.0,
    "lookback_bars": 24
  },
  "parent_strategy_id": null,
  "schema_version": "strategy-hypothesis-v1",
  "strategy_family": "lookback-momentum-long-flat",
  "thesis": "Positive 24-hour momentum persists into the next event"
}
```

Only `lookback-momentum-long-flat` is supported. `lookback_bars` is an integer from 1 through 8,760; booleans are not integers for this contract. `entry_threshold` is a finite, non-negative JSON number and is canonically represented as a JSON float. Thesis and falsification are non-empty strings.

The instrument and bar type shown above are the only v1 values. Parameter trees may not carry code, imports, executable serialization, credentials, quantity, leverage, order semantics, fees, Funding policy, PnL, or accounting claims. The parameter object has exactly `entry_threshold` and `lookback_bars`.

A root has both lineage fields `null`. A child has both populated with 64-character lowercase SHA-256 IDs. When recorded, the ledger requires the pair to identify an existing parent strategy and a verdict issued for that strategy.

`strategy_id` is SHA-256 over the canonical JSON bytes, including the trailing LF, of only:

```json
{
  "bar_type": "BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",
  "instrument_id": "BTCUSDT-PERP.BINANCE",
  "parameters": {
    "entry_threshold": 0.0,
    "lookback_bars": 24
  },
  "strategy_family": "lookback-momentum-long-flat"
}
```

`hypothesis_id` is SHA-256 over the complete canonical hypothesis bytes. IDs are not embedded in the hypothesis, avoiding a self-hash cycle.

## `strategy-loop-policy-v1`

The tracked policy is canonical JSON and is outside hypothesis control. Decimal balances and quantities are strings so their intended precision is explicit.

- `historical_start` equals `config/market_data.json.backtest_start`.
- `official_only_window_start` is `first_official_funding_observation`; the boundary is discovered from canonical Funding data, not hard-coded as a timestamp.
- Signals are decided at bar close and may execute no earlier than the next bar event.
- Quantity is fixed at `0.001` BTC and leverage is disabled.
- Fee truth comes from Nautilus instrument metadata.
- Slippage is explicitly `unmodeled`; OHLCV bars are not treated as empirical spread evidence.
- Starting balance is `10000.00` USDT and decision behavior is identified by `strategy-loop-decision-v1`.

The SHA-256 of the canonical policy file is its `policy_id` when constructing an experiment identity.

## SQLite ledger

The stdlib `sqlite3` ledger enables foreign keys and uses transactions and uniqueness constraints. It stores artifact paths and SHA-256 hashes, not signal payloads.

- `strategies` deduplicates strategy versions by `strategy_id`.
- `hypotheses` records the complete hypothesis artifact identity and enforced lineage pair.
- `experiments` is content-addressed by strategy, data source, policy, engine, and runtime identities. Reusing all five identities reuses the same `experiment_id`.
- `verdicts` retains either `SUCCESS` or `REJECTION` with a deterministic reason and artifact identity.
- `errors` retains technical failures separately so they are never interpreted as bad strategies.

All record tables reject SQL `UPDATE` and `DELETE`. There are no mutable lifecycle, current-state, or current-stage columns. Funnel counts are projections over immutable rows: distinct proposed/contract-valid/experimented strategy versions, successful and rejected verdicts, and experiments with technical errors.

## Evaluation and verdict

PyBroker receives only a validated hypothesis, canonical Catalog path, and non-canonical output path. Its candidate remains `pybroker-candidate-v1`, records provisional metrics separately, and may express only LONG/FLAT intents decided at bar close. Nautilus owns fixed quantity, next-event execution, fills, fees, Funding, positions, accounting, and the final flatten.

`nautilus-verdict-v1` records source identity, actual evaluation bounds, runtime/code identity, execution assumptions, fills, fees, Funding evidence, reconciled account delta, reason codes, and a canonical result hash. Technical failures produce `strategy-loop-error-v1`; they never produce `REVISE`. V1 decisions are only `REVISE` or `RETAIN_FOR_RESEARCH`, never `PROMOTE`.

Configured history begins at the policy UTC boundary. Funding events use canonical observations; when Nautilus cannot prioritize Funding before a same-time bar, settlement is scheduled one nanosecond earlier against strictly pre-boundary evidence while the verdict retains the official timestamp. Modeled Funding, missing marks, or unmodeled slippage force `performance_claimable=false`.

## Feedback, reuse, and funnel

`strategy-feedback-v1` names immutable hypothesis, strategy, experiment, parent strategy, source verdict, result/error, status, and reason codes. A completed experiment is reused only after canonical feedback and every referenced artifact are read back and hash-verified.

The JSON/Markdown funnel derives seven fixed labels from SQLite: Proposed, Contract valid, PyBroker completed, Research screened, Nautilus replayed, Robustness passed, and Promotion eligible. Technical errors remain separate from strategy rejection; stages 6–7 are zero in v1. Counts are by unique strategy version, including Funding-truth and performance-claimability evidence, so engine reruns do not inflate the report.
