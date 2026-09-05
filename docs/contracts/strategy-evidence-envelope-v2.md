# Strategy Evidence Envelope v2 Contract

This control-plane contract defines how canonical evidence identities resolve across Nautilus-only research, robustness, and prospective runtime tiers. Evidence remains distributed across canonical artifacts and append-only ledger rows; no mutable envelope service is introduced.

## Permanent rules

1. A tier cannot declare PASS when an identity required for that tier is missing.
2. A verdict cannot be reused when strategy/family/kernel, data, policy, evaluation context, runtime, engine, or code identity changes.
3. Campaign membership is lineage/context only and does not alter execution identity.
4. Non-applicable fields are `N/A`, not fabricated zero values.
5. Artifact truth is exact canonical bytes plus verified SHA-256.
6. Existing immutable identities are not silently reidentified.

## Identity vocabulary

| Identity | Source of truth |
|---|---|
| `strategy_id` | normalized hypothesis strategy intent |
| `hypothesis_id` | canonical hypothesis bytes |
| `family_id`, `family_version` | tracked family registry + hypothesis/candidate |
| `kernel_version`, `kernel_hash` | tracked deterministic family kernel |
| `candidate_id` | canonical `strategy-candidate-v1` bytes |
| `data_snapshot_id`, `data_as_of_ns` | canonical candidate/source identity |
| `code_commit` | controller context + Nautilus verdict |
| policy IDs | canonical frozen policy bytes |
| `evaluation_context_id` | strategy/family/kernel/code/data/policy/engine/runtime preimage |
| `runtime_id` | Nautilus-only root runtime identity |
| `experiment_id` | strategy/data/policy/engine/runtime identity |
| artifact hash | SHA-256 of exact immutable artifact bytes |

## Requirement matrix

| Identity | Hypothesis | Candidate | Nautilus historical | Robustness | Paper/runtime |
|---|---:|---:|---:|---:|---:|
| strategy/hypothesis | R | Resolve | R | Resolve | Resolve |
| family/version | R | R | Resolve | Resolve | Resolve |
| kernel version/hash | Resolve | R | Resolve | Resolve | Resolve |
| data snapshot/as-of | N/A | R | R | R | prospective boundary |
| code commit | N/A | Resolve | R | Resolve | Resolve |
| relevant policy IDs | N/A/Resolve | Resolve | R | R | R |
| evaluation context | N/A | R | Resolve | R | Resolve |
| root runtime | N/A | R | R | Resolve | R |
| artifact hash | R | R | R | R | R |

## Evaluation context

The evaluation context binds the applicable strategy/family/kernel identity, code commit, canonical data identity, policy IDs, engine ID, and root runtime ID. Any material change creates a new context/experiment rather than mutating old evidence.

## Current implementation boundary

The repository implements hypothesis/candidate identity, shared deterministic family kernels, Nautilus historical evaluation, campaign census, append-only ledger evidence, Nautilus robustness evidence, strategy freeze, and Shadow/Paper runtime evidence. Venue qualification and real-capital authorization remain separately gated.
