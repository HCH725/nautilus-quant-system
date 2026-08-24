# Nautilus verdict V2 contract

The robustness aggregate uses schema `nautilus-verdict-v2`. It is canonical
UTF-8 sorted-key finite JSON with a trailing newline. The
`robustness_verdict_id` is SHA-256 over the canonical document without that ID.

The artifact includes complete strategy/candidate/experiment/data/policy/
engine/runtime/code/evaluation-context identity, all bounded cell results,
worst-cell evidence, Funding truth, claimability, reason codes, and
`DSR`/`PBO: NOT_MODELED` status. A window, tier, cost policy, or identity change
therefore produces a new artifact and cannot reuse the old verdict.

Technical and economic evidence are orthogonal:

| evidence | action |
| --- | --- |
| incomplete matrix or technical cell error | `FIX_TECHNICAL` |
| complete technical run with failed economic threshold | `MUTATE`, `NEW_FAMILY`, or `KILL` |
| complete pass with a required cost/stress not modeled | `MUTATE`/`HOLD` |
| complete, technically valid, economically valid, fully modeled evidence | `ADVANCE` |

`DSR` and `PBO` remain explicitly `NOT_MODELED`, but that status alone does
not block `ADVANCE`; only an unmodeled required cost or stress does.

`ADVANCE` is the only action that can enter the next tier, and the loader
rejects it unless every fail-closed requirement is satisfied.
