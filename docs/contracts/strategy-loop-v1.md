# Strategy Loop v1 Contract

## Scope and trust boundary

This contract defines canonical hypothesis identity, Nautilus-native candidate specification, historical Nautilus evaluation, immutable evidence, feedback, lineage, and funnel projection. It never accepts executable strategy payloads and never writes canonical market data.

## Hypothesis

`strategy-hypothesis-v1` and `strategy-hypothesis-v2` are canonical UTF-8 JSON with sorted keys, compact separators, finite numbers, no duplicate keys, and one trailing LF. Hypotheses contain thesis, falsification, approved family identity, ordinary parameters, instrument/bar identity, and optional parent/verdict lineage.

Parameter trees cannot contain code, imports, executable serialization, credentials, quantity, leverage, order semantics, fee/funding policy, PnL, or accounting claims.

`strategy_id` identifies normalized executable strategy intent. `hypothesis_id` is the SHA-256 of the complete canonical hypothesis bytes.

## Candidate

A validated hypothesis is deterministically converted to `strategy-candidate-v1` from the canonical catalog. The candidate is a specification, not a backtest result. It binds family/kernel/parameters, source digest and bounds, evaluation context, instrument/bar identity, and root runtime versions. Its `candidate_id` is the SHA-256 of canonical candidate bytes. See [`strategy-candidate-v1.md`](strategy-candidate-v1.md).

Nautilus derives decisions directly from canonical bars through the tracked strategy-family kernel. There is no external signal payload or cross-engine handoff.

## Strategy-loop policy

The tracked policy is outside hypothesis control. It binds the historical start, execution timing, quantity, leverage posture, fee/funding assumptions, slippage status, starting balance, and decision-policy version. The policy file's canonical SHA-256 participates in experiment identity.

Signals are decided on completed bars and act no earlier than the next event. Fee truth comes from Nautilus instrument metadata unless a separately versioned source is explicitly configured. Funding uses canonical observations. Unmodeled slippage or mixed funding truth prevents fully claimable performance.

## Experiment identity

An experiment is bound to strategy, canonical data source/snapshot, policy, engine, root runtime, and data-as-of. V2 evaluation context additionally binds family/kernel, code commit, relevant policy IDs, and runtime identity. Any material change creates a distinct identity and prevents reuse.

## SQLite ledger

The append-only ledger records:

- strategy versions and hypotheses;
- experiments and canonical source identities;
- candidate/screen/Nautilus stage results;
- formal verdicts and technical errors;
- campaign membership/trials;
- robustness results and lineage.

Record tables reject mutable lifecycle rewriting. Verdict and error evidence are mutually exclusive for one terminal experiment. Reuse requires artifact readback and hash verification.

## Historical evaluation

The formal historical evaluator is NautilusTrader. It owns orders/fills, fees, funding, positions, balances, PnL, accounting reconciliation, and terminal flatten behavior.

`nautilus-verdict-v1` binds candidate, strategy/hypothesis/experiment, source, runtime/code, execution assumptions, fills, costs, funding truth, reconciled account delta, reason codes, and result hash. Technical failures emit error evidence and never masquerade as an economic strategy rejection.

## Feedback and funnel

`strategy-feedback-v1` names immutable strategy/hypothesis/experiment identity, lineage, verdict or error, status, and reason codes.

The fixed funnel labels are:

```text
Proposed
Contract valid
Candidate specified
Research screened
Nautilus evaluated
Robustness passed
Promotion eligible
```

The funnel is a read-only projection of immutable evidence. It does not create admission truth.
