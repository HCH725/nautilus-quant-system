# Strategy Evidence Envelope v2 Contract

> **Card 1 control-plane contract.** Evidence remains distributed across canonical artifacts and append-only ledger rows; this document defines how identities must resolve together. It does not invent a mutable envelope database row or declare later tiers implemented.

## Permanent rules

1. A tier cannot declare PASS when an identity required for that tier is missing.
2. A verdict cannot be reused when strategy/family/kernel, data, policy, evaluation context, runtime, or environment identity changes.
3. Campaign membership is lineage/context only and does not change execution identity.
4. A field that does not apply at the current tier is `N/A`, not `0`, an empty placeholder, or model-generated content.
5. Artifact truth is the canonical bytes plus SHA-256 read back from immutable storage; read-only projections cannot create evidence.
6. Existing V1 identities and rows remain historical facts and are never recomputed to fit V2.

## Identity vocabulary

| Identity | Card 1 source of truth |
|---|---|
| `strategy_id` | `strategy-hypothesis-v2` normalized execution identity |
| `family_id`, `family_version` | tracked family registry plus hypothesis/candidate |
| `kernel_version`, `kernel_hash` | tracked family-kernel manifest plus Candidate v2 |
| `data_snapshot_id`, `data_as_of` | Candidate v2 source identity; snapshot equals source digest |
| `code_commit` | controller evaluation-context preimage and Nautilus verdict |
| `screen_policy_id` | canonical strategy-loop policy digest |
| `robustness_policy_id` | `N/A` until Card 3 |
| `cost_policy_id` | currently resolved through the canonical strategy-loop policy/evaluator identity; separately versioned stress policy begins in Card 3 |
| `risk_policy_id` | `N/A` until the independent Risk & Execution Policy in Card 4 |
| `evaluation_context_id` | SHA-256 over strategy, family/kernel, code, data, screen policy, engine, and runtime identities |
| `runtime_identity`, `environment_identity` | root runtime digest and isolated PyBroker environment digest |
| `artifact_hash` | SHA-256 of exact canonical artifact bytes |

## Card 1 requirement matrix

`R` = required, `N/A` = not applicable at this tier, `Resolve` = required and resolved through a bound parent identity/artifact.

| Identity | Hypothesis v2 | Candidate v2 | Parity result | Nautilus historical verdict |
|---|---:|---:|---:|---:|
| strategy ID | R | Resolve | Resolve | R |
| family/version | R | R | Resolve from candidate | Resolve |
| kernel version/hash | Resolve from tracked registry | R | Resolve from candidate and recomputation | Resolve |
| data snapshot/as-of | N/A | R | R/Resolve | R/Resolve |
| code commit | N/A | Resolve via evaluation context | Resolve via evaluation context | R |
| screen policy | N/A | Resolve via evaluation context | Resolve via evaluation context | Resolve via experiment |
| robustness policy | N/A | N/A | N/A | N/A |
| cost policy | N/A | N/A | N/A | Resolve via evaluator policy |
| risk policy | N/A | N/A | N/A | N/A |
| evaluation context | N/A | R | R | Resolve via experiment/parity |
| runtime/environment | N/A | R | Resolve via evaluation context | R/Resolve |
| artifact hash | R | R | R | R |

Later tiers must extend this matrix under new versioned contracts rather than weakening Card 1 fields.

## Evaluation and experiment identity

For a V2 run, `evaluation_context_id` binds:

```text
schema_version = evaluation-context-v1
strategy_id
family_id + family_version
kernel_version + kernel_hash
code_commit
data_source_id
screen_policy_id
engine_id
runtime_id
```

The V2 experiment identity then binds strategy, data source, policy, runtime, and an engine identity that includes the evaluation context. Therefore a change to kernel code/identity, canonical data, screen/evaluator policy, code commit, evaluation context, or runtime/environment creates a new experiment identity and prevents verdict reuse.

## Current implementation boundary

Card 1 implements Hypothesis v2, Candidate v2, shared deterministic family kernel, evaluation context, formal Signal Parity Gate, append-only parity evidence, V1-safe strategy-ledger migration, and Nautilus consumption of recomputed signals.

The following remain planned, not implemented by this contract: campaign trial census, substantive provisional screen policy, robustness/multiple-testing verdicts, independent Risk & Execution Policy, Shadow/Paper, Binance Demo/Testnet evidence, promotion projection, runtime qualification, and all real-funds Live capability.
