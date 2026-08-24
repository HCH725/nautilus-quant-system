from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import os
from types import SimpleNamespace
from unittest.mock import Mock, patch
import hashlib
import sqlite3
from tempfile import TemporaryDirectory
from typing import Any
import unittest

import nautilus_quant.strategy_lab as strategy_lab
import nautilus_quant.strategy_robustness as strategy_robustness
from nautilus_quant.candidate_backtest import CandidateBacktestError, CandidateBacktestRequest
from nautilus_quant.strategy_robustness import (
    deterministic_regime_label,
    build_action_v1,
    build_feedback_v2,
    build_robustness_verdict_v2,
    canonical_json,
    evaluate_robustness_matrix,
    generate_robustness_matrix,
    generate_robustness_windows,
    load_action_v1,
    load_feedback_v2,
    load_robustness_policy,
    main,
    parameter_neighborhood,
    robustness_evaluation_context_id,
    FormalNautilusEvaluator,
    RobustnessCellResult,
)


ROOT = Path(__file__).resolve().parents[1]


def _trial_context(
    *,
    generated_count: int = 1,
    executed_count: int = 1,
    deduped_count: int = 0,
    rejected_count: int = 0,
    surviving_count: int = 1,
    technical_invalid_count: int = 0,
    candidate_count: int = 1,
) -> dict[str, object]:
    return {
        "campaign_id": "b" * 64,
        "candidate_count": candidate_count,
        "cohort_id": "c" * 64,
        "data_as_of_ns": 13,
        "deduped_count": deduped_count,
        "executed_count": executed_count,
        "family_count": 1,
        "family_id": "lookback-momentum-long-flat",
        "family_version": "lookback-momentum-long-flat-v1",
        "generated_count": generated_count,
        "generation_budget": generated_count,
        "maximum_candidates": generated_count,
        "parameter_search_policy_id": "e" * 64,
        "rejected_count": rejected_count,
        "search_space": {"entry_threshold": [0.02], "lookback_bars": [10]},
        "surviving_count": surviving_count,
        "technical_invalid_count": technical_invalid_count,
        "terminal_census_complete": True,
        "trial_census_id": "d" * 64,
    }


def _seed_persisted_mutation_source(
    root: Path,
    *,
    parameters: dict[str, float | int] | None = None,
) -> dict[str, Any]:
    parameters = parameters or {"entry_threshold": 0.02, "lookback_bars": 10}
    ledger = strategy_lab.StrategyLedger(root / "ledger.sqlite3")
    ledger.initialize()
    source_path = root / "source-hypothesis.json"
    source_path.write_bytes(canonical_json({
        "bar_type": "BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",
        "based_on_verdict_id": None,
        "falsification": "Economic instability rejects the hypothesis",
        "family_version": "lookback-momentum-long-flat-v1",
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "parameters": parameters,
        "parent_strategy_id": None,
        "schema_version": "strategy-hypothesis-v2",
        "strategy_family": "lookback-momentum-long-flat",
        "thesis": "Positive momentum persists",
    }))
    source = strategy_lab.load_strategy_hypothesis(source_path)
    ledger.record_hypothesis(source)
    candidate = {
        "bar_type": "BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",
        "evaluation_context_id": "7" * 64,
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "source": {"data_as_of_ns": 13, "data_snapshot_id": "6" * 64},
        "strategy": {
            "family_id": source.family_id,
            "family_version": source.family_version,
            "parameters": dict(source.parameters.values),
        },
    }
    run_directory = root / "historical-run"
    run_directory.mkdir()
    candidate_path = run_directory / "candidate.json"
    candidate_path.write_bytes(canonical_json(candidate))
    candidate_id = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
    pybroker_path = run_directory / "pybroker-result.json"
    pybroker_path.write_bytes(canonical_json({"candidate_id": candidate_id}))
    parity_path = run_directory / "signal-parity.json"
    parity_path.write_bytes(canonical_json({"outcome": "PASS"}))
    historical_path = run_directory / "nautilus-verdict.json"
    historical_path.write_bytes(canonical_json({"status": "EVALUATED"}))
    base_identity = strategy_lab.ExperimentIdentity(
        source.strategy_id,
        "3" * 64,
        "4" * 64,
        "historical-engine",
        "5" * 64,
        13,
        "6" * 64,
    )
    historical_experiment_id = ledger.record_experiment(source.hypothesis_id, base_identity)
    campaign_document = {
        "approved_bar_types": ["BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL"],
        "approved_instruments": ["BTCUSDT-PERP.BINANCE"],
        "data_as_of_ns": 13,
        "family_id": source.family_id,
        "family_version": source.family_version,
        "generation_budget": 1,
        "maximum_candidates": 1,
        "parameter_search_policy_id": "e" * 64,
        "schema_version": "strategy-campaign-v1",
        "screen_policy_id": "f" * 64,
        "search_space": {name: [value] for name, value in parameters.items()},
        "seed": 42,
    }
    campaign_payload = canonical_json(campaign_document)
    campaign_id = hashlib.sha256(campaign_payload).hexdigest()
    historical_record = strategy_lab.VerdictRecord(
        historical_experiment_id,
        "SUCCESS",
        "RETAIN_FOR_RESEARCH",
        str(historical_path),
        hashlib.sha256(historical_path.read_bytes()).hexdigest(),
    )
    historical_verdict_id = strategy_lab._verdict_record_id(historical_record)
    with closing(sqlite3.connect(ledger.path)) as connection, connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO campaigns VALUES (?, ?, ?, ?, ?)",
            (campaign_id, campaign_payload.decode(), "f" * 64, 13, "3" * 64),
        )
        connection.execute(
            "INSERT INTO campaign_trials VALUES (?, 0, ?, ?, 'SURVIVED', 1, ?, ?)",
            (
                campaign_id,
                source.strategy_id,
                candidate_id,
                historical_experiment_id,
                canonical_json(["SCREEN_PASSED"]).decode(),
            ),
        )
        connection.execute(
            "INSERT INTO stage_results VALUES (?, ?, 'PyBroker completed', 'PASSED', ?, ?, ?)",
            (
                historical_experiment_id,
                source.strategy_id,
                "PYBROKER_PROCESS_COMPLETED",
                str(pybroker_path),
                hashlib.sha256(pybroker_path.read_bytes()).hexdigest(),
            ),
        )
        connection.execute(
            "INSERT INTO signal_parity_results VALUES (?, ?, ?, ?, ?, 'PASS', ?, NULL, ?, ?)",
            (
                "9" * 64,
                historical_experiment_id,
                candidate_id,
                "7" * 64,
                "6" * 64,
                "SIGNAL_PARITY_PASS",
                str(parity_path),
                hashlib.sha256(parity_path.read_bytes()).hexdigest(),
            ),
        )
        connection.execute(
            "INSERT INTO verdicts VALUES (?, ?, ?, 'SUCCESS', ?, ?, ?)",
            (
                historical_verdict_id,
                historical_experiment_id,
                source.strategy_id,
                "RETAIN_FOR_RESEARCH",
                str(historical_path),
                hashlib.sha256(historical_path.read_bytes()).hexdigest(),
            ),
        )
    historical_document = {
        "candidate_id": candidate_id,
        "code_commit": "c" * 40,
        "experiment_id": historical_experiment_id,
        "hypothesis_id": source.hypothesis_id,
        "reason_codes": ["RETAIN_FOR_RESEARCH"],
        "source": {"sha256": "6" * 64},
        "strategy_id": source.strategy_id,
    }
    return {
        "base_identity": base_identity,
        "campaign_id": campaign_id,
        "candidate": candidate,
        "candidate_id": candidate_id,
        "historical_document": historical_document,
        "historical_verdict_id": historical_verdict_id,
        "ledger": ledger,
        "source": source,
    }


def _run_persisted_mutation(
    root: Path,
    fixture: dict[str, Any],
    policy: Any,
) -> dict[str, Any]:
    candidate = fixture["candidate"]
    candidate_id = fixture["candidate_id"]

    def reject(_request: object, _cell: object) -> SimpleNamespace:
        verdict = {
            "execution": {"slippage_status": "modeled_one_tick"},
            "funding": {"truth_status": "official"},
            "net_account_delta": "-1",
            "realized_balance_drawdown": "0",
            "status": "EVALUATED",
        }
        payload = canonical_json(verdict)
        return SimpleNamespace(
            canonical_bytes=payload,
            verdict=verdict,
            verdict_id=hashlib.sha256(payload).hexdigest(),
        )

    with (
        patch("nautilus_quant.strategy_robustness.load_robustness_policy", return_value=policy),
        patch(
            "nautilus_quant.pybroker_candidate.load_pybroker_candidate",
            return_value=(candidate, candidate_id),
        ),
        patch(
            "nautilus_quant.strategy_lab.load_pybroker_candidate",
            return_value=(candidate, candidate_id),
        ),
        patch(
            "nautilus_quant.strategy_lab.load_candidate_backtest_verdict",
            return_value=fixture["historical_document"],
        ),
        patch(
            "nautilus_quant.candidate_backtest.validated_candidate_source_bars",
            return_value=tuple(
                SimpleNamespace(ts_event=index, close=100 + index)
                for index in range(1, 14)
            ),
        ),
        patch(
            "nautilus_quant.candidate_backtest.run_signal_parity_gate",
            return_value=object(),
        ),
        patch(
            "nautilus_quant.candidate_backtest._load_policy",
            return_value=SimpleNamespace(historical_start_ns=1),
        ),
        patch("nautilus_quant.strategy_lab._hash_tree", return_value="3" * 64),
    ):
        return strategy_robustness.run_persisted_robustness(
            ledger=fixture["ledger"],
            campaign_id=fixture["campaign_id"],
            market_data_path=Path("market-data.json"),
            catalog_path=Path("catalog"),
            funding_path=Path("funding"),
            accounting_policy_path=Path("accounting-policy.json"),
            robustness_policy_path=Path("robustness-policy.json"),
            artifact_directory=root / "robustness",
            evaluator=reject,
        )


class StrategyRobustnessPolicyTests(unittest.TestCase):
    def test_persisted_run_derives_experiment_and_publishes_complete_chain(self) -> None:
        frozen = load_robustness_policy(ROOT / "config/strategy_robustness_policy.json")
        policy = replace(
            frozen,
            maximum_windows_per_scheme=1,
            expanding_minimum_train_bars=3,
            rolling_train_bars=4,
            test_bars=2,
            step_bars=2,
        )
        candidate_id = "1" * 64
        campaign_id = "b" * 64
        candidate = {
            "bar_type": "BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",
            "instrument_id": "BTCUSDT-PERP.BINANCE",
            "strategy": {
                "family_id": "lookback-momentum-long-flat",
                "family_version": "lookback-momentum-long-flat-v1",
                "parameters": {"entry_threshold": 0.02, "lookback_bars": 10},
            },
        }
        survivor = SimpleNamespace(
            base_identity=strategy_lab.ExperimentIdentity(
                "2" * 64,
                "3" * 64,
                "4" * 64,
                "historical-engine",
                "5" * 64,
                13,
                "6" * 64,
            ),
            candidate_evaluation_context_id="7" * 64,
            candidate_id=candidate_id,
            candidate_path=Path("candidate.json"),
            code_commit="c" * 40,
            hypothesis_id="8" * 64,
            strategy_id="2" * 64,
        )
        ledger = Mock()
        ledger.robustness_trial_context.return_value = {
            **_trial_context(),
            "campaign_id": campaign_id,
            "data_as_of_ns": 13,
        }
        ledger.robustness_survivor_context.return_value = survivor
        ledger.existing_robustness.return_value = None
        ledger.record_experiment.side_effect = (
            lambda _hypothesis_id, identity: strategy_lab.experiment_id(identity)
        )
        ledger.publish_robustness.side_effect = lambda _directory, verdict, feedback, action: SimpleNamespace(
            action_path="action.json",
            action_sha256=hashlib.sha256(canonical_json(action)).hexdigest(),
            economic_status=verdict["economic_status"],
            feedback_path="feedback.json",
            feedback_sha256=hashlib.sha256(canonical_json(feedback)).hexdigest(),
            outcome="PASS",
            reason_codes=tuple(verdict["reason_codes"]),
            technical_status=verdict["technical_status"],
            verdict_path="verdict.json",
            verdict_sha256=hashlib.sha256(canonical_json(verdict)).hexdigest(),
        )
        ledger.robustness_funnel.return_value = {
            "economic_failed": 0,
            "economic_passed": 1,
            "robustness_evaluated": 1,
            "robustness_passed": 1,
            "technical_invalid": 0,
            "technical_valid": 1,
        }

        def evaluate(request: object, cell: object) -> SimpleNamespace:
            self.assertEqual(getattr(request, "policy_path"), Path("accounting-policy.json"))
            self.assertGreaterEqual(getattr(cell, "window").train_start_ns, 3)
            verdict = {
                "execution": {"slippage_status": "modeled_one_tick"},
                "funding": {"truth_status": "official"},
                "net_account_delta": "1",
                "realized_balance_drawdown": "0",
                "status": "EVALUATED",
            }
            payload = canonical_json(verdict)
            return SimpleNamespace(
                canonical_bytes=payload,
                verdict=verdict,
                verdict_id=hashlib.sha256(payload).hexdigest(),
            )

        with (
            TemporaryDirectory() as temporary,
            patch("nautilus_quant.strategy_robustness.load_robustness_policy", return_value=policy),
            patch(
                "nautilus_quant.pybroker_candidate.load_pybroker_candidate",
                return_value=(candidate, candidate_id),
            ),
            patch(
                "nautilus_quant.candidate_backtest.validated_candidate_source_bars",
                return_value=tuple(
                    SimpleNamespace(ts_event=index, close=100 + index)
                    for index in range(1, 14)
                ),
            ),
            patch(
                "nautilus_quant.candidate_backtest.run_signal_parity_gate",
                return_value=object(),
            ),
            patch(
                "nautilus_quant.candidate_backtest._load_policy",
                return_value=SimpleNamespace(historical_start_ns=3),
            ),
            patch(
                "nautilus_quant.strategy_lab._hash_tree",
                return_value="3" * 64,
            ),
        ):
            summary = strategy_robustness.run_persisted_robustness(
                ledger=ledger,
                campaign_id=campaign_id,
                market_data_path=Path("market-data.json"),
                catalog_path=Path("catalog"),
                funding_path=Path("funding"),
                accounting_policy_path=Path("accounting-policy.json"),
                robustness_policy_path=Path("robustness-policy.json"),
                artifact_directory=Path(temporary),
                evaluator=evaluate,
            )

        recorded_identity = ledger.record_experiment.call_args.args[1]
        self.assertEqual(summary["experiment_id"], strategy_lab.experiment_id(recorded_identity))
        self.assertEqual(summary["campaign_id"], campaign_id)
        self.assertEqual(summary["candidate_id"], candidate_id)
        self.assertEqual(summary["action"], "ADVANCE")
        self.assertEqual(summary["funnel"]["robustness_passed"], 1)
        ledger.publish_robustness.assert_called_once()
        published_verdict = ledger.publish_robustness.call_args.args[1]
        self.assertTrue(all(
            cell["artifact_sha256"] == cell["verdict_id"]
            for cell in published_verdict["cells"]
        ))

    def test_persisted_mutation_publishes_canonical_child_lineage_idempotently(self) -> None:
        policy = replace(
            load_robustness_policy(ROOT / "config/strategy_robustness_policy.json"),
            maximum_windows_per_scheme=1,
            expanding_minimum_train_bars=3,
            rolling_train_bars=4,
            test_bars=2,
            step_bars=2,
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = _seed_persisted_mutation_source(root)
            ledger = fixture["ledger"]
            first = _run_persisted_mutation(root, fixture, policy)

            with closing(sqlite3.connect(ledger.path)) as connection:
                child_strategy_count = connection.execute(
                    "SELECT COUNT(*) FROM strategies WHERE strategy_id != ?",
                    (fixture["source"].strategy_id,),
                ).fetchone()[0]
                child_hypothesis_count = connection.execute(
                    "SELECT COUNT(*) FROM hypotheses WHERE parent_strategy_id = ?",
                    (fixture["source"].strategy_id,),
                ).fetchone()[0]
            self.assertEqual(child_strategy_count, 1)
            self.assertEqual(child_hypothesis_count, 1)

            action = load_action_v1(Path(first["action_path"]).read_bytes())
            with closing(sqlite3.connect(ledger.path)) as connection:
                lineage = connection.execute(
                        """SELECT robustness_lineage.lineage_id,
                        robustness_lineage.robustness_verdict_id,
                        robustness_lineage.action_id,
                        robustness_lineage.child_strategy_id,
                        robustness_lineage.child_hypothesis_id,
                        hypotheses.artifact_path, hypotheses.artifact_sha256
                        FROM robustness_lineage
                        JOIN hypotheses ON
                            hypotheses.hypothesis_id = robustness_lineage.child_hypothesis_id
                            AND hypotheses.strategy_id = robustness_lineage.child_strategy_id
                        WHERE robustness_lineage.robustness_verdict_id = ?""",
                        (first["robustness_verdict_id"],),
                ).fetchone()
                first_counts = tuple(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in (
                        "strategies",
                        "hypotheses",
                        "robustness_lineage",
                        "robustness_results",
                        "campaign_trials",
                    )
                )
            self.assertIsNotNone(lineage)
            self.assertEqual(lineage[1:5], (
                first["robustness_verdict_id"],
                action["action_id"],
                action["child_strategy_id"],
                action["child_hypothesis_id"],
            ))
            self.assertEqual(lineage[6], action["child_hypothesis_id"])
            child = strategy_lab.load_strategy_hypothesis(Path(lineage[5]))
            self.assertEqual(child.strategy_id, action["child_strategy_id"])
            self.assertEqual(child.hypothesis_id, action["child_hypothesis_id"])
            self.assertEqual(child.parent_strategy_id, fixture["source"].strategy_id)
            self.assertEqual(child.based_on_verdict_id, fixture["historical_verdict_id"])
            changed = [
                name
                for name, value in child.parameters.values.items()
                if value != fixture["source"].parameters.values[name]
            ]
            self.assertEqual(changed, [action["changed_dimension"]])

            missing_child_directory = root / "missing-child"
            with self.assertRaisesRegex(ValueError, "child hypothesis"):
                ledger.publish_robustness(
                    missing_child_directory,
                    strategy_robustness.load_robustness_verdict_v2(
                        Path(first["verdict_path"]).read_bytes(),
                    ),
                    load_feedback_v2(Path(first["feedback_path"]).read_bytes()),
                    action,
                )
            self.assertFalse(missing_child_directory.exists())

            second = _run_persisted_mutation(root, fixture, policy)
            with closing(sqlite3.connect(ledger.path)) as connection:
                second_counts = tuple(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in (
                        "strategies",
                        "hypotheses",
                        "robustness_lineage",
                        "robustness_results",
                        "campaign_trials",
                    )
                )

            self.assertEqual(first_counts, second_counts)
            self.assertEqual(first["action_id"], second["action_id"])
            self.assertEqual(first["robustness_verdict_id"], second["robustness_verdict_id"])
            self.assertEqual(first["funnel"], second["funnel"])

    def test_persisted_mutation_rolls_back_child_and_new_artifacts_on_lineage_error(self) -> None:
        policy = replace(
            load_robustness_policy(ROOT / "config/strategy_robustness_policy.json"),
            maximum_windows_per_scheme=1,
            expanding_minimum_train_bars=3,
            rolling_train_bars=4,
            test_bars=2,
            step_bars=2,
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = _seed_persisted_mutation_source(root)
            ledger = fixture["ledger"]
            tables = ("strategies", "hypotheses", "robustness_results", "robustness_lineage")
            with closing(sqlite3.connect(ledger.path)) as connection, connection:
                before = tuple(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in tables
                )
                connection.executescript(
                    """CREATE TRIGGER fail_robustness_lineage
                    BEFORE INSERT ON robustness_lineage
                    BEGIN
                        SELECT RAISE(ABORT, 'forced lineage failure');
                    END;""",
                )

            with self.assertRaisesRegex(sqlite3.IntegrityError, "forced lineage failure"):
                _run_persisted_mutation(root, fixture, policy)

            with closing(sqlite3.connect(ledger.path)) as connection, connection:
                after = tuple(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in tables
                )
                quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
                foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
                connection.execute("DROP TRIGGER fail_robustness_lineage")
            self.assertEqual(after, before)
            self.assertEqual(quick_check, "ok")
            self.assertEqual(foreign_key_errors, [])
            self.assertEqual(list((root / "robustness").rglob("strategy-action-v1.json")), [])

            repaired = _run_persisted_mutation(root, fixture, policy)
            self.assertTrue(Path(repaired["action_path"]).is_file())
            with closing(sqlite3.connect(ledger.path)) as connection:
                repaired_counts = tuple(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in tables
                )
            self.assertEqual(repaired_counts, (2, 2, 1, 1))

    def test_mutation_publish_removes_new_partial_artifacts_on_immutable_conflict(self) -> None:
        policy = replace(
            load_robustness_policy(ROOT / "config/strategy_robustness_policy.json"),
            maximum_windows_per_scheme=1,
            expanding_minimum_train_bars=3,
            rolling_train_bars=4,
            test_bars=2,
            step_bars=2,
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = _seed_persisted_mutation_source(root)
            first = _run_persisted_mutation(root, fixture, policy)
            action = load_action_v1(Path(first["action_path"]).read_bytes())
            child_path = (
                Path(first["action_path"]).parent
                / f"hypothesis-{action['child_hypothesis_id']}.json"
            )
            conflict_directory = root / "conflict" / first["robustness_verdict_id"]
            conflict_directory.mkdir(parents=True)
            conflict_path = conflict_directory / "strategy-feedback-v2.json"
            conflict_path.write_bytes(b"conflict")

            with self.assertRaisesRegex(ValueError, "immutable artifact conflict"):
                fixture["ledger"].publish_robustness(
                    root / "conflict",
                    strategy_robustness.load_robustness_verdict_v2(
                        Path(first["verdict_path"]).read_bytes(),
                    ),
                    load_feedback_v2(Path(first["feedback_path"]).read_bytes()),
                    action,
                    child_hypothesis_payload=child_path.read_bytes(),
                )

            self.assertEqual(conflict_path.read_bytes(), b"conflict")
            self.assertEqual(
                sorted(path.name for path in conflict_directory.iterdir()),
                ["strategy-feedback-v2.json"],
            )

    def test_different_valid_mutation_parameters_produce_different_child_identities(self) -> None:
        policy = replace(
            load_robustness_policy(ROOT / "config/strategy_robustness_policy.json"),
            maximum_windows_per_scheme=1,
            expanding_minimum_train_bars=3,
            rolling_train_bars=4,
            test_bars=2,
            step_bars=2,
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_root = root / "first"
            second_root = root / "second"
            first = _run_persisted_mutation(
                first_root,
                _seed_persisted_mutation_source(first_root),
                policy,
            )
            second = _run_persisted_mutation(
                second_root,
                _seed_persisted_mutation_source(
                    second_root,
                    parameters={"entry_threshold": 0.03, "lookback_bars": 10},
                ),
                policy,
            )

            self.assertNotEqual(first["child_strategy_id"], second["child_strategy_id"])
            self.assertNotEqual(first["child_hypothesis_id"], second["child_hypothesis_id"])

    def test_formal_cli_uses_persisted_campaign_and_writable_ledger(self) -> None:
        ledger = Mock()
        summary = {
            "action": "ADVANCE",
            "campaign_id": "b" * 64,
            "schema_version": "formal-robustness-run-v1",
        }
        with (
            patch("nautilus_quant.strategy_lab.StrategyLedger", return_value=ledger),
            patch(
                "nautilus_quant.strategy_robustness.run_persisted_robustness",
                return_value=summary,
            ) as run,
            patch("builtins.print") as printed,
        ):
            self.assertEqual(
                main(
                    [
                        "--ledger", "ledger.sqlite3",
                        "--campaign-id", "b" * 64,
                        "--market-data", "market-data.json",
                        "--catalog", "catalog",
                        "--funding", "funding",
                        "--accounting-policy", "accounting-policy.json",
                        "--robustness-policy", "robustness-policy.json",
                        "--artifact-directory", "artifacts",
                    ],
                ),
                0,
            )

        ledger.initialize.assert_called_once_with()
        run.assert_called_once_with(
            ledger=ledger,
            campaign_id="b" * 64,
            market_data_path=Path("market-data.json"),
            catalog_path=Path("catalog"),
            funding_path=Path("funding"),
            accounting_policy_path=Path("accounting-policy.json"),
            robustness_policy_path=Path("robustness-policy.json"),
            artifact_directory=Path("artifacts"),
            candidate_id=None,
        )
        printed.assert_called_once_with(canonical_json(summary).decode(), end="")

    def test_repository_policy_is_frozen_canonical_and_bounded(self) -> None:
        policy = load_robustness_policy(ROOT / "config/strategy_robustness_policy.json")

        self.assertEqual(policy.policy_version, "strategy-robustness-decision-v1")
        self.assertEqual(policy.window_schemes, ("expanding", "rolling"))
        self.assertEqual(policy.maximum_windows_per_scheme, 3)
        self.assertEqual(policy.stress_scenarios, (
            "baseline",
            "fee_2x",
            "funding_2x",
            "delay_1_bar",
            "parameter_low",
            "parameter_high",
            "slippage_one_tick",
        ))
        self.assertEqual(policy.unmodeled_metrics, ("DSR", "PBO"))
        self.assertEqual(policy.unmodeled_metric_status, "NOT_MODELED")

    def test_candidate_backtest_request_requires_explicit_card3_identity(self) -> None:
        request = CandidateBacktestRequest(
            candidate_path=Path("candidate.json"),
            catalog_path=Path("catalog"),
            funding_path=Path("funding"),
            policy_path=Path("policy.json"),
            hypothesis_id="a" * 64,
            strategy_id="b" * 64,
            experiment_id="c" * 64,
            code_commit="d" * 40,
            evaluation_start_utc="2024-01-01T00:00:00Z",
            evaluation_end_utc="2024-01-02T00:00:00Z",
            data_as_of_ns=1704153600000000000,
            evaluation_context_id="e" * 64,
        )

        self.assertEqual(request.evaluation_start_utc, "2024-01-01T00:00:00Z")
        self.assertEqual(request.evaluation_end_utc, "2024-01-02T00:00:00Z")
        self.assertEqual(request.data_as_of_ns, 1704153600000000000)
        self.assertEqual(request.evaluation_context_id, "e" * 64)

    def test_candidate_backtest_request_rejects_incomplete_or_non_utc_bounds(self) -> None:
        fields = {
            "candidate_path": Path("candidate.json"),
            "catalog_path": Path("catalog"),
            "funding_path": Path("funding"),
            "policy_path": Path("policy.json"),
            "hypothesis_id": "a" * 64,
            "strategy_id": "b" * 64,
            "experiment_id": "c" * 64,
            "code_commit": "d" * 40,
        }
        with self.assertRaisesRegex(CandidateBacktestError, "all four"):
            CandidateBacktestRequest(
                **fields,
                evaluation_start_utc="2024-01-01T00:00:00Z",
            )
        with self.assertRaisesRegex(CandidateBacktestError, "UTC"):
            CandidateBacktestRequest(
                **fields,
                evaluation_start_utc="2024-01-01T00:00:00+08:00",
                evaluation_end_utc="2024-01-02T00:00:00Z",
                data_as_of_ns=1704153600000000000,
                evaluation_context_id="e" * 64,
            )

    def test_windows_are_bounded_utc_and_have_distinct_contexts(self) -> None:
        frozen = load_robustness_policy(ROOT / "config/strategy_robustness_policy.json")
        policy = replace(
            frozen,
            maximum_windows_per_scheme=2,
            expanding_minimum_train_bars=3,
            rolling_train_bars=4,
            test_bars=2,
            step_bars=2,
        )
        timestamps = tuple(range(1, 15))

        windows = generate_robustness_windows(
            timestamps,
            policy,
            evaluation_start_ns=1,
            evaluation_end_ns=13,
            data_as_of_ns=13,
            evaluation_context_id="f" * 64,
        )

        self.assertEqual([window.scheme for window in windows], [
            "expanding", "expanding", "rolling", "rolling",
        ])
        self.assertTrue(all(window.test_end_ns <= 13 for window in windows))
        self.assertTrue(all(window.train_end_ns < window.test_start_ns for window in windows))
        self.assertEqual(len({window.evaluation_context_id for window in windows}), 4)

    def test_window_change_produces_a_distinct_robustness_experiment(self) -> None:
        policy = replace(
            load_robustness_policy(ROOT / "config/strategy_robustness_policy.json"),
            maximum_windows_per_scheme=1,
            expanding_minimum_train_bars=3,
            rolling_train_bars=4,
            test_bars=2,
            step_bars=2,
        )
        base_windows = generate_robustness_windows(
            tuple(range(1, 14)), policy,
            evaluation_start_ns=1, evaluation_end_ns=13, data_as_of_ns=13,
            evaluation_context_id="f" * 64,
        )
        shifted_windows = generate_robustness_windows(
            tuple(range(1, 14)), policy,
            evaluation_start_ns=2, evaluation_end_ns=13, data_as_of_ns=13,
            evaluation_context_id="f" * 64,
        )
        parameters = {"lookback_bars": 10, "entry_threshold": 0.02}
        base_context = robustness_evaluation_context_id(
            policy,
            generate_robustness_matrix(parameters, base_windows, policy),
        )
        shifted_context = robustness_evaluation_context_id(
            policy,
            generate_robustness_matrix(parameters, shifted_windows, policy),
        )
        base_identity = strategy_lab.ExperimentIdentity(
            "1" * 64, "2" * 64, "3" * 64, "engine-v2", "4" * 64, 13, "5" * 64,
        )

        first = strategy_lab.robustness_experiment_identity(
            base_identity, policy.policy_id, base_context,
        )
        second = strategy_lab.robustness_experiment_identity(
            base_identity, policy.policy_id, shifted_context,
        )

        self.assertNotEqual(base_context, shifted_context)
        self.assertNotEqual(strategy_lab.experiment_id(first), strategy_lab.experiment_id(second))

    def test_regime_label_is_deterministic_and_prioritized_by_volatility(self) -> None:
        policy = load_robustness_policy(ROOT / "config/strategy_robustness_policy.json")

        trend = deterministic_regime_label(
            [SimpleNamespace(close=value) for value in (100, 101, 102, 103)],
            policy,
        )
        range_label = deterministic_regime_label(
            [SimpleNamespace(close=value) for value in (100, 100, 100, 100)],
            policy,
        )
        high_volatility = deterministic_regime_label(
            [SimpleNamespace(close=value) for value in (100, 110, 100, 110)],
            replace(policy, regime_volatility_threshold=Decimal("0.01")),
        )

        self.assertEqual(trend, "TREND")
        self.assertEqual(range_label, "RANGE")
        self.assertEqual(high_volatility, "HIGH_VOLATILITY")

    def test_parameter_neighborhood_does_not_mutate_persisted_candidate_parameters(self) -> None:
        parameters = {"lookback_bars": 10, "entry_threshold": 0.02}

        lower = parameter_neighborhood(parameters, Decimal("-0.1"))
        upper = parameter_neighborhood(parameters, Decimal("0.1"))

        self.assertEqual(parameters, {"lookback_bars": 10, "entry_threshold": 0.02})
        self.assertEqual(lower, {"lookback_bars": 9, "entry_threshold": 0.018})
        self.assertEqual(upper, {"lookback_bars": 11, "entry_threshold": 0.022})

    def test_formal_matrix_calls_one_evaluator_per_cell_and_keeps_candidate_parameters(self) -> None:
        frozen = load_robustness_policy(ROOT / "config/strategy_robustness_policy.json")
        policy = replace(
            frozen,
            maximum_windows_per_scheme=1,
            expanding_minimum_train_bars=3,
            rolling_train_bars=4,
            test_bars=2,
            step_bars=2,
        )
        windows = generate_robustness_windows(
            tuple(range(1, 14)),
            policy,
            evaluation_start_ns=1,
            evaluation_end_ns=13,
            data_as_of_ns=13,
            evaluation_context_id="f" * 64,
        )
        parameters = {"lookback_bars": 10, "entry_threshold": 0.02}
        calls: list[str] = []

        def evaluator(_request: object, cell: object) -> dict[str, object]:
            calls.append(getattr(cell, "cell_id"))
            return {
                "execution": {
                    "slippage_status": (
                        "modeled_one_tick"
                        if getattr(cell, "cost_policy").slippage_model == "one_tick"
                        else "modeled"
                    ),
                },
                "funding": {"truth_status": "official"},
                "net_account_delta": "1.0",
                "realized_balance_drawdown": "0.0",
                "status": "EVALUATED",
            }

        cells, results = evaluate_robustness_matrix(
            SimpleNamespace(parameters=parameters),
            windows,
            policy,
            evaluator=evaluator,
        )

        self.assertEqual(len(windows), 2)
        self.assertEqual(len(cells), 14)
        self.assertEqual(len(results), len(cells))
        self.assertEqual(calls, [cell.cell_id for cell in cells])
        self.assertTrue(all(result.technical_status == "PASS" for result in results))
        self.assertTrue(all(result.economic_status == "PASS" for result in results))
        self.assertEqual(parameters, {"lookback_bars": 10, "entry_threshold": 0.02})
        self.assertEqual(len({cell.evaluation_context_id for cell in cells}), len(cells))
        self.assertEqual(
            {cell.stress_scenario for cell in cells},
            set(policy.stress_scenarios),
        )

    def test_verdict_feedback_and_action_are_identity_bound_and_advance_fails_closed(self) -> None:
        frozen = load_robustness_policy(ROOT / "config/strategy_robustness_policy.json")
        policy = replace(
            frozen,
            maximum_windows_per_scheme=1,
            expanding_minimum_train_bars=3,
            rolling_train_bars=4,
            test_bars=2,
            step_bars=2,
        )
        windows = generate_robustness_windows(
            tuple(range(1, 14)),
            policy,
            evaluation_start_ns=1,
            evaluation_end_ns=13,
            data_as_of_ns=13,
            evaluation_context_id="f" * 64,
        )
        cells = generate_robustness_matrix(
            {"lookback_bars": 10, "entry_threshold": 0.02},
            windows,
            policy,
        )
        from nautilus_quant.strategy_robustness import RobustnessCellResult

        results = tuple(
            RobustnessCellResult(
                cell.cell_id,
                cell.evaluation_context_id,
                "PASS",
                "PASS",
                "a" * 64,
                Decimal("1"),
                Decimal("0"),
                "official",
                ("CELL_ECONOMIC_PASS",),
            )
            for cell in cells
        )
        identity = {
            "candidate_id": "1" * 64,
            "code_commit": "c" * 40,
            "data_as_of_ns": 13,
            "data_snapshot_id": "2" * 64,
            "data_source_id": "3" * 64,
            "engine_id": "4" * 64,
            "evaluation_context_id": robustness_evaluation_context_id(policy, cells),
            "experiment_id": "6" * 64,
            "hypothesis_id": "7" * 64,
            "policy_id": policy.policy_id,
            "runtime_id": "9" * 64,
            "strategy_id": "a" * 64,
        }
        with self.assertRaisesRegex(ValueError, "evaluation context"):
            build_robustness_verdict_v2(
                {**identity, "evaluation_context_id": "5" * 64},
                policy,
                cells,
                results,
                trial_context={
                    "campaign_id": "b" * 64,
                    "cohort_id": "c" * 64,
                    "trial_census_id": "d" * 64,
                    "generated_count": 1,
                    "executed_count": 1,
                    "rejected_count": 0,
                    "surviving_count": 1,
                    "technical_invalid_count": 0,
                    "terminal_census_complete": True,
                },
            )
        with self.assertRaisesRegex(ValueError, "data-as-of"):
            build_robustness_verdict_v2(
                {**identity, "data_as_of_ns": 14},
                policy,
                cells,
                results,
                trial_context=_trial_context(),
            )
        with self.assertRaisesRegex(ValueError, "cell result"):
            build_robustness_verdict_v2(
                identity,
                policy,
                cells,
                (replace(results[0], evaluation_context_id="0" * 64), *results[1:]),
                trial_context=_trial_context(),
            )
        verdict = build_robustness_verdict_v2(
            identity, policy, cells, results,
            trial_context=_trial_context(),
        )
        self.assertEqual(verdict["action"], "ADVANCE")
        self.assertIn("DSR_PBO_NOT_MODELED", verdict["reason_codes"])
        feedback = build_feedback_v2(verdict)
        action = build_action_v1(verdict)
        self.assertEqual(load_feedback_v2(canonical_json(feedback))["action"], "ADVANCE")
        self.assertEqual(feedback["schema_version"], "strategy-feedback-v2")
        self.assertEqual(action["schema_version"], "strategy-action-v1")
        self.assertEqual(action["evaluation_context_id"], identity["evaluation_context_id"])
        loaded_action = load_action_v1(canonical_json(action))
        self.assertEqual(loaded_action["action"], "ADVANCE")
        first_cell = verdict["cells"][0]
        self.assertEqual(first_cell["stress_scenario"], cells[0].stress_scenario)
        self.assertEqual(first_cell["cost_policy_id"], cells[0].cost_policy.cost_policy_id)
        self.assertEqual(first_cell["parameters"], cells[0].parameters)
        self.assertEqual(first_cell["window"]["scheme"], cells[0].window.scheme)

    def test_formal_evaluator_binds_cell_context_and_parameter_override(self) -> None:
        policy = replace(
            load_robustness_policy(ROOT / "config/strategy_robustness_policy.json"),
            maximum_windows_per_scheme=1,
            expanding_minimum_train_bars=3,
            rolling_train_bars=4,
            test_bars=2,
            step_bars=2,
        )
        window = generate_robustness_windows(
            tuple(range(1, 14)),
            policy,
            evaluation_start_ns=1,
            evaluation_end_ns=13,
            data_as_of_ns=13,
            evaluation_context_id="f" * 64,
        )[0]
        cell = generate_robustness_matrix(
            {"lookback_bars": 10, "entry_threshold": 0.02},
            (window,),
            policy,
        )[4]
        captured: list[object] = []
        evaluator = FormalNautilusEvaluator(runner=lambda request: captured.append(request))
        request = CandidateBacktestRequest(
            candidate_path=Path("candidate.json"),
            catalog_path=Path("catalog"),
            funding_path=Path("funding"),
            policy_path=Path("policy.json"),
            hypothesis_id="a" * 64,
            strategy_id="b" * 64,
            experiment_id="c" * 64,
            code_commit="d" * 40,
            evaluation_start_utc="1970-01-01T00:00:00.000000001Z",
            evaluation_end_utc="1970-01-01T00:00:00.000000013Z",
            data_as_of_ns=13,
            evaluation_context_id="f" * 64,
        )

        evaluator(request, cell)

        evaluated = captured[0]
        self.assertEqual(evaluated.evaluation_context_id, cell.evaluation_context_id)
        self.assertEqual(evaluated.candidate_evaluation_context_id, "f" * 64)
        self.assertEqual(evaluated.strategy_parameters_override, cell.parameters)

    def test_trial_census_is_required_and_fix_technical_cannot_create_child(self) -> None:
        policy = replace(
            load_robustness_policy(ROOT / "config/strategy_robustness_policy.json"),
            maximum_windows_per_scheme=1,
            expanding_minimum_train_bars=3,
            rolling_train_bars=4,
            test_bars=2,
            step_bars=2,
        )
        windows = generate_robustness_windows(
            tuple(range(1, 14)), policy,
            evaluation_start_ns=1, evaluation_end_ns=13, data_as_of_ns=13,
            evaluation_context_id="f" * 64,
        )
        cells = generate_robustness_matrix(
            {"lookback_bars": 10, "entry_threshold": 0.02}, windows, policy,
        )
        identity = {
            "candidate_id": "1" * 64, "code_commit": "c" * 40, "data_as_of_ns": 13,
            "data_snapshot_id": "2" * 64, "data_source_id": "3" * 64,
            "engine_id": "4" * 64,
            "evaluation_context_id": robustness_evaluation_context_id(policy, cells),
            "experiment_id": "6" * 64, "hypothesis_id": "7" * 64,
            "policy_id": policy.policy_id, "runtime_id": "9" * 64, "strategy_id": "a" * 64,
        }
        technical = tuple(
            RobustnessCellResult(
                cell.cell_id, cell.evaluation_context_id,
                "ERROR" if index == 0 else "PASS",
                "NOT_MODELED" if index == 0 else "PASS",
                None if index == 0 else "a" * 64,
                None if index == 0 else Decimal("1"),
                None if index == 0 else Decimal("0"),
                None if index == 0 else "official",
                ("ENGINE_FAILED",) if index == 0 else ("CELL_ECONOMIC_PASS",),
            )
            for index, cell in enumerate(cells)
        )
        with self.assertRaisesRegex(ValueError, "trial census"):
            build_robustness_verdict_v2(identity, policy, cells, technical)
        verdict = build_robustness_verdict_v2(
            identity,
            policy,
            cells,
            technical,
            trial_context=_trial_context(
                generated_count=3,
                executed_count=3,
                rejected_count=1,
                surviving_count=1,
                technical_invalid_count=1,
                candidate_count=2,
            ),
        )
        self.assertEqual(verdict["action"], "FIX_TECHNICAL")
        with self.assertRaisesRegex(ValueError, "technical"):
            build_action_v1(verdict, child_strategy_id="e" * 64)

    def test_economic_rejection_requires_complete_deterministic_mutation_lineage(self) -> None:
        policy = replace(
            load_robustness_policy(ROOT / "config/strategy_robustness_policy.json"),
            maximum_windows_per_scheme=1,
            expanding_minimum_train_bars=3,
            rolling_train_bars=4,
            test_bars=2,
            step_bars=2,
        )
        windows = generate_robustness_windows(
            tuple(range(1, 14)), policy,
            evaluation_start_ns=1, evaluation_end_ns=13, data_as_of_ns=13,
            evaluation_context_id="f" * 64,
        )
        cells = generate_robustness_matrix(
            {"lookback_bars": 10, "entry_threshold": 0.02}, windows, policy,
        )
        results = tuple(
            RobustnessCellResult(
                cell.cell_id, cell.evaluation_context_id, "PASS",
                "FAIL" if index == 0 else "PASS", "a" * 64,
                Decimal("-1") if index == 0 else Decimal("1"),
                Decimal("0"), "official",
                ("MINIMUM_NET_ACCOUNT_DELTA_NOT_MET",)
                if index == 0 else ("CELL_ECONOMIC_PASS",),
            )
            for index, cell in enumerate(cells)
        )
        identity = {
            "candidate_id": "1" * 64, "code_commit": "c" * 40, "data_as_of_ns": 13,
            "data_snapshot_id": "2" * 64, "data_source_id": "3" * 64,
            "engine_id": "4" * 64,
            "evaluation_context_id": robustness_evaluation_context_id(policy, cells),
            "experiment_id": "6" * 64, "hypothesis_id": "7" * 64,
            "policy_id": policy.policy_id, "runtime_id": "9" * 64, "strategy_id": "a" * 64,
        }
        verdict = build_robustness_verdict_v2(
            identity, policy, cells, results,
            trial_context=_trial_context(),
        )
        self.assertEqual(verdict["action"], "MUTATE")
        with self.assertRaisesRegex(ValueError, "mutation lineage"):
            build_action_v1(verdict)
        inputs = {
            "campaign_id": "b" * 64,
            "changed_dimension": "entry_threshold",
            "generation": 1,
            "child_hypothesis_id": "c" * 64,
            "child_strategy_id": "d" * 64,
        }
        first = build_action_v1(verdict, **inputs)
        second = build_action_v1(verdict, **inputs)
        self.assertEqual(first, second)
        self.assertEqual(first["child_hypothesis_id"], inputs["child_hypothesis_id"])
        self.assertEqual(first["child_strategy_id"], inputs["child_strategy_id"])
        self.assertEqual(load_action_v1(canonical_json(first)), first)

    def test_windows_use_closed_training_bars_and_verdict_exposes_cell_identity(self) -> None:
        frozen = load_robustness_policy(ROOT / "config/strategy_robustness_policy.json")
        policy = replace(
            frozen,
            maximum_windows_per_scheme=1,
            expanding_minimum_train_bars=3,
            rolling_train_bars=4,
            test_bars=2,
            step_bars=2,
        )
        bars = tuple(
            SimpleNamespace(ts_event_ns=index, close=100 + index)
            for index in range(1, 14)
        )
        windows = generate_robustness_windows(
            tuple(range(1, 14)), policy,
            evaluation_start_ns=1,
            evaluation_end_ns=13,
            data_as_of_ns=13,
            evaluation_context_id="f" * 64,
            closed_bars=bars,
        )
        self.assertEqual(windows[0].regime_label, "TREND")
        cells = generate_robustness_matrix(
            {"lookback_bars": 10, "entry_threshold": 0.02}, windows, policy,
        )
        results = tuple(
            RobustnessCellResult(
                cell.cell_id, cell.evaluation_context_id, "PASS", "PASS", "a" * 64,
                Decimal("1"), Decimal("0"), "official", ("CELL_ECONOMIC_PASS",),
            )
            for cell in cells
        )
        identity = {
            "candidate_id": "1" * 64, "code_commit": "c" * 40, "data_as_of_ns": 13,
            "data_snapshot_id": "2" * 64, "data_source_id": "3" * 64,
            "engine_id": "4" * 64,
            "evaluation_context_id": robustness_evaluation_context_id(policy, cells),
            "experiment_id": "6" * 64, "hypothesis_id": "7" * 64,
            "policy_id": policy.policy_id, "runtime_id": "9" * 64, "strategy_id": "a" * 64,
        }
        verdict = build_robustness_verdict_v2(
            identity, policy, cells, results,
            trial_context=_trial_context(),
        )
        cell = verdict["cells"][0]
        self.assertEqual(cell["stress_scenario"], cells[0].stress_scenario)
        self.assertEqual(cell["regime_label"], "TREND")
        self.assertEqual(cell["window"]["evaluation_context_id"], cells[0].window.evaluation_context_id)
        self.assertEqual(cell["cost_policy"]["cost_policy_id"], cells[0].cost_policy.cost_policy_id)
        self.assertEqual(cell["parameter_relative_offset"], "0")
        self.assertEqual(cell["parameters"], cells[0].parameters)

    def test_technical_and_economic_cell_outcomes_are_orthogonal(self) -> None:
        frozen = load_robustness_policy(ROOT / "config/strategy_robustness_policy.json")
        policy = replace(
            frozen, maximum_windows_per_scheme=1,
            expanding_minimum_train_bars=3, rolling_train_bars=4,
            test_bars=2, step_bars=2,
        )
        windows = generate_robustness_windows(
            tuple(range(1, 14)), policy,
            evaluation_start_ns=1, evaluation_end_ns=13, data_as_of_ns=13,
            evaluation_context_id="f" * 64,
        )
        calls = 0

        def evaluator(_request: object, cell: object) -> dict[str, object]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("engine unavailable")
            return {
                "execution": {"slippage_status": "modeled"},
                "funding": {"truth_status": "official"},
                "net_account_delta": "0.0",
                "realized_balance_drawdown": "0.0",
                "status": "EVALUATED",
            }

        _cells, results = evaluate_robustness_matrix(
            SimpleNamespace(parameters={"lookback_bars": 10, "entry_threshold": 0.02}),
            windows,
            policy,
            evaluator=evaluator,
        )
        self.assertEqual(len(results), 14)
        self.assertEqual(results[0].technical_status, "ERROR")
        self.assertEqual(results[1].technical_status, "PASS")
        self.assertEqual(results[1].economic_status, "FAIL")
        self.assertIn("MINIMUM_NET_ACCOUNT_DELTA_NOT_MET", results[1].reason_codes)

    def test_ledger_resolves_one_source_bound_persisted_survivor(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = strategy_lab.StrategyLedger(root / "ledger.sqlite3")
            ledger.initialize()
            hypothesis_payload = canonical_json({"fixture": "hypothesis"})
            hypothesis_path = root / "hypothesis.json"
            hypothesis_path.write_bytes(hypothesis_payload)
            hypothesis_id = hashlib.sha256(hypothesis_payload).hexdigest()
            candidate_payload = canonical_json({"fixture": "candidate"})
            run_directory = root / "run"
            run_directory.mkdir()
            candidate_path = run_directory / "candidate.json"
            candidate_path.write_bytes(candidate_payload)
            candidate_id = hashlib.sha256(candidate_payload).hexdigest()
            pybroker_path = run_directory / "pybroker-result.json"
            pybroker_payload = canonical_json({"fixture": "pybroker"})
            pybroker_path.write_bytes(pybroker_payload)
            parity_path = run_directory / "signal-parity.json"
            parity_payload = canonical_json({"fixture": "parity"})
            parity_path.write_bytes(parity_payload)
            historical_path = run_directory / "nautilus-verdict.json"
            historical_payload = canonical_json({"fixture": "historical"})
            historical_path.write_bytes(historical_payload)
            strategy_id = "1" * 64
            data_source_id = "2" * 64
            data_snapshot_id = "3" * 64
            historical_policy_id = "4" * 64
            runtime_id = "5" * 64
            evaluation_context_id = "6" * 64
            data_as_of_ns = 13
            base_identity = strategy_lab.ExperimentIdentity(
                strategy_id,
                data_source_id,
                historical_policy_id,
                "historical-engine",
                runtime_id,
                data_as_of_ns,
                data_snapshot_id,
            )
            historical_experiment_id = strategy_lab.experiment_id(base_identity)
            campaign_document = {
                "approved_bar_types": ["BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL"],
                "approved_instruments": ["BTCUSDT-PERP.BINANCE"],
                "data_as_of_ns": data_as_of_ns,
                "family_id": "lookback-momentum-long-flat",
                "family_version": "lookback-momentum-long-flat-v1",
                "generation_budget": 1,
                "maximum_candidates": 1,
                "parameter_search_policy_id": "7" * 64,
                "schema_version": "strategy-campaign-v1",
                "screen_policy_id": "8" * 64,
                "search_space": {"entry_threshold": [0.02], "lookback_bars": [10]},
                "seed": 42,
            }
            campaign_payload = canonical_json(campaign_document)
            campaign_id = hashlib.sha256(campaign_payload).hexdigest()
            verdict_record = strategy_lab.VerdictRecord(
                historical_experiment_id,
                "SUCCESS",
                "RETAIN_FOR_RESEARCH",
                str(historical_path),
                hashlib.sha256(historical_payload).hexdigest(),
            )
            historical_document = {
                "candidate_id": candidate_id,
                "code_commit": "c" * 40,
                "experiment_id": historical_experiment_id,
                "hypothesis_id": hypothesis_id,
                "source": {
                    "first_ts_event_ns": 1,
                    "last_ts_event_ns": data_as_of_ns,
                    "row_count": 13,
                    "sha256": data_snapshot_id,
                },
                "strategy_id": strategy_id,
            }
            candidate_document = {
                "evaluation_context_id": evaluation_context_id,
                "source": {
                    "data_as_of_ns": data_as_of_ns,
                    "data_snapshot_id": data_snapshot_id,
                },
            }
            with closing(sqlite3.connect(ledger.path)) as connection, connection:
                connection.execute(
                    "INSERT INTO strategies VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (strategy_id, "family", "family-v1", "{}", "instrument", "bar", "strategy-id-v2"),
                )
                connection.execute(
                    "INSERT INTO hypotheses VALUES (?, ?, NULL, NULL, ?, ?)",
                    (hypothesis_id, strategy_id, str(hypothesis_path), hypothesis_id),
                )
                connection.execute(
                    "INSERT INTO experiments VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        historical_experiment_id,
                        hypothesis_id,
                        strategy_id,
                        data_source_id,
                        historical_policy_id,
                        "historical-engine",
                        runtime_id,
                    ),
                )
                connection.execute(
                    "INSERT INTO experiment_sources VALUES (?, ?, ?)",
                    (historical_experiment_id, data_snapshot_id, data_as_of_ns),
                )
                connection.execute(
                    "INSERT INTO campaigns VALUES (?, ?, ?, ?, ?)",
                    (campaign_id, campaign_payload.decode(), "8" * 64, data_as_of_ns, data_source_id),
                )
                connection.execute(
                    "INSERT INTO campaign_trials VALUES (?, 0, ?, ?, 'SURVIVED', 1, ?, ?)",
                    (
                        campaign_id,
                        strategy_id,
                        candidate_id,
                        historical_experiment_id,
                        canonical_json(["SCREEN_PASSED"]).decode(),
                    ),
                )
                connection.execute(
                    "INSERT INTO stage_results VALUES (?, ?, 'PyBroker completed', 'PASSED', ?, ?, ?)",
                    (
                        historical_experiment_id,
                        strategy_id,
                        "PYBROKER_PROCESS_COMPLETED",
                        str(pybroker_path),
                        hashlib.sha256(pybroker_payload).hexdigest(),
                    ),
                )
                connection.execute(
                    "INSERT INTO signal_parity_results VALUES (?, ?, ?, ?, ?, 'PASS', ?, NULL, ?, ?)",
                    (
                        "9" * 64,
                        historical_experiment_id,
                        candidate_id,
                        evaluation_context_id,
                        data_snapshot_id,
                        "SIGNAL_PARITY_PASS",
                        str(parity_path),
                        hashlib.sha256(parity_payload).hexdigest(),
                    ),
                )
                connection.execute(
                    "INSERT INTO verdicts VALUES (?, ?, ?, 'SUCCESS', ?, ?, ?)",
                    (
                        strategy_lab._verdict_record_id(verdict_record),
                        historical_experiment_id,
                        strategy_id,
                        "RETAIN_FOR_RESEARCH",
                        str(historical_path),
                        hashlib.sha256(historical_payload).hexdigest(),
                    ),
                )

            with (
                patch(
                    "nautilus_quant.strategy_lab.load_pybroker_candidate",
                    return_value=(candidate_document, candidate_id),
                ),
                patch(
                    "nautilus_quant.strategy_lab.load_candidate_backtest_verdict",
                    return_value=historical_document,
                ),
            ):
                survivor = ledger.robustness_survivor_context(campaign_id)

            self.assertEqual(survivor.base_identity, base_identity)
            self.assertEqual(survivor.candidate_path, candidate_path)
            self.assertEqual(survivor.candidate_id, candidate_id)
            self.assertEqual(survivor.candidate_evaluation_context_id, evaluation_context_id)
            self.assertEqual(survivor.code_commit, "c" * 40)

    def test_robustness_evidence_is_append_only_and_funnel_is_ledger_derived(self) -> None:
        frozen = load_robustness_policy(ROOT / "config/strategy_robustness_policy.json")
        policy = replace(
            frozen,
            maximum_windows_per_scheme=1,
            expanding_minimum_train_bars=3,
            rolling_train_bars=4,
            test_bars=2,
            step_bars=2,
        )
        windows = generate_robustness_windows(
            tuple(range(1, 14)), policy,
            evaluation_start_ns=1, evaluation_end_ns=13, data_as_of_ns=13,
            evaluation_context_id="f" * 64,
        )
        cells = generate_robustness_matrix(
            {"lookback_bars": 10, "entry_threshold": 0.02}, windows, policy,
        )
        results = tuple(
            RobustnessCellResult(
                cell.cell_id, cell.evaluation_context_id, "PASS", "PASS", "a" * 64,
                Decimal("1"), Decimal("0"), "official", ("DSR_PBO_NOT_MODELED",),
            )
            for cell in cells
        )
        identity = {
            "candidate_id": "1" * 64, "code_commit": "c" * 40, "data_as_of_ns": 13,
            "data_snapshot_id": "2" * 64, "data_source_id": "3" * 64,
            "engine_id": "4" * 64,
            "evaluation_context_id": robustness_evaluation_context_id(policy, cells),
            "experiment_id": "6" * 64, "hypothesis_id": "7" * 64,
            "policy_id": policy.policy_id, "runtime_id": "9" * 64, "strategy_id": "a" * 64,
        }
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = strategy_lab.StrategyLedger(root / "ledger.sqlite3")
            ledger.initialize()
            campaign_document = {
                "approved_bar_types": ["BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL"],
                "approved_instruments": ["BTCUSDT-PERP.BINANCE"],
                "data_as_of_ns": 13,
                "family_id": "lookback-momentum-long-flat",
                "family_version": "lookback-momentum-long-flat-v1",
                "generation_budget": 1,
                "maximum_candidates": 1,
                "parameter_search_policy_id": "e" * 64,
                "schema_version": "strategy-campaign-v1",
                "screen_policy_id": "f" * 64,
                "search_space": {"entry_threshold": [0.02], "lookback_bars": [10]},
                "seed": 42,
            }
            campaign_payload = canonical_json(campaign_document)
            campaign_id = hashlib.sha256(campaign_payload).hexdigest()
            with closing(sqlite3.connect(ledger.path)) as connection, connection:
                connection.execute(
                    "INSERT INTO strategies VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (identity["strategy_id"], "family", "family-v1", "{}", "instrument", "bar", "strategy-id-v2"),
                )
                connection.execute(
                    "INSERT INTO hypotheses VALUES (?, ?, NULL, NULL, ?, ?)",
                    (identity["hypothesis_id"], identity["strategy_id"], str(root / "hypothesis.json"), identity["hypothesis_id"]),
                )
                connection.execute(
                    "INSERT INTO experiments VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (identity["experiment_id"], identity["hypothesis_id"], identity["strategy_id"],
                     identity["data_source_id"], identity["policy_id"], identity["engine_id"], identity["runtime_id"]),
                )
                connection.execute(
                    "INSERT INTO campaigns VALUES (?, ?, ?, ?, ?)",
                    (campaign_id, campaign_payload.decode(), "f" * 64, 13, identity["data_source_id"]),
                )
                connection.execute(
                    "INSERT INTO campaign_trials VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        campaign_id,
                        0,
                        identity["strategy_id"],
                        identity["candidate_id"],
                        "SURVIVED",
                        1,
                        identity["experiment_id"],
                        canonical_json(["SCREEN_PASSED"]).decode(),
                    ),
                )
            trial_context = ledger.robustness_trial_context(campaign_id)
            self.assertEqual(trial_context["generated_count"], 1)
            self.assertEqual(trial_context["search_space"], campaign_document["search_space"])
            forged_context = {**trial_context, "generation_budget": 2}
            forged_verdict = build_robustness_verdict_v2(
                identity, policy, cells, results,
                trial_context=forged_context,
            )
            with self.assertRaisesRegex(ValueError, "campaign trial census"):
                ledger.publish_robustness(
                    root / "forged-robustness",
                    forged_verdict,
                    build_feedback_v2(forged_verdict),
                    build_action_v1(forged_verdict, campaign_id=campaign_id),
                )
            foreign_candidate_verdict = build_robustness_verdict_v2(
                {**identity, "candidate_id": "f" * 64},
                policy,
                cells,
                results,
                trial_context=trial_context,
            )
            with self.assertRaisesRegex(ValueError, "survivor candidate"):
                ledger.publish_robustness(
                    root / "foreign-candidate-robustness",
                    foreign_candidate_verdict,
                    build_feedback_v2(foreign_candidate_verdict),
                    build_action_v1(foreign_candidate_verdict, campaign_id=campaign_id),
                )
            verdict = build_robustness_verdict_v2(
                identity, policy, cells, results,
                trial_context=trial_context,
            )
            feedback = build_feedback_v2(verdict)
            action = build_action_v1(verdict, campaign_id=campaign_id)
            relative_artifacts = Path(os.path.relpath(root / "robustness", Path.cwd()))
            record = ledger.publish_robustness(relative_artifacts, verdict, feedback, action)
            self.assertTrue(Path(record.verdict_path).is_absolute())
            self.assertTrue(Path(record.feedback_path).is_absolute())
            self.assertTrue(Path(record.action_path).is_absolute())
            self.assertEqual(ledger.robustness_funnel()["robustness_evaluated"], 1)
            self.assertEqual(
                ledger.publish_robustness(relative_artifacts, verdict, feedback, action),
                record,
            )
            self.assertEqual(ledger.robustness_funnel()["robustness_evaluated"], 1)
            reused = ledger.existing_robustness(
                identity["experiment_id"],
                identity["evaluation_context_id"],
                identity["policy_id"],
            )
            self.assertEqual(reused, record)
            with self.assertRaises(sqlite3.IntegrityError):
                with closing(sqlite3.connect(ledger.path)) as connection, connection:
                    connection.execute("UPDATE robustness_results SET outcome = 'FAIL'")


if __name__ == "__main__":
    unittest.main()
