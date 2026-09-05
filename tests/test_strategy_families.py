from __future__ import annotations

import unittest

from nautilus_quant.strategy_families import (
    ClosedBar,
    FamilyDefinition,
    FamilyEvaluation,
    FamilyKernelError,
    FamilyRegistry,
    IncrementalFamilyEvaluator,
    KERNEL_HASH,
    KERNEL_VERSION,
    DEFAULT_REGISTRY,
    canonical_decision_bytes,
    derive_signal_id,
    evaluate_batch,
    restore_incremental,
)


FAMILY_ID = "lookback-momentum-long-flat"
FAMILY_VERSION = "lookback-momentum-long-flat-v1"
PARAMETERS = {"entry_threshold": 0.05, "lookback_bars": 2}
SMA_FAMILY_ID = "close-vs-sma-mean-reversion-long-flat"
SMA_FAMILY_VERSION = "close-vs-sma-mean-reversion-long-flat-v1"
SMA_PARAMETERS = {"discount_threshold": 0.05, "window_bars": 2}
TREND_SMA_FAMILY_ID = "price-vs-sma-trend-long-flat"
TREND_SMA_FAMILY_VERSION = "price-vs-sma-trend-long-flat-v1"
TREND_SMA_PARAMETERS = {"window_bars": 3}
DUAL_SMA_FAMILY_ID = "dual-sma-trend-long-flat"
DUAL_SMA_FAMILY_VERSION = "dual-sma-trend-long-flat-v1"
DUAL_SMA_PARAMETERS = {"fast_window_bars": 2, "slow_window_bars": 3}
HYBRID_FAMILY_ID = "momentum-with-sma-regime-long-flat"
HYBRID_FAMILY_VERSION = "momentum-with-sma-regime-long-flat-v1"
HYBRID_PARAMETERS = {
    "entry_threshold": 0.0,
    "momentum_lookback_bars": 2,
    "trend_window_bars": 3,
}
SKIP_RECENT_FAMILY_ID = "skip-recent-momentum-long-flat"
SKIP_RECENT_FAMILY_VERSION = "skip-recent-momentum-long-flat-v1"
SKIP_RECENT_PARAMETERS = {
    "formation_lookback_bars": 4,
    "skip_recent_bars": 1,
}
DAILY_IBS_FAMILY_ID = "utc-daily-ibs-mean-reversion-long-flat"
DAILY_IBS_FAMILY_VERSION = "utc-daily-ibs-mean-reversion-long-flat-v1"
DAILY_IBS_PARAMETERS = {"entry_ibs_threshold": 0.2}
HOUR_NS = 3_600_000_000_000


def bar(timestamp: int, close: float) -> ClosedBar:
    return ClosedBar(
        ts_event_ns=timestamp,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1.0,
    )


class StrategyFamilyGoldenVectorTests(unittest.TestCase):
    def test_predeclared_daily_ibs_family_uses_only_prior_complete_utc_day(self) -> None:
        def daily_bars(last_close: float) -> list[ClosedBar]:
            values: list[ClosedBar] = []
            for hour in range(1, 73):
                high = 100.0
                low = 100.0
                close = 100.0
                if 49 <= hour <= 72:
                    high = 110.0 if hour == 50 else 105.0
                    low = 90.0 if hour == 51 else 95.0
                    close = last_close if hour == 72 else 100.0
                values.append(
                    ClosedBar(
                        ts_event_ns=hour * HOUR_NS,
                        open=close,
                        high=max(high, close),
                        low=min(low, close),
                        close=close,
                        volume=1.0,
                    )
                )
            return values

        low_ibs = evaluate_batch(
            family_id=DAILY_IBS_FAMILY_ID,
            family_version=DAILY_IBS_FAMILY_VERSION,
            parameters=DAILY_IBS_PARAMETERS,
            bars=daily_bars(92.0),
        )
        high_ibs = evaluate_batch(
            family_id=DAILY_IBS_FAMILY_ID,
            family_version=DAILY_IBS_FAMILY_VERSION,
            parameters=DAILY_IBS_PARAMETERS,
            bars=daily_bars(108.0),
        )

        self.assertEqual(
            (low_ibs[0].score, low_ibs[0].target_intent, low_ibs[0].reason),
            ("0.1", "LONG", "PRIOR_UTC_DAY_IBS_BELOW_ENTRY_THRESHOLD"),
        )
        self.assertEqual(
            (high_ibs[0].score, high_ibs[0].target_intent, high_ibs[0].reason),
            ("0.9", "FLAT", "PRIOR_UTC_DAY_IBS_AT_OR_ABOVE_ENTRY_THRESHOLD"),
        )
        definition = DEFAULT_REGISTRY.resolve(DAILY_IBS_FAMILY_ID, DAILY_IBS_FAMILY_VERSION)
        self.assertTrue(definition.thesis)
        self.assertTrue(definition.falsification)
        with self.assertRaisesRegex(FamilyKernelError, "strictly between 0 and 1"):
            evaluate_batch(
                family_id=DAILY_IBS_FAMILY_ID,
                family_version=DAILY_IBS_FAMILY_VERSION,
                parameters={"entry_ibs_threshold": 1.0},
                bars=daily_bars(92.0),
            )

    def test_predeclared_sma_trend_family_has_golden_vectors_and_version_identity(self) -> None:
        decisions = evaluate_batch(
            family_id=TREND_SMA_FAMILY_ID,
            family_version=TREND_SMA_FAMILY_VERSION,
            parameters=TREND_SMA_PARAMETERS,
            bars=[bar(1, 100), bar(2, 100), bar(3, 103), bar(4, 97)],
        )

        self.assertEqual(
            [
                (item.ts_event_ns, item.score, item.target_intent, item.reason)
                for item in decisions
            ],
            [
                (3, "0.019801980198", "LONG", "CLOSE_ABOVE_SMA_TREND"),
                (4, "-0.03", "FLAT", "CLOSE_AT_OR_BELOW_SMA_TREND"),
            ],
        )
        definition = DEFAULT_REGISTRY.resolve(
            TREND_SMA_FAMILY_ID,
            TREND_SMA_FAMILY_VERSION,
        )
        self.assertEqual(definition.family_version, TREND_SMA_FAMILY_VERSION)
        self.assertTrue(definition.thesis)
        self.assertTrue(definition.falsification)
        with self.assertRaisesRegex(FamilyKernelError, "only window_bars"):
            evaluate_batch(
                family_id=TREND_SMA_FAMILY_ID,
                family_version=TREND_SMA_FAMILY_VERSION,
                parameters={"window_bars": 3, "entry_threshold": 0.0},
                bars=[bar(1, 100), bar(2, 100), bar(3, 103)],
            )

    def test_predeclared_dual_sma_trend_family_has_golden_vectors_and_version_identity(self) -> None:
        decisions = evaluate_batch(
            family_id=DUAL_SMA_FAMILY_ID,
            family_version=DUAL_SMA_FAMILY_VERSION,
            parameters=DUAL_SMA_PARAMETERS,
            bars=[bar(1, 100), bar(2, 100), bar(3, 110), bar(4, 90)],
        )

        self.assertEqual(
            [
                (item.ts_event_ns, item.score, item.target_intent, item.reason)
                for item in decisions
            ],
            [
                (3, "0.016129032258", "LONG", "FAST_SMA_ABOVE_SLOW_SMA"),
                (4, "0", "FLAT", "FAST_SMA_AT_OR_BELOW_SLOW_SMA"),
            ],
        )
        definition = DEFAULT_REGISTRY.resolve(DUAL_SMA_FAMILY_ID, DUAL_SMA_FAMILY_VERSION)
        self.assertEqual(definition.family_version, DUAL_SMA_FAMILY_VERSION)
        self.assertTrue(definition.thesis)
        self.assertTrue(definition.falsification)
        with self.assertRaisesRegex(FamilyKernelError, "less than slow_window_bars"):
            evaluate_batch(
                family_id=DUAL_SMA_FAMILY_ID,
                family_version=DUAL_SMA_FAMILY_VERSION,
                parameters={"fast_window_bars": 3, "slow_window_bars": 3},
                bars=[bar(1, 100), bar(2, 100), bar(3, 110)],
            )

    def test_predeclared_momentum_sma_regime_family_has_golden_vectors_and_ablation_semantics(self) -> None:
        passing = evaluate_batch(
            family_id=HYBRID_FAMILY_ID,
            family_version=HYBRID_FAMILY_VERSION,
            parameters=HYBRID_PARAMETERS,
            bars=[bar(1, 100), bar(2, 100), bar(3, 110)],
        )
        trend_blocked = evaluate_batch(
            family_id=HYBRID_FAMILY_ID,
            family_version=HYBRID_FAMILY_VERSION,
            parameters=HYBRID_PARAMETERS,
            bars=[bar(1, 120), bar(2, 100), bar(3, 105)],
        )

        self.assertEqual(
            (passing[0].score, passing[0].target_intent, passing[0].reason),
            ("0.1", "LONG", "MOMENTUM_AND_SMA_REGIME_PASS"),
        )
        self.assertEqual(
            (trend_blocked[0].score, trend_blocked[0].target_intent, trend_blocked[0].reason),
            ("0.05", "FLAT", "SMA_REGIME_GATE_FAIL"),
        )
        definition = DEFAULT_REGISTRY.resolve(HYBRID_FAMILY_ID, HYBRID_FAMILY_VERSION)
        self.assertTrue(definition.thesis)
        self.assertTrue(definition.falsification)
        with self.assertRaisesRegex(FamilyKernelError, r"momentum\+sma regime parameters"):
            evaluate_batch(
                family_id=HYBRID_FAMILY_ID,
                family_version=HYBRID_FAMILY_VERSION,
                parameters={**HYBRID_PARAMETERS, "extra": 1},
                bars=[bar(1, 100), bar(2, 100), bar(3, 110)],
            )

    def test_predeclared_skip_recent_momentum_has_golden_vectors_and_skip_semantics(self) -> None:
        decisions = evaluate_batch(
            family_id=SKIP_RECENT_FAMILY_ID,
            family_version=SKIP_RECENT_FAMILY_VERSION,
            parameters=SKIP_RECENT_PARAMETERS,
            bars=[
                bar(1, 100),
                bar(2, 110),
                bar(3, 120),
                bar(4, 90),
                bar(5, 80),
                bar(6, 130),
            ],
        )

        self.assertEqual(
            [
                (item.ts_event_ns, item.score, item.target_intent, item.reason)
                for item in decisions
            ],
            [
                (5, "-0.1", "FLAT", "SKIP_RECENT_MOMENTUM_NON_POSITIVE"),
                (6, "-0.272727272727", "FLAT", "SKIP_RECENT_MOMENTUM_NON_POSITIVE"),
            ],
        )
        positive = evaluate_batch(
            family_id=SKIP_RECENT_FAMILY_ID,
            family_version=SKIP_RECENT_FAMILY_VERSION,
            parameters=SKIP_RECENT_PARAMETERS,
            bars=[bar(1, 100), bar(2, 110), bar(3, 120), bar(4, 130), bar(5, 90)],
        )
        self.assertEqual(
            (positive[0].score, positive[0].target_intent, positive[0].reason),
            ("0.3", "LONG", "SKIP_RECENT_MOMENTUM_POSITIVE"),
        )
        definition = DEFAULT_REGISTRY.resolve(
            SKIP_RECENT_FAMILY_ID,
            SKIP_RECENT_FAMILY_VERSION,
        )
        self.assertTrue(definition.thesis)
        self.assertTrue(definition.falsification)
        with self.assertRaisesRegex(FamilyKernelError, "skip_recent_bars"):
            evaluate_batch(
                family_id=SKIP_RECENT_FAMILY_ID,
                family_version=SKIP_RECENT_FAMILY_VERSION,
                parameters={"formation_lookback_bars": 4, "skip_recent_bars": 4},
                bars=[bar(1, 100), bar(2, 110), bar(3, 120), bar(4, 130), bar(5, 140)],
            )

    def test_predeclared_sma_mean_reversion_family_has_golden_vectors_and_version_identity(self) -> None:
        decisions = evaluate_batch(
            family_id=SMA_FAMILY_ID,
            family_version=SMA_FAMILY_VERSION,
            parameters=SMA_PARAMETERS,
            bars=[bar(1, 100), bar(2, 110), bar(3, 90)],
        )

        self.assertEqual(
            [
                (item.ts_event_ns, item.score, item.target_intent, item.reason)
                for item in decisions
            ],
            [
                (2, "0.047619047619", "FLAT", "CLOSE_AT_OR_ABOVE_SMA_DISCOUNT_THRESHOLD"),
                (3, "-0.1", "LONG", "CLOSE_BELOW_SMA_DISCOUNT_THRESHOLD"),
            ],
        )
        self.assertEqual(
            DEFAULT_REGISTRY.resolve(SMA_FAMILY_ID, SMA_FAMILY_VERSION).family_version,
            SMA_FAMILY_VERSION,
        )
        definition = DEFAULT_REGISTRY.resolve(SMA_FAMILY_ID, SMA_FAMILY_VERSION)
        self.assertTrue(definition.thesis)
        self.assertTrue(definition.falsification)

    def test_sma_mean_reversion_compares_the_canonically_rounded_score(self) -> None:
        decisions = evaluate_batch(
            family_id=SMA_FAMILY_ID,
            family_version=SMA_FAMILY_VERSION,
            parameters=SMA_PARAMETERS,
            bars=[bar(1, 1.0), bar(2, 0.9047619047611792)],
        )

        self.assertEqual(decisions[0].score, "-0.05")
        self.assertEqual(decisions[0].target_intent, "FLAT")
        self.assertEqual(
            decisions[0].reason,
            "CLOSE_AT_OR_ABOVE_SMA_DISCOUNT_THRESHOLD",
        )

    def test_momentum_batch_emits_one_canonical_decision_per_eligible_bar(self) -> None:
        decisions = evaluate_batch(
            family_id=FAMILY_ID,
            family_version=FAMILY_VERSION,
            parameters=PARAMETERS,
            bars=[bar(1, 100), bar(2, 104), bar(3, 110), bar(4, 100)],
        )

        self.assertEqual(
            [
                (
                    decision.ts_event_ns,
                    decision.score,
                    decision.target_intent,
                    decision.reason,
                    decision.family_id,
                    decision.family_version,
                    decision.kernel_version,
                    decision.kernel_hash,
                )
                for decision in decisions
            ],
            [
                (
                    2,
                    "0.04",
                    "FLAT",
                    "MOMENTUM_AT_OR_BELOW_ENTRY_THRESHOLD",
                    FAMILY_ID,
                    FAMILY_VERSION,
                    KERNEL_VERSION,
                    KERNEL_HASH,
                ),
                (
                    3,
                    "0.057692307692",
                    "LONG",
                    "MOMENTUM_ABOVE_ENTRY_THRESHOLD",
                    FAMILY_ID,
                    FAMILY_VERSION,
                    KERNEL_VERSION,
                    KERNEL_HASH,
                ),
                (
                    4,
                    "-0.090909090909",
                    "FLAT",
                    "MOMENTUM_AT_OR_BELOW_ENTRY_THRESHOLD",
                    FAMILY_ID,
                    FAMILY_VERSION,
                    KERNEL_VERSION,
                    KERNEL_HASH,
                ),
            ],
        )
        self.assertEqual(len({decision.signal_id for decision in decisions}), 3)

    def test_signal_id_binds_every_identity_bearing_field(self) -> None:
        decision = evaluate_batch(
            family_id=FAMILY_ID,
            family_version=FAMILY_VERSION,
            parameters=PARAMETERS,
            bars=[bar(1, 100), bar(2, 110)],
        )[0]
        base = {
            "family_id": decision.family_id,
            "family_version": decision.family_version,
            "kernel_hash": decision.kernel_hash,
            "kernel_version": decision.kernel_version,
            "parameters": PARAMETERS,
            "reason": decision.reason,
            "score": decision.score,
            "target_intent": decision.target_intent,
            "ts_event_ns": decision.ts_event_ns,
        }
        mutations = {
            "family_id": "other-family",
            "family_version": "other-version",
            "kernel_hash": "f" * 64,
            "kernel_version": "other-kernel",
            "parameters": {**PARAMETERS, "entry_threshold": 0.06},
            "reason": "OTHER_REASON",
            "score": "0.100000000001",
            "target_intent": "FLAT",
            "ts_event_ns": decision.ts_event_ns + 1,
        }

        self.assertEqual(derive_signal_id(**base), decision.signal_id)
        for field, value in mutations.items():
            with self.subTest(field=field):
                self.assertNotEqual(
                    derive_signal_id(**{**base, field: value}),
                    decision.signal_id,
                )

    def test_registry_accepts_a_second_plain_parameter_shape(self) -> None:
        def validate(parameters):
            if set(parameters) != {"minimum_close"}:
                raise FamilyKernelError("invalid minimum-close parameters")
            return {"minimum_close": float(parameters["minimum_close"])}

        registry = FamilyRegistry()
        registry.register(
            FamilyDefinition(
                family_id="minimum-close-long-flat",
                family_version="minimum-close-long-flat-v1",
                warmup_bars=lambda _parameters: 1,
                validate_parameters=validate,
                evaluate=lambda bars, parameters: FamilyEvaluation(
                    score=bars[-1].close,
                    target_intent=(
                        "LONG" if bars[-1].close > parameters["minimum_close"] else "FLAT"
                    ),
                    reason="CLOSE_COMPARED_WITH_MINIMUM",
                ),
            )
        )

        decisions = evaluate_batch(
            family_id="minimum-close-long-flat",
            family_version="minimum-close-long-flat-v1",
            parameters={"minimum_close": 100},
            bars=[bar(1, 99), bar(2, 101)],
            registry=registry,
        )

        self.assertEqual([item.target_intent for item in decisions], ["FLAT", "LONG"])
        self.assertEqual([item.score for item in decisions], ["99", "101"])
        original = registry.resolve(
            "minimum-close-long-flat",
            "minimum-close-long-flat-v1",
        )
        registry.register(
            FamilyDefinition(
                family_id=original.family_id,
                family_version="minimum-close-long-flat-v2",
                warmup_bars=original.warmup_bars,
                validate_parameters=original.validate_parameters,
                evaluate=original.evaluate,
            )
        )
        self.assertEqual(
            registry.resolve("minimum-close-long-flat", "minimum-close-long-flat-v2").family_version,
            "minimum-close-long-flat-v2",
        )
        with self.assertRaisesRegex(FamilyKernelError, "already registered"):
            registry.register(original)


class IncrementalFamilyEvaluatorTests(unittest.TestCase):
    def test_batch_incremental_and_restart_emit_identical_canonical_bytes(self) -> None:
        bars = [bar(1, 100), bar(2, 104), bar(3, 110), bar(4, 100)]
        batch = evaluate_batch(
            family_id=FAMILY_ID,
            family_version=FAMILY_VERSION,
            parameters=PARAMETERS,
            bars=bars,
        )
        incremental = IncrementalFamilyEvaluator(
            family_id=FAMILY_ID,
            family_version=FAMILY_VERSION,
            parameters=PARAMETERS,
        )
        first = [item for item in (incremental.push(value) for value in bars[:3]) if item]
        snapshot = incremental.snapshot()
        restored = restore_incremental(snapshot)
        second = restored.push(bars[3])

        self.assertIsNotNone(second)
        resumed = [*first, second]
        self.assertEqual(
            [canonical_decision_bytes(item) for item in resumed],
            [canonical_decision_bytes(item) for item in batch],
        )
        self.assertEqual(restored.snapshot(), IncrementalFamilyEvaluator.restore(restored.snapshot()).snapshot())

    def test_duplicate_or_out_of_order_bar_fails_without_emitting_a_signal(self) -> None:
        evaluator = IncrementalFamilyEvaluator(
            family_id=FAMILY_ID,
            family_version=FAMILY_VERSION,
            parameters=PARAMETERS,
        )
        evaluator.push(bar(1, 100))
        emitted = evaluator.push(bar(2, 110))
        self.assertIsNotNone(emitted)

        for value in (bar(2, 110), bar(1, 100)):
            with self.subTest(timestamp=value.ts_event_ns):
                with self.assertRaisesRegex(FamilyKernelError, "strictly increasing"):
                    evaluator.push(value)
        self.assertEqual(evaluator.last_ts_event_ns, 2)

    def test_snapshot_fails_closed_on_noncanonical_or_wrong_kernel_identity(self) -> None:
        evaluator = IncrementalFamilyEvaluator(
            family_id=FAMILY_ID,
            family_version=FAMILY_VERSION,
            parameters=PARAMETERS,
        )
        evaluator.push(bar(1, 100))
        snapshot = evaluator.snapshot()

        with self.assertRaisesRegex(FamilyKernelError, "canonical"):
            restore_incremental(snapshot.rstrip(b"\n") + b" \n")
        with self.assertRaisesRegex(FamilyKernelError, "kernel identity"):
            restore_incremental(snapshot.replace(KERNEL_HASH.encode(), b"f" * 64))


if __name__ == "__main__":
    unittest.main()
