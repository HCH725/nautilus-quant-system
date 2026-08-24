# Strategy action V1 contract

`strategy-action-v1` is the narrow deterministic action envelope emitted from
an identity-bound robustness verdict. It repeats the complete execution
identity and references `robustness_verdict_id`.

Allowed actions are `ADVANCE`, `HOLD`, `MUTATE`, `NEW_FAMILY`, `KILL`, and
`FIX_TECHNICAL`.
The artifact also names `source_tier`, `source_verdict_id`, consumed reason
codes, changed dimension, campaign/generation, child strategy ID, and child
hypothesis ID. Both child IDs are required for `MUTATE`/`NEW_FAMILY`, absent
for all other actions, and bind through the append-only robustness lineage
relation to the canonical child hypothesis artifact.
`ADVANCE` is fail-closed: it is invalid when the matrix is incomplete, any
cell is technical-invalid, an economic gate fails, or a required cost/stress
is unmodeled. DSR/PBO remain explicitly `NOT_MODELED` and do not alone block
the action. The artifact is canonical JSON and
its `action_id` is the SHA-256 of the canonical content without `action_id`.
