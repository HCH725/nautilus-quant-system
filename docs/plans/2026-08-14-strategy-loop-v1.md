# PyBroker → Nautilus → Hermes Strategy Loop v1 Implementation Plan

> **For Hermes:** Execute this plan through the dedicated Kanban board `quant-strategy-loop-20260814`. Use one goal-mode `hermes-implementer` writer followed by one independent read-only `hermes-auditor`. Do not auto-decompose, fan out writers, create phase umbrellas, or add approval placeholders.

**Goal:** Build and truly exercise one traceable two-generation research loop: Hermes hypothesis → PyBroker candidate → NautilusTrader verdict → Hermes child hypothesis.

**Architecture:** Hermes emits plain JSON hypotheses that may select only an approved strategy family and ordinary parameters. PyBroker remains an isolated, read-only provisional research frontend; NautilusTrader remains the sole owner of fills, fees, Funding, positions, accounting, and verdicts. A stdlib SQLite ledger records immutable identities, experiments, failures, verdicts, and parent/child lineage; ignored runtime artifacts live under `var/strategy-loop/`.

**Tech Stack:** Python 3.13 root runtime, isolated Python 3.12 PyBroker runtime, PyBroker 1.2.14, NautilusTrader 2.0.0rc2, SQLite from Python stdlib, canonical JSON, `unittest`.

---

## 0. Frozen baseline and operating contract

Plan baseline commit at authoring time:

```text
35f2b0532ad15e6bc95eecd446934e5d08908af1
```

Verified baseline:

- branch `main` is clean and equals `origin/main`;
- root suite: 112 tests pass;
- research suite: 4 tests pass;
- `nautilus-data status --config config/market_data.json` exits 0;
- current PyBroker path is hard-coded BTCUSDT 1H / 24-bar long-flat momentum;
- `load_pybroker_candidate()` validates the handoff but has no formal historical-backtest caller;
- the only existing Nautilus backtest is the synthetic Funding/accounting oracle.

### Hard boundaries

1. Do not change or write canonical `data/catalog` or `data/funding` from research code.
2. Do not add API keys, credentials, Testnet, live trading, OMS deployment, or external side effects.
3. Do not add Qlib, AutoML, queue, daemon, service, cron, MCP, Dashboard server, SQLAlchemy, or another database package.
4. Do not let a hypothesis carry Python, imports, quantity, leverage, order type, fee assumptions, Funding policy, or executable payloads.
5. Keep `pybroker-candidate-v1` unchanged unless an executable failing test proves it cannot carry the existing handoff; lineage belongs in the ledger, not in candidate schema churn.
6. Keep the first loop to `BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL` and the existing `lookback-momentum-long-flat` family. New families are outside today’s scope.
7. Preserve every evaluated failure. `ERROR`/`BLOCKED` means a technical or evidence failure; `REVISE` means a valid strategy result that did not advance. Never count technical failures as bad strategies.
8. No performance claim may hide modeled Funding, bar-fill assumptions, or missing slippage evidence.
9. One active writer only. The Kanban implementer must not create child cards or background agents that can outlive its terminal state.
10. The auditor is read-only. An audit FAIL is a completed audit finding, not permission to mutate files or create an uncontrolled remediation swarm.

### Control regime

```text
BUILD (goal-mode, one writer)
  └── AUDIT (read-only, starts only after BUILD is done)
        └── operator final readback
```

The seven funnel labels described later are a derived report, **not seven Kanban phases**.

---

## 1. Expected coherent diff

Use the fewest files that keep responsibilities clear. Expected paths:

- Modify: `research/pybroker_research.py`
- Modify: `research/test_pybroker_research.py`
- Modify: `pyproject.toml`
- Create: `config/strategy_loop_policy.json`
- Create: `docs/contracts/strategy-loop-v1.md`
- Create: `src/nautilus_quant/strategy_lab.py`
- Create: `src/nautilus_quant/candidate_backtest.py`
- Create: `tests/test_strategy_lab.py`
- Create: `tests/test_candidate_backtest.py`
- Modify only if shared test helpers make the diff smaller: `tests/test_pybroker_candidate.py`

Runtime outputs must remain ignored:

```text
var/strategy-loop/ledger.sqlite3
var/strategy-loop/runs/<experiment_id>/hypothesis.json
var/strategy-loop/runs/<experiment_id>/research-result.json
var/strategy-loop/runs/<experiment_id>/candidate.json
var/strategy-loop/runs/<experiment_id>/nautilus-verdict.json
var/strategy-loop/latest-funnel.json
var/strategy-loop/latest-funnel.md
```

Do not create a generic plugin system, repository class hierarchy, ORM models, or web UI.

---

## 2. Public contracts

### 2.1 `strategy-hypothesis-v1`

Canonical UTF-8 JSON with one trailing LF. Candidate ID rules should be reused where sensible.

Required semantic shape:

```json
{
  "schema_version": "strategy-hypothesis-v1",
  "parent_strategy_id": null,
  "based_on_verdict_id": null,
  "thesis": "Positive 24-hour momentum persists into the next event",
  "falsification": "No fills, non-positive net result after fees and Funding, or unstable official-window result",
  "strategy_family": "lookback-momentum-long-flat",
  "parameters": {
    "lookback_bars": 24,
    "entry_threshold": 0.0
  },
  "instrument_id": "BTCUSDT-PERP.BINANCE",
  "bar_type": "BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL"
}
```

Validation rules:

- exact top-level keys;
- finite ordinary JSON only;
- allowlisted family only;
- `lookback_bars` positive integer within a bounded v1 range;
- `entry_threshold` finite and non-negative;
- parent and verdict IDs are either `null` or lowercase SHA-256;
- root hypothesis has both lineage fields null;
- child hypothesis has both lineage fields populated;
- no code/import/pickle/joblib/cache/credentials/quantity/leverage/order/accounting fields;
- content-addressed `strategy_id` is the SHA-256 of canonical family + parameters + instrument + bar type;
- `hypothesis_id` is the SHA-256 of the complete canonical hypothesis bytes.

### 2.2 `strategy-loop-policy-v1`

Tracked policy is outside Hermes hypothesis control. Keep only values required by v1:

- schema version;
- starting balance;
- fixed BTC quantity with no leverage;
- configured historical start must equal `config/market_data.json.backtest_start`;
- official-only window begins no earlier than the first official FundingObservation;
- signal timing is bar close, executable no earlier than the next bar event;
- fee source is Nautilus instrument metadata;
- slippage status is explicitly `unmodeled` unless the implementation provides a tested deterministic model;
- decision policy version.

Do not silently invent empirical spread realism from OHLCV bars.

### 2.3 `nautilus-verdict-v1`

Required result sections:

- hypothesis/candidate/strategy/experiment IDs;
- code commit and runtime versions;
- source hash, source first/last timestamps and row count;
- evaluation windows;
- execution assumptions;
- Funding truth counts and source;
- order/fill/trade counts;
- starting and ending balance;
- gross trading result where available;
- fees and Funding totals;
- net account delta;
- realized balance drawdown, explicitly named as such rather than full mark-to-market drawdown;
- ending position and open-position count;
- status: `EVALUATED`, `ERROR`, or `BLOCKED`;
- decision: `REVISE` or `RETAIN_FOR_RESEARCH`; v1 must not emit `PROMOTE`;
- deterministic reason codes;
- `performance_claimable`, which must remain false whenever Funding is mixed/modeled, slippage is unmodeled, or another required evidence tier is absent;
- canonical result hash.

---

## 3. Task A — RED tests for hypothesis and ledger

**Objective:** Establish the trust boundary and durable experiment identity before wiring engines.

**Files:**

- Create: `tests/test_strategy_lab.py`
- Create: `src/nautilus_quant/strategy_lab.py`
- Create: `config/strategy_loop_policy.json`
- Create: `docs/contracts/strategy-loop-v1.md`

### Step A1: Write failing hypothesis tests

Cover at minimum:

- canonical root hypothesis accepted;
- unknown family rejected;
- boolean/zero/negative lookback rejected;
- NaN/Infinity rejected;
- forbidden nested fields rejected;
- malformed lineage rejected;
- duplicate JSON keys rejected;
- non-canonical JSON rejected;
- same semantic hypothesis produces identical IDs.

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest tests.test_strategy_lab -v
```

Expected before implementation: FAIL because the module/API does not exist.

### Step A2: Implement the minimum validator and canonical IDs

Prefer plain functions and dataclasses only where they reduce repeated validation. Do not create an extensible framework for one strategy family.

### Step A3: Write failing ledger tests

Use a temporary SQLite path and prove:

- schema creation is idempotent;
- strategy version is deduplicated by content ID;
- experiment identity includes strategy, data source, policy and engine/runtime identity;
- success, rejection and error records are all retained;
- a child references an existing parent and verdict;
- query output can derive funnel counts without mutable lifecycle columns.

### Step A4: Implement the stdlib SQLite ledger

Use `sqlite3`, transactions, foreign keys and unique constraints. Store artifact hashes and paths, not large signal payloads.

### Step A5: Verify focused tests

Run the same focused command and require PASS.

---

## 4. Task B — Parameterize the existing PyBroker furnace

**Objective:** Consume a validated hypothesis while preserving the existing isolated runtime and candidate contract.

**Files:**

- Modify: `research/pybroker_research.py`
- Modify: `research/test_pybroker_research.py`

### Step B1: Write RED tests

Cover:

- validated `lookback_bars` and `entry_threshold` reach execution logic;
- signal score uses no future bars;
- signal timestamps remain strictly increasing;
- decision timing remains `bar-close; effective no earlier than next event`;
- candidate output cannot land inside canonical data, including symlink paths;
- same hypothesis + same source + same seed produces identical bytes/hash;
- research result records provisional metrics and candidate ID separately;
- unsupported family fails before reading/writing output.

Run:

```bash
cd research
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest test_pybroker_research.py -v
```

Expected before implementation: new tests FAIL.

### Step B2: Make the smallest implementation change

- replace hard-coded `WINDOW` with validated hypothesis parameters;
- add `--hypothesis` input;
- keep `--catalog` and `--output` explicit;
- keep seed deterministic and parallel execution disabled;
- keep candidate schema v1 unchanged;
- print one machine-readable research result to stdout;
- do not import root Nautilus runtime objects or write the root ledger from the research environment.

### Step B3: Focused GREEN

Require all research tests PASS.

---

## 5. Task C — Formal Nautilus candidate consumer

**Objective:** Turn the existing validated candidate handoff into a historical Nautilus evaluation using canonical bars and Funding observations.

**Files:**

- Create: `src/nautilus_quant/candidate_backtest.py`
- Create: `tests/test_candidate_backtest.py`

### Step C1: Write RED tests with bounded synthetic data

Do not begin with the multi-year catalog. Create the smallest fixture that proves:

1. `load_pybroker_candidate()` is the formal entry point.
2. Candidate source identity mismatch fails closed.
3. LONG/FLAT at bar close executes no earlier than the next bar event.
4. Repeated LONG or FLAT intents do not double the position.
5. Final position is flattened deterministically at the evaluation boundary.
6. Fees are owned by Nautilus and reconcile with account events.
7. Official Funding inserts mark before rate at the same timestamp and settles exactly once.
8. Modeled/missing-mark rows force non-claimable truth and use only an explicitly documented bar-price fallback if that fallback is implemented.
9. Technical errors are not emitted as `REVISE` strategy verdicts.
10. Canonical verdict bytes/hash are stable.

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest tests.test_candidate_backtest -v
```

Expected before implementation: FAIL because the evaluator does not exist.

### Step C2: Reuse existing Nautilus primitives

Reuse from the existing synthetic oracle where correct:

- `BacktestEngine` configuration;
- venue/account/instrument construction patterns;
- `FundingRateUpdate` / `MarkPriceUpdate` ordering;
- fee and account-event reconciliation;
- final `engine.dispose()` cleanup.

Load historical bars through `ParquetDataCatalog`; do not manually decode Parquet again in the root evaluator when Nautilus already owns the catalog reader.

### Step C3: Implement a narrow replay strategy

The strategy may only interpret candidate intents:

- `LONG`: enter the fixed policy quantity if not already long;
- `FLAT`: close the existing long position if present;
- no shorts in v1;
- no quantity/leverage/order semantics from the candidate;
- action timestamp must be strictly later than the source signal timestamp.

Mark any shortcut ceiling with a `ponytail:` comment, especially bar-fill/slippage limitations and modeled-Funding price fallback.

### Step C4: Generate and canonicalize evidence

Use Nautilus reports and account/cache readback. If a metric is not truthfully derivable from available data, omit it or label the narrower metric; do not synthesize Sharpe, full mark-to-market drawdown, or execution realism.

### Step C5: Focused GREEN

Require the candidate-backtest suite and existing Funding oracle suite both PASS:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest \
  tests.test_candidate_backtest tests.test_backtest_funding -v
```

---

## 6. Task D — One deterministic loop CLI and feedback packet

**Objective:** Join isolated PyBroker execution, Nautilus evaluation, ledger writes, and derived funnel reporting without adding a service.

**Files:**

- Modify: `src/nautilus_quant/strategy_lab.py`
- Modify: `tests/test_strategy_lab.py`
- Modify: `pyproject.toml`

### Step D1: Write RED controller tests

Mock only the subprocess boundary; keep validators and ledger real. Prove:

- `nautilus-research run --hypothesis ...` invokes the fixed research interpreter and captures stdout/stderr/exit code;
- a PyBroker crash records `ERROR` and does not invoke Nautilus;
- a valid candidate is loaded and evaluated exactly once;
- artifacts publish atomically only after readback;
- the ledger never points to a missing or hash-mismatched artifact;
- rerunning the same experiment key returns/reuses the existing deterministic result rather than counting a duplicate strategy attempt;
- feedback names the failed stage/reason codes and immutable parent IDs;
- funnel counts unique strategy versions, not raw retries.

### Step D2: Implement one CLI

Add:

```text
nautilus-research run --hypothesis PATH
nautilus-research funnel [--format json|markdown]
```

The root controller may use `subprocess.run([...], check=False)` to call:

```text
research/.venv/bin/python research/pybroker_research.py
```

Use an argv list, never shell interpolation. Capture bounded output and persist exact errors.

### Step D3: Derived funnel

Generate these labels from ledger queries:

1. Proposed
2. Contract valid
3. PyBroker completed
4. Research screened
5. Nautilus replayed
6. Robustness passed
7. Promotion eligible

For v1, stages 6–7 may honestly be zero. The report must include:

- entered/passed/rejected;
- previous-stage and cumulative survival rates;
- top reason codes;
- cohort/policy version;
- data-as-of;
- Funding truth and performance-claimability counts.

Do not hard-code the reference screenshot’s numbers and do not create a web dashboard.

### Step D4: Focused GREEN

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest tests.test_strategy_lab -v
```

---

## 7. Task E — Real two-generation execution

**Objective:** Prove the feedback edge, not just the forward pipeline.

### Step E1: Create root hypothesis H0

Write an ignored runtime hypothesis using the existing baseline family with `lookback_bars=24` and `entry_threshold=0.0`.

### Step E2: Execute H0

```bash
.venv/bin/nautilus-research run \
  --hypothesis var/strategy-loop/hypotheses/h0.json
```

Require a persisted hypothesis, research result, candidate if research completed, Nautilus verdict if candidate was valid, feedback packet and ledger row. A valid negative result is acceptable; a technical error is not end-to-end completion.

### Step E3: Read H0 feedback before creating H1

H1 must:

- reference H0’s `strategy_id` and verdict ID;
- change exactly one parameter;
- explain the change using a real H0 metric or reason code;
- keep the same family/instrument/bar type/policy.

Examples, chosen only if supported by H0 evidence:

- excessive switching/cost erosion → increase lookback;
- too few fills → lower threshold;
- weak persistence → increase threshold.

Do not preselect a winning mutation before H0 runs.

### Step E4: Execute H1

Run the same CLI with H1 and require the same artifact/readback chain.

### Step E5: Reproducibility readback

Rerun one identical hypothesis and require unchanged logical IDs and canonical hashes without a duplicate funnel count.

### Step E6: Generate the funnel

```bash
.venv/bin/nautilus-research funnel --format json
.venv/bin/nautilus-research funnel --format markdown
```

Read both generated artifacts and verify their counts against direct SQLite queries.

---

## 8. Data-truth requirements

The configured backtest start remains:

```text
2022-07-01T00:00:00Z
```

The current Funding generation contains both modeled and official observations. Therefore:

1. A full configured-history evaluation must be labeled mixed/non-claimable.
2. An official-only evaluation may begin at the first official observation boundary, discovered from the canonical store rather than hard-coded from prose.
3. Official truth only proves Funding provenance, not alpha or execution realism.
4. Since the existing reference runner already consumed the full catalog, no historical slice may be relabeled as a genuinely untouched sealed holdout.
5. V1 can emit only `RETAIN_FOR_RESEARCH`, never production promotion.
6. If OHLCV-only fills leave slippage unmodeled, `performance_claimable` remains false even on the official-Funding slice.

These labels are acceptance criteria, not advisory copy.

---

## 9. Full verification after the final write

Run in this order:

```bash
# Focused loop tests
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest \
  tests.test_strategy_lab tests.test_candidate_backtest \
  tests.test_pybroker_candidate tests.test_backtest_funding -v

# Entire root suite
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -q

# Entire isolated research suite
PYTHONDONTWRITEBYTECODE=1 research/.venv/bin/python -m unittest \
  discover -s research -p 'test*.py' -q

# Canonical data health
.venv/bin/nautilus-data status --config config/market_data.json

# Secrets and whitespace
.venv/bin/python scripts/check_secrets.py
git diff --check
```

Then verify:

- `data/`, `.venv/`, `.local/` and `var/` are not staged;
- only plan-scoped files changed;
- no credential or live/Testnet surface was added;
- no task-bound child/background process can write after the card terminal event;
- H0/H1 artifacts and ledger readback exist locally but are ignored;
- `git status --short` is understood before commit.

Commit and push through configured hooks; never use `--no-verify`:

```bash
git add <tracked plan-scoped files>
git commit -m "feat: close the PyBroker Nautilus research loop"
git push origin main
```

Remote readback:

```bash
git fetch origin main
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)"
git status --short
```

Expected final state: local `HEAD == origin/main`, clean tracked worktree, runtime artifacts ignored.

---

## 10. Independent audit contract

The read-only auditor starts only after BUILD is `done` and must inspect final committed bytes, not the implementer’s prose.

Audit minimum:

1. Read this plan at the commit referenced by BUILD.
2. Inspect the actual diff and remote commit.
3. Verify hypothesis trust-boundary validation.
4. Verify candidate source digest and next-event timing.
5. Verify quantity/leverage/cost/Funding policy cannot come from Hermes input.
6. Verify Funding truth and `performance_claimable` fail closed.
7. Verify errors are not counted as strategy rejects.
8. Verify H0 → verdict → H1 lineage from durable local artifacts and SQLite.
9. Rerun focused suites, both full suites, data status, secrets scan and `git diff --check` after the final implementation write.
10. Verify canonical data is unchanged by the loop and no live/Testnet/credential surface exists.
11. Verify `HEAD == origin/main` and tracked worktree is clean.

Audit outcome:

- PASS: complete with exact commands, counts, commit and artifact IDs.
- FAIL: complete the audit with `approved=false`, exact reproducer and bounded required repair. Do not mutate the repo, unblock hidden work, or create cards.

Operator closeout occurs only after reading the audit evidence and confirming the board has no running/blocked executable residue.

---

## 11. Definition of done

All boxes are mandatory:

- [ ] Plan-scoped code and contracts implemented with no new runtime dependency.
- [ ] Hermes H0 is canonical and recorded.
- [ ] PyBroker H0 truly runs and produces durable research evidence.
- [ ] `load_pybroker_candidate()` has a formal historical Nautilus caller.
- [ ] Nautilus H0 truly runs and owns fills, fees, Funding, positions and accounting.
- [ ] H0 verdict and reason codes are persisted.
- [ ] Hermes H1 is created only after reading H0 feedback and changes exactly one parameter.
- [ ] H1 traverses the same full path.
- [ ] Parent/child lineage and all failure outcomes are queryable from SQLite.
- [ ] Identical rerun preserves logical IDs/hashes and does not inflate funnel counts.
- [ ] Funnel report is derived from ledger truth and clearly shows zero/unimplemented tiers.
- [ ] Mixed/modeled Funding and unmodeled slippage cannot produce a claimable verdict.
- [ ] Root and research full suites pass after the final edit.
- [ ] Canonical data status remains PASS and canonical bytes are not research-written.
- [ ] Secrets scan and configured hooks pass.
- [ ] Clean commit pushed and read back from `origin/main`.
- [ ] Independent read-only audit returns PASS.
- [ ] Kanban shows BUILD/AUDIT terminal with no running writer or unexplained process residue.

A negative strategy result is allowed. A stub, mocked integration-only path, synthetic-only demonstration, candidate-only handoff, unverified local diff, or unpushed commit is not done.
