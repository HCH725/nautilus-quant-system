# PyBroker Candidate v2 Contract

> **Implemented by Card 1 `FAMILY-KERNEL-V2`.** A Candidate v2 is provisional research output. It is never fills, positions, PnL, accounting truth, promotion evidence, or Live authorization.

## Canonical document

The UTF-8 JSON object has exactly these top-level fields and must use sorted-key compact canonical encoding with one trailing newline:

```text
bar_type
evaluation_context_id
instrument_id
runtime
schema_version = pybroker-candidate-v2
signals
source
strategy
truth_status = provisional
```

### `runtime`

Exactly:

```text
environment_id      lowercase SHA-256
pybroker_version    non-empty string
python_version      non-empty string
seed                non-negative integer
```

### `source`

Exactly:

```text
data_as_of_ns       equals last_ts_event_ns
data_snapshot_id    equals sha256
first_ts_event_ns
last_ts_event_ns
row_count           positive integer
sha256              lowercase SHA-256 of the identified source bytes
```

The source interval is non-reversed. `bar_type` must identify `instrument_id`.

### `strategy`

Exactly:

```text
decision_timing = bar-close; effective no earlier than next event
family_id
family_version
kernel_hash         lowercase SHA-256
kernel_version
parameters          finite plain-JSON object
```

The parameters cannot carry code/import paths, credentials, quantity, leverage, order type, PnL, or accounting fields.

### `signals[]`

Every eligible completed bar after warm-up produces one ordered signal object with exactly:

```text
family_id
family_version
kernel_hash
kernel_version
reason
score                canonical finite decimal string
signal_id            lowercase SHA-256
target_intent        LONG | FLAT
ts_event_ns          positive, strictly increasing, inside source interval
```

Signal family/kernel identity must equal the enclosing strategy identity. `signal_id` is recomputed from canonical identity-bearing fields:

```text
family_id, family_version, kernel_hash, kernel_version,
normalized parameters, reason, score, target_intent, ts_event_ns
```

Any mismatch is rejected before the formal Signal Parity Gate.

## Identity and trust

`candidate_id` is SHA-256 over the exact canonical Candidate v2 bytes. `evaluation_context_id`, `environment_id`, source identity, family/kernel identity, parameters, and all signal bytes are therefore bound by the artifact hash.

PyBroker may use the tracked pure family kernel to create this artifact, but Nautilus does not trust the artifact's signal sequence. A formally valid Candidate v2 must pass [`signal-parity-gate-v1.md`](signal-parity-gate-v1.md); accounting consumes only the independently recomputed sequence.

## V1 compatibility

Schema dispatch preserves the exact `pybroker-candidate-v1` validator, canonical bytes, IDs, and historical replay behavior. Existing V1 artifacts are not upgraded or rewritten as V2.
