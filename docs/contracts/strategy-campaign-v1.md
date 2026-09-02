# Strategy Campaign V1 Contract

## Scope and truth boundary

`strategy-campaign-v1` is the bounded, deterministic **Loop B — PyBroker Experiment & Attrition Loop** contract (high-throughput, no LLM per candidate; survivors only → Gate → Nautilus high-fidelity).
PyBroker is provisional research only. Nautilus remains the sole authoritative
owner of fills, fees, Funding, PnL, accounting, and final historical verdicts.
The campaign screen cannot produce a Nautilus verdict, a robustness verdict, a
Paper result, a Demo result, or a promotion decision. Robustness and promotion
funnel stages remain projections of work that is not implemented by Card 2.

Technical invalidity is orthogonal to strategy rejection. A malformed result,
runtime failure, data inconsistency, or other technical error is recorded as
`TECHNICAL_INVALID`; it is never converted into `SCREEN_REJECTED`. A candidate
with `SCREEN_REJECTED` evidence is preserved but never calls
`run_candidate_backtest`.

## `research-result-v2`

The isolated research adapter emits one canonical UTF-8 JSON object with exactly
these fields:

```json
{
  "candidate_id": "<lowercase sha256>",
  "provisional_metrics": {
    "max_drawdown": 0.0,
    "signal_count": 0,
    "total_return": 0.0,
    "trade_count": 0,
    "turnover": 0.0
  },
  "schema_version": "research-result-v2",
  "truth_status": "provisional"
}
```

All five metrics must be finite. `signal_count` must exactly equal the length of
the validated candidate `signals` array; the candidate and recomputed counts in
`signal-parity-result-v1` must match that same value before Nautilus may run.
The units and formulas are:

- `trade_count`: count of completed PyBroker trades.
- `signal_count`: count of eligible completed-bar decisions emitted for the
  candidate.
- `total_return`: final provisional equity divided by initial provisional
  equity, minus one.
- `max_drawdown`: the maximum, over the provisional equity series, of
  `(running_peak_equity - equity) / running_peak_equity`; it is a non-negative
  magnitude.
- `turnover`: sum of `abs(shares * fill_price)` for provisional orders divided
  by initial provisional equity. This is a notional traded-value ratio, with no
  fees or Funding; it is a screen metric, not formal accounting.

## `research-screen-result-v1`

The raw adapter output remains unchanged at `research-result-v2-raw.json`.
Applying the frozen policy produces a separate canonical
`research-screen-result-v1.json` artifact with the same candidate ID and bounded
provisional metrics plus exactly `screen_outcome`, `screen_policy_id`, and
`screen_reason_codes`. It also embeds the complete canonical `screen_policy`
object whose SHA-256 must equal `screen_policy_id`, so a strict reader can
recompute the outcome and reasons without consulting mutable runtime config.
Its schema version is `research-screen-result-v1`.
`PASSED` requires an empty reason list; `SCREEN_REJECTED` requires one or more
ordered reason codes. The screen artifact never claims to be raw adapter output.

## Frozen screen policy

Before campaign results are read, the canonical
`config/strategy_research_policy.json` is hashed. Its SHA-256 is the
`screen_policy_id`, and that identity participates in the V2 evaluation context
and experiment identity. Changing a threshold therefore invalidates reuse.

The initial human-readable, conservative defaults are:

| Policy field | V1 default |
| --- | ---: |
| `minimum_signal_count` | `1` |
| `minimum_trade_count` | `1` |
| `max_provisional_drawdown` | `0.25` |
| `max_turnover` | `4.0` |
| `reject_no_signal` | `true` |

These are provisional screen thresholds chosen before observing campaign
results. They are neither alpha criteria nor promotion criteria. Rejection
reason codes are deterministic and can include `NO_SIGNALS`,
`MINIMUM_TRADE_COUNT`, `MINIMUM_SIGNAL_COUNT`,
`MAX_PROVISIONAL_DRAWDOWN_EXCEEDED`, and `TURNOVER_CEILING_EXCEEDED`.

## `strategy-campaign-v1`

The canonical campaign object contains exactly:

```text
schema_version = strategy-campaign-v1
family_id, family_version
search_space
approved_instruments, approved_bar_types
parameter_search_policy_id
seed
data_as_of_ns
generation_budget, maximum_candidates
screen_policy_id
```

The campaign ID is the SHA-256 of the canonical JSON object. Search-space keys
are sorted lexicographically, values retain their declared order, and
`itertools.product` emits the Cartesian product in that order. Budget is
checked before data access, ledger initialization, subprocess launch, or
executor invocation. V1 accepts one approved instrument and one matching bar
type, and both policy IDs are lowercase SHA-256 content IDs. Integer control
fields (`seed`, `data_as_of_ns`, `generation_budget`, and `maximum_candidates`)
must fit the non-negative signed 64-bit range used by the immutable SQLite
ledger.

Each generated attempt is identified before execution by the existing canonical
`strategy_id`, derived from family and family version, instrument, bar type,
identity schema, and normalized parameter values. The campaign ID is not part of
experiment identity. A PyBroker `candidate_id` exists only after the research
artifact is emitted. Campaign identity and membership are stored separately from
experiment identity and execution evidence. Reusing a strategy in another
campaign adds a membership/trial row and does not create a new experiment.

### Execution identity and terminal trust

A V2 execution binds the stable `hash → catalog tail → hash` data snapshot,
`data_as_of_ns`, accounting and screen policies, engine, code commit, and the
actual isolated research runtime. The controller recomputes that identity after
acquiring the per-experiment lock. The PyBroker process independently hashes its
own interpreter and locked dependency surface; its attestation must equal the
controller challenge and the candidate runtime identity. The controller then
revalidates the complete prepared identity after candidate validation and again
after Nautilus returns, before terminal evidence is committed. Any drift fails
closed as technical invalidity and cannot be reused under the old identity.

The per-experiment lock is the launch authority. A waiter that observes a
terminal result produced by the lock holder records membership as
`REUSED_EXECUTION`, with `execution_started = false`; stale non-terminal ledger
evidence never relaunches work. Verdict and error rows are mutually exclusive
for one experiment, and readers reject legacy contradictory terminal evidence.

Each generated attempt has exactly one immutable terminal status:

```text
DUPLICATE_SUPPRESSED | TECHNICAL_INVALID | SCREEN_REJECTED | SURVIVED
```

`execution_started` is stored independently of terminal status. Duplicate
suppression is membership-only and never overwrites the single execution row.
The bounded `strategy-cohort-summary-v1` reports generated, deduped, technical,
rejected, and surviving counts, status counts, family counts, IDs, data-as-of,
policy IDs, budget, and top reason codes. Its counts reconcile to the immutable
trial census.

## Predeclared Card 2 family

Card 2 adds this code-owned family before observing campaign results:

- family: `close-vs-sma-mean-reversion-long-flat`
- version: `close-vs-sma-mean-reversion-long-flat-v1`
- parameters: `window_bars` integer in `2..8760`; `discount_threshold` finite
  non-negative float
- score: current completed-bar close divided by the arithmetic mean of the
  completed-bar warmup-window closes, minus one, canonically rounded through the
  shared kernel
- target: `LONG` when score is below `-discount_threshold`, otherwise `FLAT`
- reasons: `CLOSE_BELOW_SMA_DISCOUNT_THRESHOLD` or
  `CLOSE_AT_OR_ABOVE_SMA_DISCOUNT_THRESHOLD`
- thesis: a completed-bar close materially below its short-window SMA mean
  reverts after the next event
- falsification: no activity, excessive provisional drawdown or turnover, or
  failure under later authoritative evaluation

Golden vectors and the family/version identity are code-owned in
`tests/test_strategy_families.py` and the shared family registry. This family
does not introduce a plugin mechanism or alter V1 contracts.
