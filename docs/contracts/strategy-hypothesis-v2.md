# Strategy Hypothesis v2 Contract

> **Implemented by Card 1 `FAMILY-KERNEL-V2`.** This contract extends, but does not replace, [`strategy-loop-v1.md`](strategy-loop-v1.md). Existing V1 bytes, IDs, rows, and lineage remain valid and are never recomputed as V2.

## Purpose

`strategy-hypothesis-v2` selects one tracked strategy family and version with finite plain-JSON parameters. It is research intent, not executable code, credentials, order configuration, accounting truth, or Live authorization.

## Canonical document

The UTF-8 JSON object has exactly these fields and must use sorted-key compact canonical encoding with one trailing newline:

```json
{
  "bar_type": "BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",
  "based_on_verdict_id": null,
  "falsification": "non-empty falsification condition",
  "family_version": "lookback-momentum-long-flat-v1",
  "instrument_id": "BTCUSDT-PERP.BINANCE",
  "parameters": {
    "entry_threshold": 0.01,
    "lookback_bars": 24
  },
  "parent_strategy_id": null,
  "schema_version": "strategy-hypothesis-v2",
  "strategy_family": "lookback-momentum-long-flat",
  "thesis": "non-empty thesis"
}
```

The current production registry contains `lookback-momentum-long-flat` at `lookback-momentum-long-flat-v1`. A new family or version is accepted only after it is added to tracked code with a parameter validator and golden-vector tests; an artifact cannot inject a callable or import path.

## Validation

- The object has exactly the fields above; duplicate JSON keys and non-finite values are rejected.
- `instrument_id` and `bar_type` must be in the currently approved universe.
- `strategy_family` and `family_version` must resolve to the same tracked registry entry.
- `parameters` must be a plain JSON object accepted by that family validator.
- Credentials, code/import paths, quantity, leverage, order, fee, Funding policy, PnL, and accounting fields are forbidden recursively.
- `parent_strategy_id` and `based_on_verdict_id` are both null for a root, or both lowercase SHA-256 IDs for a child.
- `thesis` and `falsification` are non-empty strings.

## Identities

`strategy_id` is SHA-256 over canonical JSON containing:

```text
bar_type
family_version
identity_schema = strategy-id-v2
instrument_id
normalized parameters
strategy_family
```

`hypothesis_id` is SHA-256 over the exact canonical hypothesis bytes. Thesis, falsification, and lineage therefore alter `hypothesis_id` but not the execution-equivalent `strategy_id`; family version or normalized parameters alter both execution identity and any later experiment identity.

## Compatibility and ledger safety

- `strategy-hypothesis-v1` remains readable and retains its original `strategy-id-v1` calculation.
- Legacy strategy rows migrate transactionally to `parameters_json`, the tracked legacy family version, and `identity_schema=strategy-id-v1` while retaining original IDs and foreign keys.
- New V2 rows use `identity_schema=strategy-id-v2`.
- No migration may reset the ledger or rewrite V1 IDs.
