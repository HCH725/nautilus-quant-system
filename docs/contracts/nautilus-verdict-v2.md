# Nautilus verdict V2 contract

The robustness aggregate uses schema `nautilus-verdict-v2`. It is canonical
UTF-8 sorted-key finite JSON with a trailing newline. The
`robustness_verdict_id` is SHA-256 over the canonical document without that ID.

The artifact includes complete strategy/candidate/experiment/data/policy/
engine/runtime/code/evaluation-context identity, the canonical policy document,
all bounded cell results, the frozen `matrix_shape` (`window_schemes`,
`maximum_windows_per_scheme`, and `stress_scenarios`), worst-cell evidence,
Funding truth, claimability, reason codes, and `DSR`/`PBO: NOT_MODELED` status.
The policy document hashes to `policy_id`; the loader derives the required matrix
shape from that policy preimage rather than trusting the aggregate's shape.
Legacy artifacts without the policy document are accepted only when `policy_id`
matches the repository's frozen policy, whose shape is then authoritative. A
window, tier, cost policy, or identity change therefore produces a new artifact
and cannot reuse the old verdict.

Each formal `nautilus-cost-policy-v1` identity is SHA-256 over its canonical
policy document. The producer and loader reject caller-supplied or persisted
identities that do not match those bytes. Persisted formal cell reuse decodes
each candidate verdict through the candidate loader and requires its cost-policy
identity and modeled slippage status to match the aggregate cell.

Technical and economic evidence are orthogonal:

| evidence | action |
| --- | --- |
| incomplete matrix, technical cell error, or required evidence/modeling gap | `FIX_TECHNICAL` with `status=TECHNICAL_INVALID` and `technical_status=ERROR` |
| complete technical run with failed economic threshold | `MUTATE`, `NEW_FAMILY`, or `KILL` |
| complete, technically valid, economically valid, fully modeled evidence | `ADVANCE` |

`DSR` and `PBO` remain explicitly `NOT_MODELED`, but that status alone does
not block `ADVANCE`; only an unmodeled required cost or stress does.

`ADVANCE` is the only action that can enter the next tier. The loader derives
matrix completeness, technical/economic status, reason codes, Funding counts,
and claimability from the policy-bound cells and rejects any conflicting
top-level value.
