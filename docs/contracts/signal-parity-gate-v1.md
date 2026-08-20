# Signal Parity Gate v1 Contract

> **Implemented by Card 1 `FAMILY-KERNEL-V2`.** This is the trust boundary between provisional Candidate v2 signals and Nautilus historical accounting.

## Input and independent recomputation

The gate accepts only a canonical, formally valid `pybroker-candidate-v2` and the canonical Nautilus catalog path. It:

1. reloads the candidate-identified closed bars from the catalog;
2. verifies source row count, interval, and digest against the Candidate v2 source identity;
3. instantiates an independent `IncrementalFamilyEvaluator` from the tracked family ID/version and parameters;
4. recomputes every eligible decision in strict bar order; and
5. compares the complete ordered sequence to Candidate v2.

The comparison is exact across sequence length/order and all fields:

```text
signal_id
ts_event_ns
score
target_intent
reason
family_id
family_version
kernel_version
kernel_hash
```

Duplicate or out-of-order bars, unsupported family/version, source mismatch, kernel error, or non-finite data fail closed.

## Result artifact

Canonical `signal-parity-result-v1` contains exactly the following semantics:

```text
candidate_id
candidate_signal_count
detail
mismatch_index
outcome                    PASS | ERROR
reason_code
recomputed_signal_count
recomputed_signals_sha256
required_action            null | FIX_TECHNICAL
schema_version              signal-parity-result-v1
```

Valid outcome/action pairs are:

| Outcome | Reason | Required action |
|---|---|---|
| `PASS` | `SIGNAL_PARITY_MATCH` | `null` |
| `ERROR` | `SIGNAL_PARITY_MISMATCH` | `FIX_TECHNICAL` |
| `ERROR` | `SIGNAL_PARITY_RECOMPUTE_FAILED` | `FIX_TECHNICAL` |

A Candidate contract failure may be recorded earlier as `RESEARCH_CANDIDATE_INVALID`; it is likewise technical and cannot become a strategy economic rejection.

## Accounting gate

- `ERROR` is technical evidence, never an economic reject.
- On `ERROR`, the controller records `FIX_TECHNICAL`, does not mark Research screen PASS, and does not invoke the Nautilus accounting path.
- On `PASS`, Nautilus receives only the gate's independently recomputed `FamilyDecision` sequence. It does not replay the untrusted Candidate v2 sequence.
- The accounting consumer verifies the candidate ID and parity artifact hash before constructing `BacktestEngine`.

## Durable evidence

The append-only `signal_parity_results` row binds:

```text
parity_result_id
experiment_id
candidate_id
evaluation_context_id
data_snapshot_id
outcome
reason_code
required_action
artifact_path
artifact_sha256
```

`parity_result_id` is content-addressed from the row semantics. `UPDATE` and `DELETE` are forbidden by immutable ledger triggers. The existing V1 `stage_results` schema and rows remain unchanged; parity evidence is not backfilled into historical V1 experiments.
