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
    canonical_decision_bytes,
    derive_signal_id,
    evaluate_batch,
    restore_incremental,
)


FAMILY_ID = "lookback-momentum-long-flat"
FAMILY_VERSION = "lookback-momentum-long-flat-v1"
PARAMETERS = {"entry_threshold": 0.05, "lookback_bars": 2}


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
