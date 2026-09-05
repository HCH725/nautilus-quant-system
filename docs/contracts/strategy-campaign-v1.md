# Strategy Campaign v1 Contract

## Purpose

`strategy-campaign-v1` freezes a bounded deterministic parameter search for one tracked strategy family. It is the high-throughput machine-compute loop underneath one Hermes thesis/family decision; it does not call an LLM per trial. Every executable trial is evaluated through the Nautilus-native strategy loop.

## Campaign identity

The canonical object binds:

```text
schema_version = strategy-campaign-v1
family_id + family_version
search_space
approved_instruments + approved_bar_types
parameter_search_policy_id
seed
data_as_of_ns
generation_budget + maximum_candidates
screen_policy_id
```

Campaign ID is SHA-256 of canonical JSON. Search-space keys are deterministic, values retain their declared order, and Cartesian expansion is bounded before execution. Integer control fields fit the non-negative signed 64-bit SQLite range.

## Trial execution

Each valid parameter tuple produces the canonical strategy identity before execution. Campaign identity is membership/context and does not alter the experiment identity. A strategy reused by another campaign may reuse the same already-validated experiment when all execution identities and immutable artifacts match.

For a new execution the controller:

1. freezes/rechecks the prepared data/runtime/policy identity;
2. derives `strategy-candidate-v1` from canonical data;
3. validates candidate/source identity;
4. calls the Nautilus historical evaluator;
5. records immutable stage, verdict/error, and campaign-trial evidence;
6. rechecks the prepared snapshot where required before terminal reuse.

There is no isolated second-engine runtime.

## Terminal census

Each generated attempt has one immutable terminal status:

```text
DUPLICATE_SUPPRESSED | TECHNICAL_INVALID | SCREEN_REJECTED | SURVIVED
```

`execution_started` is tracked independently. Technical invalidity never becomes strategy rejection. Duplicate suppression is membership-only and never overwrites execution evidence. A campaign summary must reconcile generated, duplicate, technical, rejected, and surviving counts to the immutable census.

## Research screen

When a screen policy is configured, it is a deterministic policy over Nautilus-native/strategy-loop evidence and is content-addressed. Screen policy is an attrition policy, not formal promotion truth. A threshold change changes policy identity and prevents incorrect reuse.

## Concurrency and drift

Per-experiment locking is execution authority. Waiters may reuse a terminal result only after artifact and identity revalidation. Non-terminal stale evidence is never silently relaunched as if it were new. Campaign preflight and data snapshot drift fail closed and remain technical evidence.

## Truth boundary

NautilusTrader remains the only historical trading/accounting engine. The campaign layer owns only deterministic expansion, trial census, membership, bounded attrition policy, and reuse coordination.
