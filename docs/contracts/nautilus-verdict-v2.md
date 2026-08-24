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

Technical and economic evidence are orthogonal:

| evidence | action |
| --- | --- |
| incomplete matrix or technical cell error | `FIX_TECHNICAL` |
| complete technical run with failed economic threshold | `MUTATE`, `NEW_FAMILY`, or `KILL` |
| complete pass with a required cost/stress not modeled | `MUTATE`/`HOLD` |
| complete, technically valid, economically valid, fully modeled evidence | `ADVANCE` |

`DSR` and `PBO` remain explicitly `NOT_MODELED`, but that status alone does
not block `ADVANCE`; only an unmodeled required cost or stress does.

`ADVANCE` is the only action that can enter the next tier. The loader derives
matrix completeness, technical/economic status, reason codes, Funding counts,
and claimability from the policy-bound cells and rejects any conflicting
top-level value.
