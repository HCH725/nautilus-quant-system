from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import json
import unittest

from nautilus_quant.live_strategy import (
    FamilyStrategy,
    LiveStrategyError,
    load_risk_execution_policy,
)
from nautilus_quant.strategy_families import ClosedBar, evaluate_batch


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config/strategy_risk_execution_policy.json"
FAMILY_ID = "lookback-momentum-long-flat"
FAMILY_VERSION = "lookback-momentum-long-flat-v1"
PARAMETERS = {"entry_threshold": 0.05, "lookback_bars": 2}


def bar(timestamp: int, close: float) -> ClosedBar:
    return ClosedBar(timestamp, close, close, close, close, 1.0)


class RiskExecutionPolicyTests(unittest.TestCase):
    def test_tracked_policy_is_content_addressed_and_fail_closed(self) -> None:
        policy = load_risk_execution_policy(POLICY_PATH)

        self.assertEqual(len(policy.policy_id), 64)
        self.assertEqual(policy.schema_version, "strategy-risk-execution-policy-v1")
        self.assertFalse(policy.allow_live_execution)
        self.assertGreater(policy.maximum_loss, 0)
        self.assertGreater(policy.maximum_notional, 0)
        self.assertEqual(policy.position_intents, ("FLAT", "LONG"))

        with self.assertRaisesRegex(LiveStrategyError, "policy hash"):
            load_risk_execution_policy(POLICY_PATH.read_bytes() + b" ")


class FamilyStrategyTests(unittest.TestCase):
    def strategy(self, *, mode: str = "SHADOW") -> FamilyStrategy:
        return FamilyStrategy(
            family_id=FAMILY_ID,
            family_version=FAMILY_VERSION,
            parameters=PARAMETERS,
            risk_policy=load_risk_execution_policy(POLICY_PATH),
            mode=mode,
        )

    def test_shadow_matches_batch_and_never_emits_order_intents(self) -> None:
        bars = [bar(1, 100), bar(2, 110), bar(3, 100)]
        strategy = self.strategy()

        actual = tuple(
            decision
            for decision in (strategy.on_closed_bar(value) for value in bars)
            if decision is not None
        )

        self.assertEqual(actual, evaluate_batch(
            family_id=FAMILY_ID,
            family_version=FAMILY_VERSION,
            parameters=PARAMETERS,
            bars=bars,
        ))
        self.assertEqual(strategy.order_intents, ())

    def test_restart_preserves_dedupe_and_duplicate_bar_fails_closed(self) -> None:
        strategy = self.strategy(mode="PAPER")
        strategy.on_closed_bar(bar(1, 100))
        long_decision = strategy.on_closed_bar(bar(2, 110))
        self.assertEqual(strategy.order_intents, ((long_decision.signal_id, "LONG"),))

        restored = FamilyStrategy.restore(strategy.snapshot(), load_risk_execution_policy(POLICY_PATH))
        with self.assertRaisesRegex(LiveStrategyError, "strictly increasing"):
            restored.on_closed_bar(bar(2, 110))
        self.assertEqual(restored.order_intents, strategy.order_intents)

        flat_decision = restored.on_closed_bar(bar(3, 100))
        self.assertEqual(restored.order_intents[-1], (flat_decision.signal_id, "FLAT"))

    def test_stale_data_and_maximum_loss_force_flat_once(self) -> None:
        policy = load_risk_execution_policy(POLICY_PATH)
        strategy = self.strategy(mode="PAPER")
        strategy.on_closed_bar(bar(1, 100))
        strategy.on_closed_bar(bar(2, 110))

        self.assertEqual(strategy.trip_circuit_breaker("STALE_DATA"), "FLAT")
        self.assertIsNone(strategy.trip_circuit_breaker("STALE_DATA"))
        self.assertEqual(strategy.technical_status, "ERROR")
        self.assertEqual(strategy.order_intents[-1][1], "FLAT")

        loss_strategy = FamilyStrategy(
            family_id=FAMILY_ID,
            family_version=FAMILY_VERSION,
            parameters=PARAMETERS,
            risk_policy=replace(policy, maximum_loss=1),
            mode="PAPER",
        )
        loss_strategy.on_closed_bar(bar(1, 100))
        loss_strategy.on_closed_bar(bar(2, 110))
        self.assertEqual(loss_strategy.observe_account_loss(1), "FLAT")
        self.assertEqual(loss_strategy.reason_codes[-1], "MAXIMUM_LOSS")

    def test_quantity_gap_staleness_and_shutdown_policy_fail_closed(self) -> None:
        policy = load_risk_execution_policy(POLICY_PATH)
        with self.assertRaisesRegex(LiveStrategyError, "maximum_quantity"):
            FamilyStrategy(
                family_id=FAMILY_ID,
                family_version=FAMILY_VERSION,
                parameters=PARAMETERS,
                risk_policy=policy,
                mode="PAPER",
                quantity="0.002",
            )

        strategy = FamilyStrategy(
            family_id=FAMILY_ID,
            family_version=FAMILY_VERSION,
            parameters=PARAMETERS,
            risk_policy=replace(policy, stale_data_after_seconds=1),
            mode="PAPER",
            quantity="0.001",
            expected_interval_ns=1,
        )
        strategy.on_closed_bar(bar(1, 100))
        strategy.on_closed_bar(bar(2, 110))
        with self.assertRaisesRegex(LiveStrategyError, "gap"):
            strategy.on_closed_bar(bar(4, 110))
        self.assertEqual(strategy.check_stale(1_000_000_003), "FLAT")
        self.assertEqual(strategy.order_intents[-1][1], "FLAT")

        shutdown = self.strategy(mode="PAPER")
        shutdown.on_closed_bar(bar(1, 100))
        shutdown.on_closed_bar(bar(2, 110))
        self.assertEqual(shutdown.shutdown_flatten(), "FLAT")
        self.assertEqual(shutdown.reason_codes[-1], "SHUTDOWN_FLATTEN")
        self.assertEqual(shutdown.technical_status, "PASS")

    def test_policy_rejects_limits_that_do_not_close_the_risk_envelope(self) -> None:
        policy = json.loads(POLICY_PATH.read_bytes())
        policy["maximum_notional"] = "101"
        payload = (json.dumps(policy, separators=(",", ":"), sort_keys=True) + "\n").encode()

        with self.assertRaisesRegex(LiveStrategyError, "exposure"):
            load_risk_execution_policy(payload)

        self.assertEqual(load_risk_execution_policy(POLICY_PATH).maximum_quantity, Decimal("0.001"))


if __name__ == "__main__":
    unittest.main()
