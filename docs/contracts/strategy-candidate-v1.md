# Strategy Candidate v1 Contract

`strategy-candidate-v1` is the canonical, Nautilus-native strategy specification handed to the historical evaluator. It is plain data and contains no executable payload.

## Required shape

```json
{
  "bar_type": "<instrument bar type>",
  "evaluation_context_id": "<sha256>",
  "instrument_id": "<instrument>",
  "runtime": {
    "nautilus_trader": "<version>",
    "python_version": "<version>"
  },
  "schema_version": "strategy-candidate-v1",
  "source": {
    "data_as_of_ns": 0,
    "data_snapshot_id": "<sha256>",
    "first_ts_event_ns": 0,
    "last_ts_event_ns": 0,
    "row_count": 1,
    "sha256": "<same canonical source digest>"
  },
  "strategy": {
    "decision_timing": "bar-close; effective no earlier than next event",
    "family_id": "<tracked family>",
    "family_version": "<tracked version>",
    "kernel_hash": "<sha256>",
    "kernel_version": "<kernel version>",
    "parameters": {}
  },
  "truth_status": "provisional"
}
```

## Canonical encoding and identity

The file is UTF-8 canonical JSON with sorted keys, compact separators, finite values, and one trailing LF. `candidate_id` is SHA-256 over the exact canonical bytes. Source row count must be positive; timestamp bounds must be ordered; `data_as_of_ns` equals `last_ts_event_ns`; source snapshot ID equals the source digest.

## Trust boundary

The candidate contains only strategy intent and reproducibility identity. It does **not** contain:

- historical signals;
- fills/orders/trades;
- return, drawdown, or PnL;
- fees or funding results;
- framework objects, cache objects, pickle/joblib payloads, imports, or code;
- credentials, quantity, leverage, or order-type instructions.

Nautilus reloads canonical bars and computes decisions through the tracked strategy-family kernel. All historical accounting truth is created by Nautilus, not by the candidate.
