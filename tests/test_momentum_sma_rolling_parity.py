"""Rolling-sum parity for momentum-with-sma-regime-long-flat-v1.

Compares the O(N) rolling fast path (evaluate_batch + IncrementalFamilyEvaluator)
against the O(N*window) slow reference (per-bar definition.evaluate over the
warmup window). Asserts byte/field-equivalent decisions incl. canonical bytes,
plus snapshot/restore exact parity and generic-family regression.
"""

from __future__ import annotations

import json
import random
import unittest

from nautilus_quant.strategy_families import (
    ClosedBar,
    DEFAULT_REGISTRY,
    IncrementalFamilyEvaluator,
    KERNEL_HASH,
    KERNEL_VERSION,
    FamilyDecision,
    canonical_decision_bytes,
    evaluate_batch,
    restore_incremental,
)

FAMILY_ID = "momentum-with-sma-regime-long-flat"
FAMILY_VERSION = "momentum-with-sma-regime-long-flat-v1"
HOUR_NS = 3_600_000_000_000
BASE_TS = 1_700_000_000_000_000_000

# KERNEL_HASH must never change with a pure performance remediation.
EXPECTED_KERNEL_HASH = "15316d4cdf3ecfcad56e056910dbf43922b027b89ea33aab5cefe462cd2a6427"

EXPECTED_SNAPSHOT_FIELDS = {
    "bars",
    "family_id",
    "family_version",
    "kernel_hash",
    "kernel_version",
    "last_ts_event_ns",
    "parameters",
    "schema_version",
}


def make_bars(count: int, seed: int) -> list[ClosedBar]:
    rng = random.Random(seed)
    bars: list[ClosedBar] = []
    price = 100.0
    for index in range(count):
        price = max(1.0, price * (1.0 + rng.uniform(-0.02, 0.02)))
        bars.append(
            ClosedBar(
                ts_event_ns=BASE_TS + index * HOUR_NS,
                open=price,
                high=price * 1.001,
                low=price * 0.999,
                close=price,
                volume=1.0,
            )
        )
    return bars


def slow_reference(
    bars: list[ClosedBar], parameters: dict
) -> tuple[FamilyDecision, ...]:
    """Old O(N*window) path: per-bar definition.evaluate over warmup window."""
    definition = DEFAULT_REGISTRY.resolve(FAMILY_ID, FAMILY_VERSION)
    normalized = definition.validate_parameters(parameters)
    warmup = definition.warmup_bars(normalized)
    from nautilus_quant.strategy_families import _decision

    decisions: list[FamilyDecision] = []
    window: list[ClosedBar] = []
    for bar in bars:
        window.append(bar)
        if len(window) > warmup:
            window.pop(0)
        if len(window) < warmup:
            continue
        decisions.append(
            _decision(
                bar=bar,
                evaluation=definition.evaluate(tuple(window), normalized),
                definition=definition,
                parameters=normalized,
            )
        )
    return tuple(decisions)


def incremental_decisions(
    bars: list[ClosedBar], parameters: dict
) -> tuple[FamilyDecision, ...]:
    evaluator = IncrementalFamilyEvaluator(
        family_id=FAMILY_ID, family_version=FAMILY_VERSION, parameters=parameters
    )
    decisions: list[FamilyDecision] = []
    for bar in bars:
        decision = evaluator.push(bar)
        if decision is not None:
            decisions.append(decision)
    return tuple(decisions)


def assert_decisions_identical(
    case: unittest.TestCase,
    first: tuple[FamilyDecision, ...],
    second: tuple[FamilyDecision, ...],
) -> None:
    case.assertEqual(len(first), len(second))
    for left, right in zip(first, second):
        case.assertEqual(left.ts_event_ns, right.ts_event_ns)
        case.assertEqual(left.score, right.score)
        case.assertEqual(left.target_intent, right.target_intent)
        case.assertEqual(left.reason, right.reason)
        case.assertEqual(left.signal_id, right.signal_id)
        case.assertEqual(left.family_id, right.family_id)
        case.assertEqual(left.family_version, right.family_version)
        case.assertEqual(left.kernel_version, right.kernel_version)
        case.assertEqual(left.kernel_hash, right.kernel_hash)
        case.assertEqual(
            canonical_decision_bytes(left), canonical_decision_bytes(right)
        )


class MomentumSmaRollingParityTests(unittest.TestCase):
    def test_kernel_hash_unchanged(self) -> None:
        self.assertEqual(KERNEL_HASH, EXPECTED_KERNEL_HASH)
        self.assertEqual(KERNEL_VERSION, "strategy-family-kernel-v1")

    def test_short_window_parity(self) -> None:
        parameters = {
            "entry_threshold": 0.005,
            "momentum_lookback_bars": 5,
            "trend_window_bars": 8,
        }
        bars = make_bars(60, seed=7)
        expected = slow_reference(bars, parameters)
        fast = evaluate_batch(
            family_id=FAMILY_ID,
            family_version=FAMILY_VERSION,
            parameters=parameters,
            bars=bars,
        )
        stepped = incremental_decisions(bars, parameters)
        self.assertGreater(len(expected), 0)
        assert_decisions_identical(self, expected, fast)
        assert_decisions_identical(self, expected, stepped)

    def test_mid_window_parity(self) -> None:
        parameters = {
            "entry_threshold": 0.01,
            "momentum_lookback_bars": 64,
            "trend_window_bars": 256,
        }
        bars = make_bars(1500, seed=1234)
        expected = slow_reference(bars, parameters)
        fast = evaluate_batch(
            family_id=FAMILY_ID,
            family_version=FAMILY_VERSION,
            parameters=parameters,
            bars=bars,
        )
        stepped = incremental_decisions(bars, parameters)
        self.assertGreater(len(expected), 0)
        assert_decisions_identical(self, expected, fast)
        assert_decisions_identical(self, expected, stepped)

    def test_8760_long_window_parity(self) -> None:
        parameters = {
            "entry_threshold": 0.02,
            "momentum_lookback_bars": 2880,
            "trend_window_bars": 8760,
        }
        bars = make_bars(9200, seed=99)
        expected = slow_reference(bars, parameters)
        fast = evaluate_batch(
            family_id=FAMILY_ID,
            family_version=FAMILY_VERSION,
            parameters=parameters,
            bars=bars,
        )
        stepped = incremental_decisions(bars, parameters)
        self.assertGreater(len(expected), 0)
        assert_decisions_identical(self, expected, fast)
        assert_decisions_identical(self, expected, stepped)

    def test_snapshot_restore_exact_parity(self) -> None:
        parameters = {
            "entry_threshold": 0.01,
            "momentum_lookback_bars": 32,
            "trend_window_bars": 128,
        }
        bars = make_bars(600, seed=2026)
        split = 400
        live = IncrementalFamilyEvaluator(
            family_id=FAMILY_ID, family_version=FAMILY_VERSION, parameters=parameters
        )
        live_decisions: list[FamilyDecision] = []
        for bar in bars[:split]:
            decision = live.push(bar)
            if decision is not None:
                live_decisions.append(decision)
        snapshot = live.snapshot()
        # Snapshot format unchanged: exact field set, canonical encoding.
        value = json.loads(snapshot)
        self.assertEqual(set(value), EXPECTED_SNAPSHOT_FIELDS)
        self.assertEqual(
            snapshot,
            restore_incremental(snapshot).snapshot(),
        )
        restored = restore_incremental(snapshot)
        resumed: list[FamilyDecision] = []
        for bar in bars[split:]:
            decision = restored.push(bar)
            if decision is not None:
                resumed.append(decision)
        for bar in bars[split:]:
            decision = live.push(bar)
            if decision is not None:
                live_decisions.append(decision)
        assert_decisions_identical(
            self,
            tuple(live_decisions[len(live_decisions) - len(resumed) :]),
            tuple(resumed),
        )
        batch = evaluate_batch(
            family_id=FAMILY_ID,
            family_version=FAMILY_VERSION,
            parameters=parameters,
            bars=bars,
        )
        assert_decisions_identical(self, batch, tuple(live_decisions))

    def test_old_snapshot_restores(self) -> None:
        # An old snapshot is byte-identical in format (bars-only state); the
        # rolling sum rebuilds from replay, so a snapshot taken by the slow
        # code restores cleanly under the new code.
        parameters = {
            "entry_threshold": 0.005,
            "momentum_lookback_bars": 5,
            "trend_window_bars": 8,
        }
        bars = make_bars(30, seed=11)
        evaluator = IncrementalFamilyEvaluator(
            family_id=FAMILY_ID, family_version=FAMILY_VERSION, parameters=parameters
        )
        for bar in bars[:20]:
            evaluator.push(bar)
        snapshot = evaluator.snapshot()
        restored = restore_incremental(snapshot)
        self.assertEqual(restored.snapshot(), snapshot)
        for bar in bars[20:]:
            self.assertEqual(
                canonical_decision_bytes(evaluator.push(bar)),  # type: ignore[arg-type]
                canonical_decision_bytes(restored.push(bar)),  # type: ignore[arg-type]
            )

    def test_generic_families_regression(self) -> None:
        cases = [
            (
                "lookback-momentum-long-flat",
                "lookback-momentum-long-flat-v1",
                {"entry_threshold": 0.05, "lookback_bars": 3},
            ),
            (
                "close-vs-sma-mean-reversion-long-flat",
                "close-vs-sma-mean-reversion-long-flat-v1",
                {"discount_threshold": 0.05, "window_bars": 4},
            ),
            (
                "dual-sma-trend-long-flat",
                "dual-sma-trend-long-flat-v1",
                {"fast_window_bars": 3, "slow_window_bars": 6},
            ),
            (
                "price-vs-sma-trend-long-flat",
                "price-vs-sma-trend-long-flat-v1",
                {"window_bars": 5},
            ),
            (
                "skip-recent-momentum-long-flat",
                "skip-recent-momentum-long-flat-v1",
                {"formation_lookback_bars": 6, "skip_recent_bars": 2},
            ),
        ]
        bars = make_bars(60, seed=5150)
        for family_id, family_version, parameters in cases:
            with self.subTest(family=family_id):
                batch = evaluate_batch(
                    family_id=family_id,
                    family_version=family_version,
                    parameters=parameters,
                    bars=bars,
                )
                evaluator = IncrementalFamilyEvaluator(
                    family_id=family_id,
                    family_version=family_version,
                    parameters=parameters,
                )
                stepped: list[FamilyDecision] = []
                for bar in bars:
                    decision = evaluator.push(bar)
                    if decision is not None:
                        stepped.append(decision)
                self.assertGreater(len(batch), 0)
                assert_decisions_identical(self, batch, tuple(stepped))

    def test_momentum_greater_than_trend_parity(self) -> None:
        parameters = {
            "entry_threshold": 0.005,
            "momentum_lookback_bars": 128,
            "trend_window_bars": 32,
        }
        bars = make_bars(400, seed=41)
        expected = slow_reference(bars, parameters)
        fast = evaluate_batch(
            family_id=FAMILY_ID,
            family_version=FAMILY_VERSION,
            parameters=parameters,
            bars=bars,
        )
        stepped = incremental_decisions(bars, parameters)
        self.assertGreater(len(expected), 0)
        assert_decisions_identical(self, expected, fast)
        assert_decisions_identical(self, expected, stepped)

    def test_momentum_one_parity(self) -> None:
        parameters = {
            "entry_threshold": 0.0,
            "momentum_lookback_bars": 1,
            "trend_window_bars": 24,
        }
        bars = make_bars(120, seed=42)
        expected = slow_reference(bars, parameters)
        fast = evaluate_batch(
            family_id=FAMILY_ID,
            family_version=FAMILY_VERSION,
            parameters=parameters,
            bars=bars,
        )
        stepped = incremental_decisions(bars, parameters)
        self.assertGreater(len(expected), 0)
        # momentum window of 1 always scores 0.0; gate is strict >.
        for decision in expected:
            self.assertEqual(decision.score, "0")
            self.assertEqual(decision.target_intent, "FLAT")
            self.assertEqual(decision.reason, "MOMENTUM_GATE_FAIL")
        assert_decisions_identical(self, expected, fast)
        assert_decisions_identical(self, expected, stepped)

    def test_trend_two_parity(self) -> None:
        parameters = {
            "entry_threshold": 0.003,
            "momentum_lookback_bars": 5,
            "trend_window_bars": 2,
        }
        bars = make_bars(120, seed=43)
        expected = slow_reference(bars, parameters)
        fast = evaluate_batch(
            family_id=FAMILY_ID,
            family_version=FAMILY_VERSION,
            parameters=parameters,
            bars=bars,
        )
        stepped = incremental_decisions(bars, parameters)
        self.assertGreater(len(expected), 0)
        assert_decisions_identical(self, expected, fast)
        assert_decisions_identical(self, expected, stepped)

    def test_momentum_equals_trend_parity(self) -> None:
        parameters = {
            "entry_threshold": 0.004,
            "momentum_lookback_bars": 32,
            "trend_window_bars": 32,
        }
        bars = make_bars(300, seed=44)
        expected = slow_reference(bars, parameters)
        fast = evaluate_batch(
            family_id=FAMILY_ID,
            family_version=FAMILY_VERSION,
            parameters=parameters,
            bars=bars,
        )
        stepped = incremental_decisions(bars, parameters)
        self.assertGreater(len(expected), 0)
        assert_decisions_identical(self, expected, fast)
        assert_decisions_identical(self, expected, stepped)

    def test_pre_warmup_snapshot_restore(self) -> None:
        parameters = {
            "entry_threshold": 0.01,
            "momentum_lookback_bars": 32,
            "trend_window_bars": 128,
        }
        bars = make_bars(400, seed=45)
        split = 20  # well before warmup=128: no decisions yet
        live = IncrementalFamilyEvaluator(
            family_id=FAMILY_ID, family_version=FAMILY_VERSION, parameters=parameters
        )
        for bar in bars[:split]:
            self.assertIsNone(live.push(bar))
        snapshot = live.snapshot()
        value = json.loads(snapshot)
        self.assertEqual(set(value), EXPECTED_SNAPSHOT_FIELDS)
        restored = restore_incremental(snapshot)
        self.assertEqual(restored.snapshot(), snapshot)
        live_decisions: list[FamilyDecision] = []
        resumed: list[FamilyDecision] = []
        for bar in bars[split:]:
            decision = live.push(bar)
            if decision is not None:
                live_decisions.append(decision)
        for bar in bars[split:]:
            decision = restored.push(bar)
            if decision is not None:
                resumed.append(decision)
        self.assertGreater(len(live_decisions), 0)
        assert_decisions_identical(self, tuple(live_decisions), tuple(resumed))
        batch = evaluate_batch(
            family_id=FAMILY_ID,
            family_version=FAMILY_VERSION,
            parameters=parameters,
            bars=bars,
        )
        assert_decisions_identical(self, batch, tuple(live_decisions))

    def test_long_history_split_restore(self) -> None:
        parameters = {
            "entry_threshold": 0.008,
            "momentum_lookback_bars": 64,
            "trend_window_bars": 256,
        }
        bars = make_bars(2000, seed=46)
        for split in (999, 1500):
            with self.subTest(split=split):
                live = IncrementalFamilyEvaluator(
                    family_id=FAMILY_ID,
                    family_version=FAMILY_VERSION,
                    parameters=parameters,
                )
                prefix: list[FamilyDecision] = []
                for bar in bars[:split]:
                    decision = live.push(bar)
                    if decision is not None:
                        prefix.append(decision)
                snapshot = live.snapshot()
                restored = restore_incremental(snapshot)
                self.assertEqual(restored.snapshot(), snapshot)
                live_tail: list[FamilyDecision] = []
                resumed: list[FamilyDecision] = []
                for bar in bars[split:]:
                    decision = live.push(bar)
                    if decision is not None:
                        live_tail.append(decision)
                for bar in bars[split:]:
                    decision = restored.push(bar)
                    if decision is not None:
                        resumed.append(decision)
                self.assertGreater(len(resumed), 0)
                assert_decisions_identical(self, tuple(live_tail), tuple(resumed))
                full = incremental_decisions(bars, parameters)
                assert_decisions_identical(
                    self, full, tuple(prefix) + tuple(resumed)
                )
                batch = evaluate_batch(
                    family_id=FAMILY_ID,
                    family_version=FAMILY_VERSION,
                    parameters=parameters,
                    bars=bars,
                )
                assert_decisions_identical(self, batch, full)

    def test_flat_adversarial_exact_boundary(self) -> None:
        # Flat prices: every close identical, so momentum score is exactly 0.0
        # and last_close == trend_mean exactly. Both gates use strict >,
        # so every decision must be FLAT/MOMENTUM_GATE_FAIL at threshold 0.
        parameters = {
            "entry_threshold": 0.0,
            "momentum_lookback_bars": 8,
            "trend_window_bars": 16,
        }
        flat = [
            ClosedBar(
                ts_event_ns=BASE_TS + index * HOUR_NS,
                open=100.0,
                high=100.0,
                low=100.0,
                close=100.0,
                volume=1.0,
            )
            for index in range(64)
        ]
        expected = slow_reference(flat, parameters)
        fast = evaluate_batch(
            family_id=FAMILY_ID,
            family_version=FAMILY_VERSION,
            parameters=parameters,
            bars=flat,
        )
        stepped = incremental_decisions(flat, parameters)
        self.assertGreater(len(expected), 0)
        for decision in expected:
            self.assertEqual(decision.score, "0")
            self.assertEqual(decision.target_intent, "FLAT")
            self.assertEqual(decision.reason, "MOMENTUM_GATE_FAIL")
        assert_decisions_identical(self, expected, fast)
        assert_decisions_identical(self, expected, stepped)
        # Adversarial: pin the threshold exactly to a realized slow score so
        # the strict-greater gate must stay FLAT on that bar on all paths.
        probe_parameters = {
            "entry_threshold": 0.0,
            "momentum_lookback_bars": 5,
            "trend_window_bars": 8,
        }
        probe_bars = make_bars(40, seed=47)
        probe_slow = slow_reference(probe_bars, probe_parameters)
        self.assertGreater(len(probe_slow), 0)
        pinned_index = next(
            index
            for index, decision in enumerate(probe_slow)
            if float(decision.score) > 0.0
        )
        pinned_threshold = float(probe_slow[pinned_index].score)
        pinned = {
            "entry_threshold": pinned_threshold,
            "momentum_lookback_bars": 5,
            "trend_window_bars": 8,
        }
        expected_pinned = slow_reference(probe_bars, pinned)
        fast_pinned = evaluate_batch(
            family_id=FAMILY_ID,
            family_version=FAMILY_VERSION,
            parameters=pinned,
            bars=probe_bars,
        )
        stepped_pinned = incremental_decisions(probe_bars, pinned)
        self.assertEqual(expected_pinned[pinned_index].target_intent, "FLAT")
        assert_decisions_identical(self, expected_pinned, fast_pinned)
        assert_decisions_identical(self, expected_pinned, stepped_pinned)


if __name__ == "__main__":
    unittest.main()
