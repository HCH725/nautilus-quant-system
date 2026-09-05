from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import platform
from tempfile import TemporaryDirectory
import sqlite3
import unittest

from nautilus_trader.persistence import ParquetDataCatalog

import nautilus_trader
from nautilus_quant.nautilus_io import make_bar
from nautilus_quant.paper_runtime import (
    PaperRuntimeError,
    RuntimeEvidenceStore,
    _fixture_instrument,
    build_paper_node,
    build_shadow_node,
    build_strategy_freeze,
    forced_long_flat_fixture,
    load_paper_policy,
    reconcile_closed_bars,
)
from nautilus_quant.strategy_families import (
    ClosedBar,
    KERNEL_HASH,
    KERNEL_VERSION,
    evaluate_batch,
)
from nautilus_quant.strategy_candidate import load_strategy_candidate
from nautilus_quant.live_strategy import FamilyStrategy, load_risk_execution_policy


ROOT = Path(__file__).resolve().parents[1]
PAPER_POLICY_PATH = ROOT / "config/strategy_paper_policy.json"
RISK_POLICY_PATH = ROOT / "config/strategy_risk_execution_policy.json"
FAMILY_ID = "lookback-momentum-long-flat"
FAMILY_VERSION = "lookback-momentum-long-flat-v1"
PARAMETERS = {"entry_threshold": 0.05, "lookback_bars": 2}
SHA = "a" * 64


def bar(timestamp: int, close: float) -> ClosedBar:
    return ClosedBar(timestamp, close, close, close, close, 1.0)


def admission() -> dict[str, object]:
    return {
        "bar_type": "BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",
        "candidate_id": "1" * 64,
        "code_commit": "2" * 40,
        "data_as_of_ns": 10,
        "data_snapshot_id": "3" * 64,
        "family_id": FAMILY_ID,
        "family_version": FAMILY_VERSION,
        "historical_verdict_id": "4" * 64,
        "hypothesis_id": "5" * 64,
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "instrument_metadata_id": "6" * 64,
        "kernel_hash": "7" * 64,
        "kernel_version": "strategy-family-kernel-v1",
        "parameters": PARAMETERS,
        "robustness_verdict_id": "8" * 64,
        "robustness_action": "ADVANCE",
        "runtime_id": "9" * 64,
        "strategy_id": "a" * 64,
    }


class PaperPolicyAndFreezeTests(unittest.TestCase):
    def test_policy_and_freeze_are_content_addressed_before_results(self) -> None:
        paper = load_paper_policy(PAPER_POLICY_PATH)
        risk = load_risk_execution_policy(RISK_POLICY_PATH)
        freeze = build_strategy_freeze(admission(), paper, risk)

        self.assertEqual(paper.schema_version, "strategy-paper-policy-v1")
        self.assertGreaterEqual(paper.minimum_completed_bars, 2)
        self.assertGreater(paper.minimum_wall_clock_seconds, 0)
        self.assertEqual(len(freeze.freeze_id), 64)
        self.assertEqual(freeze.risk_policy_id, risk.policy_id)
        self.assertEqual(freeze.paper_policy_id, paper.policy_id)
        self.assertEqual(freeze.inspected_data_boundary_ns, 10)

        changed = admission()
        changed["instrument_metadata_id"] = "b" * 64
        self.assertNotEqual(build_strategy_freeze(changed, paper, risk).freeze_id, freeze.freeze_id)
        blocked = admission()
        blocked["robustness_action"] = "MUTATE"
        with self.assertRaisesRegex(PaperRuntimeError, "ADVANCE"):
            build_strategy_freeze(blocked, paper, risk)


class RuntimeEvidenceTests(unittest.TestCase):
    def test_append_only_store_reads_back_freeze_run_and_terminal_verdict(self) -> None:
        paper = load_paper_policy(PAPER_POLICY_PATH)
        risk = load_risk_execution_policy(RISK_POLICY_PATH)
        freeze = build_strategy_freeze(admission(), paper, risk)
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = RuntimeEvidenceStore(root / "runtime.sqlite3", root / "artifacts")
            store.initialize()
            store.record_freeze(freeze)
            run = store.start_run(
                freeze,
                tier="SHADOW",
                environment="BINANCE_LIVE_DATA_ONLY",
                cohort_start_ns=11,
                instrument_metadata_id="6" * 64,
                mark_price_metadata_id="b" * 64,
                fee_metadata_id="c" * 64,
            )
            verdict = store.finish_run(
                run.run_id,
                paper_policy=paper,
                technical_status="PASS",
                strategy_outcome="NOT_APPLICABLE",
                reason_codes=("SHADOW_BOUNDED_SMOKE_PASS",),
                cohort_end_ns=12,
                completed_bars=2,
                missing_bars=0,
                revised_bars=0,
                terminal_flat=True,
                open_order_count=0,
                restart_count=0,
            )

            self.assertEqual(store.read_freeze(freeze.freeze_id), freeze)
            self.assertEqual(store.read_run(run.run_id), run)
            self.assertEqual(store.read_verdict(verdict.verdict_id), verdict)
            with sqlite3.connect(store.path) as connection:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("UPDATE runtime_runs SET tier = 'PAPER'")
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("DELETE FROM runtime_verdicts")

    def test_runtime_store_rejects_canonical_data_paths(self) -> None:
        with self.assertRaisesRegex(PaperRuntimeError, "canonical data"):
            RuntimeEvidenceStore(ROOT / "data/runtime.sqlite3", ROOT / "var/runtime")

    def test_paper_pass_requires_full_prospective_cohort_and_restart(self) -> None:
        paper = load_paper_policy(PAPER_POLICY_PATH)
        risk = load_risk_execution_policy(RISK_POLICY_PATH)
        freeze = build_strategy_freeze(admission(), paper, risk)
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = RuntimeEvidenceStore(root / "runtime.sqlite3", root / "artifacts")
            store.initialize()
            store.record_freeze(freeze)
            run = store.start_run(
                freeze,
                tier="PAPER",
                environment="BINANCE_LIVE_SANDBOX_EXECUTION",
                cohort_start_ns=11,
                instrument_metadata_id="6" * 64,
                mark_price_metadata_id="b" * 64,
                fee_metadata_id="c" * 64,
            )

            with self.assertRaisesRegex(PaperRuntimeError, "prospective cohort"):
                store.finish_run(
                    run.run_id,
                    paper_policy=paper,
                    technical_status="PASS",
                    strategy_outcome="PASS",
                    reason_codes=("PAPER_COHORT_PASS",),
                    cohort_end_ns=12,
                    completed_bars=paper.minimum_completed_bars,
                    missing_bars=0,
                    revised_bars=0,
                    terminal_flat=True,
                    open_order_count=0,
                    restart_count=0,
                )

            verdict = store.finish_run(
                run.run_id,
                paper_policy=paper,
                technical_status="PASS",
                strategy_outcome="PASS",
                reason_codes=("PAPER_COHORT_PASS",),
                cohort_end_ns=11 + paper.minimum_wall_clock_seconds * 1_000_000_000,
                completed_bars=paper.minimum_completed_bars,
                missing_bars=0,
                revised_bars=0,
                terminal_flat=True,
                open_order_count=0,
                restart_count=paper.minimum_restart_count,
            )
            self.assertEqual(verdict.restart_count, paper.minimum_restart_count)


class PaperCompositionAndParityTests(unittest.TestCase):
    def test_shadow_has_no_execution_client_and_paper_has_active_risk_sandbox(self) -> None:
        risk = load_risk_execution_policy(RISK_POLICY_PATH)
        shadow = build_shadow_node(risk)
        paper = build_paper_node(risk)
        try:
            self.assertFalse(shadow.execution_registered)
            self.assertEqual(shadow.node.cache.orders(), [])
            self.assertTrue(paper.execution_registered)
            self.assertFalse(paper.risk_config.bypass)
            self.assertEqual(paper.data_environment, "LIVE")
            self.assertEqual(paper.execution_environment, "SANDBOX")
        finally:
            shadow.node.dispose()
            paper.node.dispose()

    def test_live_normalized_bars_and_signal_ids_reconcile_exactly(self) -> None:
        bars = (bar(1, 100), bar(2, 110), bar(3, 100))
        decisions = evaluate_batch(
            family_id=FAMILY_ID,
            family_version=FAMILY_VERSION,
            parameters=PARAMETERS,
            bars=bars,
        )
        result = reconcile_closed_bars(bars, bars, decisions, decisions)
        self.assertEqual(result, {"bar_count": 3, "signal_count": 2, "status": "PASS"})

        with self.assertRaisesRegex(PaperRuntimeError, "normalized bar bytes"):
            reconcile_closed_bars(bars, (*bars[:2], bar(3, 101)), decisions, decisions)

    def test_historical_candidate_batch_and_live_share_exact_decisions(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog_path = root / "catalog"
            catalog_path.mkdir()
            catalog = ParquetDataCatalog(str(catalog_path))
            catalog.write_instruments([_fixture_instrument()])
            nautilus_bars = [
                make_bar(
                    instrument_id="BTCUSDT-PERP.BINANCE",
                    interval="1h",
                    price_type="LAST",
                    price_precision=2,
                    size_precision=3,
                    open_=str(close),
                    high=str(close),
                    low=str(close),
                    close=str(close),
                    volume="1",
                    close_ms=hour * 3_600_000,
                )
                for hour, close in enumerate((100, 110, 100), start=1)
            ]
            catalog.write_bars(nautilus_bars)
            bars = tuple(
                ClosedBar(
                    item.ts_event,
                    item.open.as_double(),
                    item.high.as_double(),
                    item.low.as_double(),
                    item.close.as_double(),
                    item.volume.as_double(),
                )
                for item in nautilus_bars
            )
            batch = evaluate_batch(
                family_id=FAMILY_ID,
                family_version=FAMILY_VERSION,
                parameters=PARAMETERS,
                bars=bars,
            )
            digest = sha256()
            bar_type = "BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL"
            instrument_id = "BTCUSDT-PERP.BINANCE"
            for path in sorted((catalog_path / "data" / "bars" / bar_type).glob("*.parquet")):
                digest.update(path.relative_to(catalog_path).as_posix().encode())
                digest.update(b"\0")
                digest.update(path.read_bytes())
            candidate = {
                "bar_type": bar_type,
                "evaluation_context_id": "e" * 64,
                "instrument_id": instrument_id,
                "runtime": {
                    "nautilus_trader": nautilus_trader.__version__,
                    "python_version": platform.python_version(),
                },
                "schema_version": "strategy-candidate-v1",
                "source": {
                    "data_as_of_ns": bars[-1].ts_event_ns,
                    "data_snapshot_id": digest.hexdigest(),
                    "first_ts_event_ns": bars[0].ts_event_ns,
                    "last_ts_event_ns": bars[-1].ts_event_ns,
                    "row_count": len(bars),
                    "sha256": digest.hexdigest(),
                },
                "strategy": {
                    "decision_timing": "bar-close; effective no earlier than next event",
                    "family_id": FAMILY_ID,
                    "family_version": FAMILY_VERSION,
                    "kernel_hash": KERNEL_HASH,
                    "kernel_version": KERNEL_VERSION,
                    "parameters": PARAMETERS,
                },
                "truth_status": "provisional",
            }
            candidate_path = root / "candidate.json"
            candidate_path.write_text(
                json.dumps(candidate, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n",
            )
            loaded, candidate_id = load_strategy_candidate(candidate_path)
            self.assertNotIn("signals", loaded)
            self.assertEqual(candidate_id, sha256(candidate_path.read_bytes()).hexdigest())
            self.assertEqual(loaded["schema_version"], "strategy-candidate-v1")
            source = loaded["source"]
            assert isinstance(source, dict)
            self.assertEqual(source["sha256"], digest.hexdigest())
            self.assertEqual(source["data_snapshot_id"], digest.hexdigest())
            live = FamilyStrategy(
                family_id=FAMILY_ID,
                family_version=FAMILY_VERSION,
                parameters=PARAMETERS,
                risk_policy=load_risk_execution_policy(RISK_POLICY_PATH),
                mode="SHADOW",
            )
            live_decisions = tuple(
                decision for item in bars if (decision := live.on_closed_bar(item)) is not None
            )

            self.assertEqual(live_decisions, batch)

    def test_forced_long_flat_crosses_real_nautilus_accounting_and_finishes_flat(self) -> None:
        result = forced_long_flat_fixture(load_risk_execution_policy(RISK_POLICY_PATH))

        self.assertEqual([fill["intent"] for fill in result["fills"]], ["LONG", "FLAT"])
        self.assertTrue(result["terminal_flat"])
        self.assertEqual(result["open_order_count"], 0)
        self.assertEqual(result["position_count"], 0)
        self.assertEqual(result["account_type"], "MARGIN")
        self.assertEqual(result["oms_type"], "NETTING")
        self.assertEqual(result["strategy_class"], "FamilyStrategy")


if __name__ == "__main__":
    unittest.main()
