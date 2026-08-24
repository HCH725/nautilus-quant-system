# noqa: E501  # noqa: SIZE_OK — Task A keeps all acceptance tests in one focused module.
from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import tomllib
import unittest
from unittest.mock import patch

from nautilus_trader.persistence import ParquetDataCatalog

import nautilus_quant.strategy_lab as strategy_lab
from nautilus_quant.funding_observation import migrate_funding_observations
from nautilus_quant.nautilus_io import make_bar
from nautilus_quant.strategy_families import (
    ClosedBar,
    FamilyDecision,
    FamilyDefinition,
    FamilyEvaluation,
    FamilyRegistry,
    KERNEL_HASH,
    KERNEL_VERSION,
    canonical_decision_bytes,
    derive_signal_id,
    evaluate_batch,
)
from nautilus_quant.strategy_lab import load_strategy_hypothesis
from tests.test_candidate_backtest import (
    BAR_TYPE,
    HOUR_MS,
    HOUR_NS,
    INSTRUMENT_ID,
    _FundingClient,
    _catalog_digest,
    _instrument,
)

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]


def canonical_bytes(value: JsonValue) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()


def valid_hypothesis() -> dict[str, JsonValue]:
    return {
        "schema_version": "strategy-hypothesis-v1",
        "parent_strategy_id": None,
        "based_on_verdict_id": None,
        "thesis": "Positive 24-hour momentum persists into the next event",
        "falsification": (
            "No fills, non-positive net result after fees and Funding, "
            "or unstable official-window result"
        ),
        "strategy_family": "lookback-momentum-long-flat",
        "parameters": {"lookback_bars": 24, "entry_threshold": 0.0},
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "bar_type": "BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",
    }


def valid_hypothesis_v2() -> dict[str, JsonValue]:
    return {
        "bar_type": "BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",
        "based_on_verdict_id": None,
        "falsification": "Momentum does not survive formal accounting",
        "family_version": "lookback-momentum-long-flat-v1",
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "parameters": {"entry_threshold": 0.05, "lookback_bars": 2},
        "parent_strategy_id": None,
        "schema_version": "strategy-hypothesis-v2",
        "strategy_family": "lookback-momentum-long-flat",
        "thesis": "Positive momentum persists into the next event",
    }


def create_legacy_v1_ledger(path: Path, *, valid: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE strategies (
                strategy_id TEXT PRIMARY KEY,
                family TEXT NOT NULL,
                lookback_bars INTEGER NOT NULL,
                entry_threshold REAL NOT NULL,
                instrument_id TEXT NOT NULL,
                bar_type TEXT NOT NULL
            );
            CREATE TABLE hypotheses (
                hypothesis_id TEXT PRIMARY KEY,
                strategy_id TEXT NOT NULL,
                parent_strategy_id TEXT,
                based_on_verdict_id TEXT,
                artifact_path TEXT NOT NULL,
                artifact_sha256 TEXT NOT NULL CHECK (artifact_sha256 = hypothesis_id),
                UNIQUE (hypothesis_id, strategy_id),
                CHECK ((parent_strategy_id IS NULL) = (based_on_verdict_id IS NULL)),
                FOREIGN KEY (strategy_id) REFERENCES strategies(strategy_id),
                FOREIGN KEY (parent_strategy_id, based_on_verdict_id)
                    REFERENCES verdicts(strategy_id, verdict_id)
            );
            CREATE TABLE experiments (
                experiment_id TEXT PRIMARY KEY,
                hypothesis_id TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                data_source_id TEXT NOT NULL,
                policy_id TEXT NOT NULL,
                engine_id TEXT NOT NULL,
                runtime_id TEXT NOT NULL,
                UNIQUE (experiment_id, strategy_id),
                UNIQUE (strategy_id, data_source_id, policy_id, engine_id, runtime_id),
                FOREIGN KEY (hypothesis_id, strategy_id)
                    REFERENCES hypotheses(hypothesis_id, strategy_id)
            );
            CREATE TABLE verdicts (
                verdict_id TEXT PRIMARY KEY,
                experiment_id TEXT NOT NULL UNIQUE,
                strategy_id TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK (outcome IN ('SUCCESS', 'REJECTION')),
                reason_code TEXT NOT NULL,
                artifact_path TEXT NOT NULL,
                artifact_sha256 TEXT NOT NULL,
                UNIQUE (strategy_id, verdict_id),
                FOREIGN KEY (experiment_id, strategy_id)
                    REFERENCES experiments(experiment_id, strategy_id)
            );
            CREATE TABLE errors (
                error_id TEXT PRIMARY KEY,
                experiment_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                artifact_path TEXT NOT NULL,
                artifact_sha256 TEXT NOT NULL,
                FOREIGN KEY (experiment_id) REFERENCES experiments(experiment_id)
            );
            CREATE TABLE stage_results (
                experiment_id TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                stage TEXT NOT NULL CHECK (stage IN (
                    'PyBroker completed', 'Research screened', 'Nautilus replayed'
                )),
                outcome TEXT NOT NULL CHECK (outcome IN ('PASSED', 'REJECTED', 'ERROR')),
                reason_code TEXT NOT NULL,
                artifact_path TEXT NOT NULL,
                artifact_sha256 TEXT NOT NULL,
                PRIMARY KEY (experiment_id, stage),
                FOREIGN KEY (experiment_id, strategy_id)
                    REFERENCES experiments(experiment_id, strategy_id)
            );
            """
        )
        strategy_id = "1" * 64
        hypothesis_id = "2" * 64
        experiment_id = "3" * 64
        verdict_id = "4" * 64
        connection.execute(
            "INSERT INTO strategies VALUES (?, ?, ?, ?, ?, ?)",
            (
                strategy_id,
                "lookback-momentum-long-flat" if valid else "unknown-family",
                24,
                0.0,
                "BTCUSDT-PERP.BINANCE",
                "BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",
            ),
        )
        connection.execute(
            "INSERT INTO hypotheses VALUES (?, ?, NULL, NULL, ?, ?)",
            (hypothesis_id, strategy_id, "/legacy/hypothesis.json", hypothesis_id),
        )
        connection.execute(
            "INSERT INTO experiments VALUES (?, ?, ?, ?, ?, ?, ?)",
            (experiment_id, hypothesis_id, strategy_id, "data", "policy", "engine", "runtime"),
        )
        connection.execute(
            "INSERT INTO verdicts VALUES (?, ?, ?, 'SUCCESS', ?, ?, ?)",
            (verdict_id, experiment_id, strategy_id, "RETAINED", "/legacy/verdict.json", "5" * 64),
        )
        connection.execute(
            "INSERT INTO errors VALUES (?, ?, ?, ?, ?, ?)",
            ("6" * 64, experiment_id, "PYBROKER", "OLD_ERROR", "/legacy/error.json", "7" * 64),
        )
        connection.execute(
            "INSERT INTO stage_results VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                experiment_id,
                strategy_id,
                "Nautilus replayed",
                "PASSED",
                "OLD_STAGE",
                "/legacy/stage.json",
                "8" * 64,
            ),
        )
        for table in (
            "strategies",
            "hypotheses",
            "experiments",
            "verdicts",
            "errors",
            "stage_results",
        ):
            for action in ("UPDATE", "DELETE"):
                connection.execute(
                    f"""CREATE TRIGGER {table}_immutable_{action.lower()}
                    BEFORE {action} ON {table}
                    BEGIN SELECT RAISE(ABORT, '{table} records are immutable'); END"""
                )


class StrategyHypothesisTests(unittest.TestCase):
    def _load(self, payload: bytes):
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        path = Path(temporary_directory.name) / "hypothesis.json"
        path.write_bytes(payload)
        return load_strategy_hypothesis(path)

    def test_accepts_canonical_root_hypothesis(self):
        hypothesis = valid_hypothesis()
        payload = canonical_bytes(hypothesis)

        loaded = self._load(payload)

        self.assertEqual(loaded.hypothesis_id, sha256(payload).hexdigest())
        self.assertEqual(loaded.parameters.lookback_bars, 24)
        self.assertEqual(loaded.identity_schema, "strategy-id-v1")
        self.assertIsNone(loaded.parent_strategy_id)

    def test_accepts_v2_and_binds_family_version_into_strategy_identity(self):
        first_document = valid_hypothesis_v2()
        first = self._load(canonical_bytes(first_document))
        second_document = valid_hypothesis_v2()
        parameters = second_document["parameters"]
        self.assertIsInstance(parameters, dict)
        parameters["entry_threshold"] = 0.06
        second = self._load(canonical_bytes(second_document))

        self.assertEqual(first.identity_schema, "strategy-id-v2")
        self.assertEqual(first.family_id, "lookback-momentum-long-flat")
        self.assertEqual(first.family_version, "lookback-momentum-long-flat-v1")
        self.assertEqual(first.parameters.values, {"entry_threshold": 0.05, "lookback_bars": 2})
        self.assertNotEqual(first.strategy_id, second.strategy_id)

    def test_v2_family_acceptance_is_owned_by_the_tracked_registry(self):
        document = valid_hypothesis_v2()
        document["strategy_family"] = "minimum-close-long-flat"
        document["family_version"] = "minimum-close-long-flat-v1"
        document["parameters"] = {"minimum_close": 100.0}
        registry = FamilyRegistry(
            (
                FamilyDefinition(
                    family_id="minimum-close-long-flat",
                    family_version="minimum-close-long-flat-v1",
                    warmup_bars=lambda _parameters: 1,
                    validate_parameters=lambda parameters: {
                        "minimum_close": float(parameters["minimum_close"])
                    },
                    evaluate=lambda bars, parameters: FamilyEvaluation(
                        score=bars[-1].close,
                        target_intent=(
                            "LONG"
                            if bars[-1].close > parameters["minimum_close"]
                            else "FLAT"
                        ),
                        reason="CLOSE_COMPARED_WITH_MINIMUM",
                    ),
                ),
            ),
        )

        with patch.object(strategy_lab, "DEFAULT_REGISTRY", registry):
            loaded = self._load(canonical_bytes(document))

        self.assertEqual(loaded.family_id, "minimum-close-long-flat")
        self.assertEqual(loaded.parameters.values, {"minimum_close": 100.0})

    def test_v2_rejects_untracked_family_version_and_forbidden_parameters(self):
        wrong_version = valid_hypothesis_v2()
        wrong_version["family_version"] = "lookback-momentum-long-flat-v999"
        with self.assertRaisesRegex(ValueError, "family_version"):
            self._load(canonical_bytes(wrong_version))

        forbidden = valid_hypothesis_v2()
        parameters = forbidden["parameters"]
        self.assertIsInstance(parameters, dict)
        parameters["leverage"] = 2
        with self.assertRaisesRegex(ValueError, "forbidden hypothesis field"):
            self._load(canonical_bytes(forbidden))

    def test_rejects_unknown_strategy_family(self):
        hypothesis = valid_hypothesis()
        hypothesis["strategy_family"] = "generated-python"

        with self.assertRaisesRegex(ValueError, "strategy family"):
            self._load(canonical_bytes(hypothesis))

    def test_rejects_boolean_zero_negative_or_excessive_lookback(self):
        for lookback in (True, 0, -1, 8761):
            with self.subTest(lookback=lookback):
                hypothesis = valid_hypothesis()
                parameters = hypothesis["parameters"]
                self.assertIsInstance(parameters, dict)
                parameters["lookback_bars"] = lookback

                with self.assertRaisesRegex(ValueError, "lookback_bars"):
                    self._load(canonical_bytes(hypothesis))

    def test_rejects_nan_and_infinity(self):
        for threshold in (math.nan, math.inf, -math.inf):
            with self.subTest(threshold=threshold):
                hypothesis = valid_hypothesis()
                parameters = hypothesis["parameters"]
                self.assertIsInstance(parameters, dict)
                parameters["entry_threshold"] = threshold
                payload = (
                    json.dumps(
                        hypothesis,
                        allow_nan=True,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    + "\n"
                ).encode()

                with self.assertRaisesRegex(ValueError, "finite JSON"):
                    self._load(payload)

    def test_rejects_negative_or_boolean_entry_threshold(self):
        for threshold in (-0.01, True):
            with self.subTest(threshold=threshold):
                hypothesis = valid_hypothesis()
                parameters = hypothesis["parameters"]
                self.assertIsInstance(parameters, dict)
                parameters["entry_threshold"] = threshold

                with self.assertRaisesRegex(ValueError, "entry_threshold"):
                    self._load(canonical_bytes(hypothesis))

    def test_rejects_forbidden_nested_fields(self):
        hypothesis = valid_hypothesis()
        parameters = hypothesis["parameters"]
        self.assertIsInstance(parameters, dict)
        parameters["risk"] = {"leverage": 2}

        with self.assertRaisesRegex(ValueError, "forbidden hypothesis field"):
            self._load(canonical_bytes(hypothesis))

    def test_rejects_malformed_lineage(self):
        hypothesis = valid_hypothesis()
        hypothesis["parent_strategy_id"] = "a" * 64

        with self.assertRaisesRegex(ValueError, "lineage"):
            self._load(canonical_bytes(hypothesis))

    def test_rejects_duplicate_json_keys(self):
        payload = canonical_bytes(valid_hypothesis()).replace(
            b'"schema_version":"strategy-hypothesis-v1"',
            b'"schema_version":"strategy-hypothesis-v1",'
            b'"schema_version":"strategy-hypothesis-v1"',
        )

        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            self._load(payload)

    def test_rejects_noncanonical_json(self):
        payload = json.dumps(valid_hypothesis(), indent=2).encode()

        with self.assertRaisesRegex(ValueError, "canonical JSON"):
            self._load(payload)

    def test_same_semantic_hypothesis_has_identical_ids(self):
        payload = canonical_bytes(valid_hypothesis())

        first = self._load(payload)
        second = self._load(payload)

        self.assertEqual(first.hypothesis_id, second.hypothesis_id)
        self.assertEqual(first.strategy_id, second.strategy_id)

    def test_negative_zero_threshold_is_not_a_second_strategy_identity(self):
        hypothesis = valid_hypothesis()
        parameters = hypothesis["parameters"]
        self.assertIsInstance(parameters, dict)
        parameters["entry_threshold"] = -0.0

        with self.assertRaisesRegex(ValueError, "canonical JSON"):
            self._load(canonical_bytes(hypothesis))


class StrategyLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.ledger = strategy_lab.StrategyLedger(self.root / "ledger.sqlite3")
        self._hypothesis_number = 0

    def _hypothesis(
        self,
        lookback_bars: int,
        *,
        parent_strategy_id: str | None = None,
        based_on_verdict_id: str | None = None,
    ):
        hypothesis = valid_hypothesis()
        parameters = hypothesis["parameters"]
        self.assertIsInstance(parameters, dict)
        parameters["lookback_bars"] = lookback_bars
        hypothesis["parent_strategy_id"] = parent_strategy_id
        hypothesis["based_on_verdict_id"] = based_on_verdict_id
        self._hypothesis_number += 1
        path = self.root / f"hypothesis-{self._hypothesis_number}.json"
        path.write_bytes(canonical_bytes(hypothesis))
        return load_strategy_hypothesis(path)

    def _experiment(self, hypothesis, suffix: str):
        identity = strategy_lab.ExperimentIdentity(
            strategy_id=hypothesis.strategy_id,
            data_source_id=f"source-{suffix}",
            policy_id=f"policy-{suffix}",
            engine_id=f"nautilus-{suffix}",
            runtime_id=f"python-{suffix}",
        )
        return self.ledger.record_experiment(hypothesis.hypothesis_id, identity)

    def _artifact(self, name: str, fill: str) -> tuple[str, str]:
        path = self.root / name
        path.write_bytes(fill.encode() * 64)
        return str(path), sha256(path.read_bytes()).hexdigest()

    def test_schema_creation_is_idempotent(self):
        self.ledger.initialize()

        self.ledger.initialize()

        with closing(sqlite3.connect(self.ledger.path)) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'",
                )
            }
        self.assertTrue(
            {
                "strategies",
                "hypotheses",
                "experiments",
                "experiment_sources",
                "verdicts",
                "errors",
                "robustness_results",
                "robustness_lineage",
            }
            <= tables
        )

    def test_initialize_migrates_copied_v1_ledger_without_reidentifying_or_losing_rows(self):
        create_legacy_v1_ledger(self.ledger.path)
        preserved_tables = ("hypotheses", "experiments", "verdicts", "errors", "stage_results")
        with closing(sqlite3.connect(self.ledger.path)) as connection:
            before = {
                table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                for table in preserved_tables
            }
            stage_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'stage_results'"
            ).fetchone()[0]

        self.ledger.initialize()

        with closing(sqlite3.connect(self.ledger.path)) as connection:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(strategies)")]
            migrated = connection.execute("SELECT * FROM strategies").fetchone()
            after = {
                table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                for table in preserved_tables
            }
            migrated_stage_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'stage_results'"
            ).fetchone()[0]
            source_rows = connection.execute("SELECT * FROM experiment_sources").fetchall()
            robustness_rows = connection.execute("SELECT * FROM robustness_results").fetchall()
            lineage_rows = connection.execute("SELECT * FROM robustness_lineage").fetchall()
            quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
            foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
            triggers = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                )
            }

        self.assertEqual(
            columns,
            [
                "strategy_id",
                "family",
                "family_version",
                "parameters_json",
                "instrument_id",
                "bar_type",
                "identity_schema",
            ],
        )
        self.assertEqual(migrated[0], "1" * 64)
        self.assertEqual(migrated[1:4], (
            "lookback-momentum-long-flat",
            "lookback-momentum-long-flat-v1",
            canonical_bytes({"entry_threshold": 0.0, "lookback_bars": 24}).decode(),
        ))
        self.assertEqual(migrated[-1], "strategy-id-v1")
        self.assertEqual(after, before)
        self.assertEqual(migrated_stage_sql, stage_sql)
        self.assertEqual(source_rows, [])
        self.assertEqual(robustness_rows, [])
        self.assertEqual(lineage_rows, [])
        self.assertEqual(quick_check, "ok")
        self.assertEqual(foreign_key_errors, [])
        for table in (
            *preserved_tables,
            "strategies",
            "experiment_sources",
            "signal_parity_results",
            "robustness_results",
            "robustness_lineage",
        ):
            self.assertIn(f"{table}_immutable_update", triggers)
            self.assertIn(f"{table}_immutable_delete", triggers)

    def test_invalid_legacy_strategy_rolls_back_migration_instead_of_resetting(self):
        create_legacy_v1_ledger(self.ledger.path, valid=False)

        with self.assertRaisesRegex(ValueError, "legacy strategy family"):
            self.ledger.initialize()

        with closing(sqlite3.connect(self.ledger.path)) as connection:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(strategies)")]
            row = connection.execute("SELECT family FROM strategies").fetchone()
            source_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'experiment_sources'"
            ).fetchone()
            quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        self.assertIn("lookback_bars", columns)
        self.assertEqual(row, ("unknown-family",))
        self.assertIsNone(source_table)
        self.assertEqual(quick_check, "ok")

    def test_initialize_migrates_legacy_stage_outcomes_without_losing_rows(self):
        self.ledger.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.ledger.path)) as connection, connection:
            connection.execute(
                """CREATE TABLE stage_results (
                    experiment_id TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    outcome TEXT NOT NULL CHECK (outcome IN ('PASSED', 'REJECTED')),
                    reason_code TEXT NOT NULL,
                    artifact_path TEXT NOT NULL,
                    artifact_sha256 TEXT NOT NULL,
                    PRIMARY KEY (experiment_id, stage)
                )""",
            )
            connection.execute(
                "INSERT INTO stage_results VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("e", "s", "PyBroker completed", "REJECTED", "OLD", "a", "b"),
            )

        self.ledger.initialize()

        with closing(sqlite3.connect(self.ledger.path)) as connection, connection:
            self.assertEqual(
                connection.execute("SELECT outcome FROM stage_results").fetchall(),
                [("REJECTED",)],
            )
            connection.execute(
                "INSERT INTO stage_results VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("e2", "s2", "PyBroker completed", "ERROR", "NEW", "a", "b"),
            )

    def test_strategy_version_is_deduplicated_by_content_id(self):
        self.ledger.initialize()
        hypothesis = self._hypothesis(24)

        self.ledger.record_hypothesis(hypothesis)
        self.ledger.record_hypothesis(hypothesis)

        with closing(sqlite3.connect(self.ledger.path)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM strategies").fetchone()[0]
        self.assertEqual(count, 1)

    def test_signal_parity_result_is_hashed_append_only_evidence(self):
        self.ledger.initialize()
        hypothesis = self._hypothesis(24)
        self.ledger.record_hypothesis(hypothesis)
        experiment_id = self._experiment(hypothesis, "parity")
        candidate_id = "a" * 64
        decisions = (
            FamilyDecision(
                "1" * 64, 1, "1", "LONG", "test",
                "lookback-momentum-long-flat", "lookback-momentum-long-flat-v1",
                "strategy-family-kernel-v1", "2" * 64,
            ),
            FamilyDecision(
                "3" * 64, 2, "-1", "FLAT", "test-close",
                "lookback-momentum-long-flat", "lookback-momentum-long-flat-v1",
                "strategy-family-kernel-v1", "2" * 64,
            ),
        )
        decision_payload = b"".join(canonical_decision_bytes(item) for item in decisions)
        artifact = {
            "candidate_id": candidate_id,
            "candidate_signal_count": 2,
            "detail": None,
            "mismatch_index": None,
            "outcome": "PASS",
            "reason_code": "SIGNAL_PARITY_MATCH",
            "recomputed_signal_count": 2,
            "recomputed_signals_sha256": sha256(decision_payload).hexdigest(),
            "required_action": None,
            "schema_version": "signal-parity-result-v1",
        }
        path = self.root / "signal-parity-result.json"
        path.write_bytes(canonical_bytes(artifact))
        record = strategy_lab.SignalParityRecord(
            experiment_id=experiment_id,
            candidate_id=candidate_id,
            evaluation_context_id="c" * 64,
            data_snapshot_id="d" * 64,
            outcome="PASS",
            reason_code="SIGNAL_PARITY_MATCH",
            required_action=None,
            artifact_path=str(path),
            artifact_sha256=sha256(path.read_bytes()).hexdigest(),
            decisions=decisions,
        )

        first_id = self.ledger.record_signal_parity(record)
        second_id = self.ledger.record_signal_parity(record)

        self.assertEqual(first_id, second_id)
        with closing(sqlite3.connect(self.ledger.path)) as connection:
            stored = connection.execute(
                "SELECT * FROM signal_parity_results",
            ).fetchone()
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE signal_parity_results SET reason_code = 'TAMPERED'",
                )
        self.assertEqual(stored[0], first_id)
        self.assertEqual(stored[1:8], (
            experiment_id,
            candidate_id,
            "c" * 64,
            "d" * 64,
            "PASS",
            "SIGNAL_PARITY_MATCH",
            None,
        ))

        conflict_artifact = {**artifact, "reason_code": "DIFFERENT"}
        conflict_path = self.root / "signal-parity-conflict.json"
        conflict_path.write_bytes(canonical_bytes(conflict_artifact))
        conflict = strategy_lab.SignalParityRecord(
            experiment_id=record.experiment_id,
            candidate_id=record.candidate_id,
            evaluation_context_id=record.evaluation_context_id,
            data_snapshot_id=record.data_snapshot_id,
            outcome=record.outcome,
            reason_code="DIFFERENT",
            required_action=record.required_action,
            artifact_path=str(conflict_path),
            artifact_sha256=sha256(conflict_path.read_bytes()).hexdigest(),
        )
        with self.assertRaisesRegex(ValueError, "signal parity artifact does not match record"):
            self.ledger.record_signal_parity(conflict)

    def test_existing_hypothesis_and_stage_artifacts_are_reverified_on_conflict(self):
        self.ledger.initialize()
        original = self._hypothesis(24)
        self.ledger.record_hypothesis(original)
        duplicate_path = self.root / "duplicate-hypothesis.json"
        duplicate_path.write_bytes(original.source_path.read_bytes())
        duplicate = load_strategy_hypothesis(duplicate_path)
        original.source_path.unlink()

        with self.assertRaisesRegex(ValueError, "artifact is missing"):
            self.ledger.record_hypothesis(duplicate)

        original.source_path.write_bytes(duplicate_path.read_bytes())
        self.ledger.record_hypothesis(original)
        experiment_id = self._experiment(original, "artifact-conflict")
        first_path, first_hash = self._artifact("first-stage.json", "a")
        second_path, second_hash = self._artifact("second-stage.json", "b")
        self.ledger.record_stage(
            strategy_lab.StageRecord(
                experiment_id,
                "PyBroker completed",
                "PASSED",
                "FIRST",
                first_path,
                first_hash,
            ),
        )

        with self.assertRaisesRegex(ValueError, "stage record conflict"):
            self.ledger.record_stage(
                strategy_lab.StageRecord(
                    experiment_id,
                    "PyBroker completed",
                    "PASSED",
                    "SECOND",
                    second_path,
                    second_hash,
                ),
            )

    def test_experiment_id_includes_strategy_source_policy_engine_and_runtime(self):
        self.ledger.initialize()
        first = self._hypothesis(24)
        second = self._hypothesis(48)
        self.ledger.record_hypothesis(first)
        self.ledger.record_hypothesis(second)
        identities = [
            strategy_lab.ExperimentIdentity(
                first.strategy_id, "source-a", "policy-a", "engine-a", "runtime-a"
            ),
            strategy_lab.ExperimentIdentity(
                second.strategy_id, "source-a", "policy-a", "engine-a", "runtime-a"
            ),
            strategy_lab.ExperimentIdentity(
                first.strategy_id, "source-b", "policy-a", "engine-a", "runtime-a"
            ),
            strategy_lab.ExperimentIdentity(
                first.strategy_id, "source-a", "policy-b", "engine-a", "runtime-a"
            ),
            strategy_lab.ExperimentIdentity(
                first.strategy_id, "source-a", "policy-a", "engine-b", "runtime-a"
            ),
            strategy_lab.ExperimentIdentity(
                first.strategy_id, "source-a", "policy-a", "engine-a", "runtime-b"
            ),
        ]

        experiment_ids = {
            self.ledger.record_experiment(
                second.hypothesis_id
                if identity.strategy_id == second.strategy_id
                else first.hypothesis_id,
                identity,
            )
            for identity in identities
        }

        self.assertEqual(len(experiment_ids), len(identities))

    def test_stage_projection_counts_a_recovered_strategy_only_as_passed(self):
        self.ledger.initialize()
        hypothesis = self._hypothesis(24)
        self.ledger.record_hypothesis(hypothesis)
        identities = [
            strategy_lab.ExperimentIdentity(
                hypothesis.strategy_id,
                "source",
                "policy",
                engine,
                "runtime",
            )
            for engine in ("engine-before-fix", "engine-after-fix")
        ]
        experiments = [
            self.ledger.record_experiment(hypothesis.hypothesis_id, identity)
            for identity in identities
        ]
        artifact = self.root / "stage.json"
        artifact.write_bytes(b"{}\n")
        artifact_hash = sha256(artifact.read_bytes()).hexdigest()
        self.ledger.record_stage(
            strategy_lab.StageRecord(
                experiments[0],
                "Nautilus replayed",
                "REJECTED",
                "ENGINE_FAILED",
                str(artifact),
                artifact_hash,
            ),
        )
        self.ledger.record_stage(
            strategy_lab.StageRecord(
                experiments[1],
                "Nautilus replayed",
                "PASSED",
                "NAUTILUS_REPLAY_COMPLETED",
                str(artifact),
                artifact_hash,
            ),
        )

        with closing(sqlite3.connect(self.ledger.path)) as connection:
            projection = strategy_lab._stage_projection(
                connection,
                "Nautilus replayed",
                "source",
                "policy",
            )

        self.assertEqual(projection, (1, 1, 0))

    def test_success_rejection_and_error_records_are_all_retained(self):
        self.ledger.initialize()
        hypotheses = [self._hypothesis(lookback) for lookback in (24, 48, 72)]
        for hypothesis in hypotheses:
            self.ledger.record_hypothesis(hypothesis)
        experiments = [
            self._experiment(hypothesis, str(index))
            for index, hypothesis in enumerate(hypotheses)
        ]

        success_path, success_hash = self._artifact("success.json", "a")
        reject_path, reject_hash = self._artifact("reject.json", "b")
        error_path, error_hash = self._artifact("error.json", "c")
        self.ledger.record_verdict(
            strategy_lab.VerdictRecord(
                experiments[0], "SUCCESS", "RETAINED", success_path, success_hash
            ),
        )
        self.ledger.record_verdict(
            strategy_lab.VerdictRecord(
                experiments[1], "REJECTION", "NET_RESULT", reject_path, reject_hash
            ),
        )
        self.ledger.record_error(
            strategy_lab.ErrorRecord(
                experiments[2], "PYBROKER", "PROCESS_FAILED", error_path, error_hash
            ),
        )

        with closing(sqlite3.connect(self.ledger.path)) as connection:
            verdicts = connection.execute(
                "SELECT outcome FROM verdicts ORDER BY outcome"
            ).fetchall()
            errors = connection.execute("SELECT reason_code FROM errors").fetchall()
        self.assertEqual(verdicts, [("REJECTION",), ("SUCCESS",)])
        self.assertEqual(errors, [("PROCESS_FAILED",)])

    def test_one_experiment_cannot_have_two_terminal_errors(self):
        self.ledger.initialize()
        hypothesis = self._hypothesis(24)
        self.ledger.record_hypothesis(hypothesis)
        experiment_id = self._experiment(hypothesis, "duplicate-error")
        first_path, first_hash = self._artifact("first-error.json", "a")
        second_path, second_hash = self._artifact("second-error.json", "b")
        self.ledger.record_error(
            strategy_lab.ErrorRecord(
                experiment_id, "PYBROKER", "FIRST", first_path, first_hash
            ),
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.record_error(
                strategy_lab.ErrorRecord(
                    experiment_id, "NAUTILUS", "SECOND", second_path, second_hash
                ),
            )

    def test_initialize_rejects_legacy_duplicate_terminal_errors(self):
        create_legacy_v1_ledger(self.ledger.path)
        with closing(sqlite3.connect(self.ledger.path)) as connection, connection:
            connection.execute(
                "INSERT INTO errors VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "9" * 64,
                    "3" * 64,
                    "NAUTILUS",
                    "SECOND_ERROR",
                    "/legacy/second-error.json",
                    "a" * 64,
                ),
            )

        with self.assertRaisesRegex(ValueError, "duplicate terminal errors"):
            self.ledger.initialize()

    def test_child_requires_an_existing_parent_verdict_pair(self):
        self.ledger.initialize()
        parent = self._hypothesis(24)
        self.ledger.record_hypothesis(parent)
        experiment_id = self._experiment(parent, "parent")
        verdict_path, verdict_hash = self._artifact("verdict.json", "d")
        verdict_id = self.ledger.record_verdict(
            strategy_lab.VerdictRecord(
                experiment_id, "REJECTION", "REVISE", verdict_path, verdict_hash
            ),
        )
        child = self._hypothesis(
            48, parent_strategy_id=parent.strategy_id, based_on_verdict_id=verdict_id
        )

        self.ledger.record_hypothesis(child)

        invalid_child = self._hypothesis(
            72, parent_strategy_id="f" * 64, based_on_verdict_id=verdict_id
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.record_hypothesis(invalid_child)

    def test_records_are_append_only_and_funnel_counts_are_query_derived(self):
        self.ledger.initialize()
        hypotheses = [self._hypothesis(lookback) for lookback in (24, 48, 72)]
        for hypothesis in hypotheses:
            self.ledger.record_hypothesis(hypothesis)
        experiments = [
            self._experiment(hypothesis, str(index))
            for index, hypothesis in enumerate(hypotheses)
        ]
        success_path, success_hash = self._artifact("success.json", "a")
        reject_path, reject_hash = self._artifact("reject.json", "b")
        error_path, error_hash = self._artifact("error.json", "c")
        self.ledger.record_verdict(
            strategy_lab.VerdictRecord(
                experiments[0], "SUCCESS", "RETAINED", success_path, success_hash
            ),
        )
        self.ledger.record_verdict(
            strategy_lab.VerdictRecord(
                experiments[1], "REJECTION", "NET_RESULT", reject_path, reject_hash
            ),
        )
        self.ledger.record_error(
            strategy_lab.ErrorRecord(
                experiments[2], "NAUTILUS", "ENGINE_FAILED", error_path, error_hash
            ),
        )

        counts = self.ledger.funnel_counts()

        self.assertEqual(counts, strategy_lab.FunnelCounts(3, 3, 3, 1, 1, 1))
        with closing(sqlite3.connect(self.ledger.path)) as connection:
            columns = {
                row[1]
                for table in (
                    "strategies",
                    "hypotheses",
                    "experiments",
                    "verdicts",
                    "errors",
                )
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
            self.assertTrue({"status", "state", "lifecycle_stage"}.isdisjoint(columns))
            for table in (
                "strategies",
                "hypotheses",
                "experiments",
                "verdicts",
                "errors",
            ):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    connection.execute(f"UPDATE {table} SET rowid = rowid")
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    connection.execute(f"DELETE FROM {table}")

    def test_aggregate_readers_reject_legacy_verdict_error_conflicts(self):
        self.ledger.initialize()
        hypothesis = self._hypothesis(24)
        self.ledger.record_hypothesis(hypothesis)
        identity = strategy_lab.ExperimentIdentity(
            strategy_id=hypothesis.strategy_id,
            data_source_id="a" * 64,
            policy_id="b" * 64,
            engine_id="nautilus-test",
            runtime_id="c" * 64,
        )
        experiment_id = self.ledger.record_experiment(hypothesis.hypothesis_id, identity)
        verdict = {
            "funding": {"truth_status": "official"},
            "performance_claimable": True,
            "source": {"last_ts_event_ns": 1},
        }
        verdict_path = self.root / "verdict.json"
        verdict_path.write_bytes(canonical_bytes(verdict))
        self.ledger.record_verdict(
            strategy_lab.VerdictRecord(
                experiment_id,
                "SUCCESS",
                "VALID",
                str(verdict_path),
                sha256(verdict_path.read_bytes()).hexdigest(),
            ),
        )
        error_path = self.root / "error.json"
        error_path.write_bytes(
            canonical_bytes(
                {
                    "experiment_id": experiment_id,
                    "reason_code": "CONTRADICTORY",
                    "schema_version": "strategy-loop-error-v1",
                    "stage": "RESEARCH",
                },
            ),
        )
        with closing(sqlite3.connect(self.ledger.path)) as connection, connection:
            connection.execute("DROP TRIGGER errors_reject_existing_verdict")
            connection.execute(
                "INSERT INTO errors VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "d" * 64,
                    experiment_id,
                    "RESEARCH",
                    "CONTRADICTORY",
                    str(error_path),
                    sha256(error_path.read_bytes()).hexdigest(),
                ),
            )

        with self.assertRaisesRegex(ValueError, "contradictory terminal evidence"):
            self.ledger.funnel_counts()
        paths = strategy_lab.StrategyLoopPaths(
            self.root / "market.json",
            self.root / "policy.json",
            self.root / "catalog",
            self.root / "funding",
            self.root,
        )
        with (
            patch.object(strategy_lab, "_hash_tree", return_value=identity.data_source_id),
            patch.object(
                strategy_lab,
                "_policy_identity",
                return_value=(identity.policy_id, "test-v1"),
            ),
            patch.object(strategy_lab, "load_screen_policy", side_effect=OSError("missing")),
            self.assertRaisesRegex(ValueError, "contradictory terminal evidence"),
        ):
            strategy_lab.write_funnel_reports(paths)

    def test_ledger_refuses_missing_or_hash_mismatched_artifacts(self):
        self.ledger.initialize()
        hypothesis = self._hypothesis(24)
        hypothesis.source_path.unlink()
        with self.assertRaisesRegex(ValueError, "artifact is missing"):
            self.ledger.record_hypothesis(hypothesis)

        hypothesis.source_path.write_bytes(canonical_bytes(valid_hypothesis()))
        self.ledger.record_hypothesis(hypothesis)
        experiment = self._experiment(hypothesis, "artifact-integrity")
        path = self.root / "wrong.json"
        path.write_bytes(b"wrong\n")
        with self.assertRaisesRegex(ValueError, "artifact hash mismatch"):
            self.ledger.record_verdict(
                strategy_lab.VerdictRecord(
                    experiment,
                    "REJECTION",
                    "TEST",
                    str(path),
                    "a" * 64,
                ),
            )


class StrategyLoopControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.catalog_path = self.root / "catalog"
        self.catalog_path.mkdir()
        catalog = ParquetDataCatalog(str(self.catalog_path))
        catalog.write_instruments([_instrument()])
        catalog.write_bars(
            [
                make_bar(
                    instrument_id=INSTRUMENT_ID,
                    interval="1h",
                    price_type="LAST",
                    price_precision=2,
                    size_precision=3,
                    open_="1000",
                    high="1000",
                    low="1000",
                    close="1000",
                    volume="10",
                    close_ms=hour * HOUR_MS,
                )
                for hour in range(1, 11)
            ],
        )
        self.funding_path = self.root / "funding"
        migrate_funding_observations(
            client=_FundingClient(modeled_first=False),
            funding_path=self.funding_path,
            symbols=("BTCUSDT", "ETHUSDT"),
            start_ms=4 * HOUR_MS,
            end_ms=13 * HOUR_MS,
        )
        self.market_data_path = self.root / "market-data.json"
        self.market_data_path.write_bytes(Path("config/market_data.json").read_bytes())
        policy = json.loads(Path("config/strategy_loop_policy.json").read_bytes())
        policy["historical_start"] = "1970-01-01T00:00:00Z"
        self.policy_path = self.root / "strategy-loop-policy.json"
        self.policy_path.write_bytes(canonical_bytes(policy))
        self.paths = strategy_lab.StrategyLoopPaths(
            market_data_path=self.market_data_path,
            policy_path=self.policy_path,
            catalog_path=self.catalog_path,
            funding_path=self.funding_path,
            state_path=self.root / "strategy-loop",
        )

    def _hypothesis(
        self,
        lookback_bars: int,
        *,
        parent_strategy_id: str | None = None,
        based_on_verdict_id: str | None = None,
    ) -> Path:
        document = valid_hypothesis()
        parameters = document["parameters"]
        self.assertIsInstance(parameters, dict)
        parameters["lookback_bars"] = lookback_bars
        document["parent_strategy_id"] = parent_strategy_id
        document["based_on_verdict_id"] = based_on_verdict_id
        path = self.root / f"hypothesis-{lookback_bars}.json"
        path.write_bytes(canonical_bytes(document))
        return path

    def _hypothesis_v2(self, lookback_bars: int = 2) -> Path:
        document = valid_hypothesis_v2()
        parameters = document["parameters"]
        self.assertIsInstance(parameters, dict)
        parameters["entry_threshold"] = 0.0
        parameters["lookback_bars"] = lookback_bars
        path = self.root / f"hypothesis-v2-{lookback_bars}.json"
        path.write_bytes(canonical_bytes(document))
        return path

    def _candidate(
        self, lookback_bars: int, *, source_hash: str | None = None
    ) -> bytes:
        return canonical_bytes(
            {
                "bar_type": BAR_TYPE,
                "instrument_id": INSTRUMENT_ID,
                "runtime": {
                    "pybroker_version": "1.2.14",
                    "python_version": "3.12.13",
                    "seed": 42,
                },
                "schema_version": "pybroker-candidate-v1",
                "signals": [
                    {"intent": "LONG", "score": 0.1, "ts_event_ns": HOUR_NS},
                    {"intent": "FLAT", "score": -0.1, "ts_event_ns": 7 * HOUR_NS},
                ],
                "source": {
                    "first_ts_event_ns": HOUR_NS,
                    "last_ts_event_ns": 10 * HOUR_NS,
                    "row_count": 10,
                    "sha256": source_hash or _catalog_digest(self.catalog_path),
                },
                "strategy": {
                    "decision_timing": (
                        "bar-close; effective no earlier than next event"
                    ),
                    "name": "lookback-momentum-long-flat",
                    "parameters": {
                        "entry_threshold": 0.0,
                        "lookback_bars": lookback_bars,
                    },
                },
                "truth_status": "provisional",
            },
        )

    @staticmethod
    def _process(candidate: bytes) -> Callable[..., strategy_lab._ProcessResult]:
        def run(argv, **_kwargs):
            output = Path(argv[argv.index("--output") + 1])
            output.write_bytes(candidate)
            candidate_id = sha256(candidate).hexdigest()
            stdout = canonical_bytes(
                {
                    "candidate_id": candidate_id,
                    "provisional_metrics": {"orders": 2, "signals": 2},
                },
            )
            return strategy_lab._ProcessResult(0, stdout, b"", False, False, False)

        return run

    def _process_v2(self, *, tamper: bool = False) -> Callable[..., strategy_lab._ProcessResult]:
        bars = tuple(
            ClosedBar(
                ts_event_ns=hour * HOUR_NS,
                open=1000,
                high=1000,
                low=1000,
                close=1000,
                volume=10,
            )
            for hour in range(1, 11)
        )

        def run(argv, **_kwargs):
            context = argv[argv.index("--evaluation-context-id") + 1]
            environment = argv[argv.index("--environment-id") + 1]
            decisions = [
                asdict(item)
                for item in evaluate_batch(
                    family_id="lookback-momentum-long-flat",
                    family_version="lookback-momentum-long-flat-v1",
                    parameters={"entry_threshold": 0.0, "lookback_bars": 2},
                    bars=bars,
                )
            ]
            if tamper:
                decisions[0]["score"] = "0.1"
                decisions[0]["signal_id"] = derive_signal_id(
                    family_id=str(decisions[0]["family_id"]),
                    family_version=str(decisions[0]["family_version"]),
                    kernel_hash=str(decisions[0]["kernel_hash"]),
                    kernel_version=str(decisions[0]["kernel_version"]),
                    parameters={"entry_threshold": 0.0, "lookback_bars": 2},
                    reason=str(decisions[0]["reason"]),
                    score=str(decisions[0]["score"]),
                    target_intent=str(decisions[0]["target_intent"]),
                    ts_event_ns=int(decisions[0]["ts_event_ns"]),
                )
            source_hash = _catalog_digest(self.catalog_path)
            candidate = canonical_bytes(
                {
                    "bar_type": BAR_TYPE,
                    "evaluation_context_id": context,
                    "instrument_id": INSTRUMENT_ID,
                    "runtime": {
                        "environment_id": environment,
                        "pybroker_version": "1.2.14",
                        "python_version": "3.12.13",
                        "seed": 42,
                    },
                    "schema_version": "pybroker-candidate-v2",
                    "signals": decisions,
                    "source": {
                        "data_as_of_ns": 10 * HOUR_NS,
                        "data_snapshot_id": source_hash,
                        "first_ts_event_ns": HOUR_NS,
                        "last_ts_event_ns": 10 * HOUR_NS,
                        "row_count": 10,
                        "sha256": source_hash,
                    },
                    "strategy": {
                        "decision_timing": "bar-close; effective no earlier than next event",
                        "family_id": "lookback-momentum-long-flat",
                        "family_version": "lookback-momentum-long-flat-v1",
                        "kernel_hash": KERNEL_HASH,
                        "kernel_version": KERNEL_VERSION,
                        "parameters": {"entry_threshold": 0.0, "lookback_bars": 2},
                    },
                    "truth_status": "provisional",
                },
            )
            output = Path(argv[argv.index("--output") + 1])
            output.write_bytes(candidate)
            stdout = canonical_bytes(
                {
                    "candidate_id": sha256(candidate).hexdigest(),
                    "provisional_metrics": {
                        "max_drawdown": 0.0,
                        "signal_count": len(decisions),
                        "total_return": 0.0,
                        "trade_count": 1,
                        "turnover": 0.0,
                    },
                    "schema_version": "research-result-v2",
                    "truth_status": "provisional",
                },
            )
            return strategy_lab._ProcessResult(0, stdout, b"", False, False, False)

        return run

    def test_pybroker_failure_is_bounded_recorded_and_never_reaches_nautilus(self):
        hypothesis_path = self._hypothesis(24)
        process = strategy_lab._ProcessResult(
            17,
            b"x" * strategy_lab.PROCESS_OUTPUT_LIMIT,
            b"pybroker exploded",
            True,
            False,
            False,
        )

        with patch(
            "nautilus_quant.strategy_lab._run_bounded_process",
            return_value=process,
        ):
            feedback = strategy_lab.run_strategy_loop(hypothesis_path, self.paths)

        experiment_path = self.paths.state_path / "runs" / feedback["experiment_id"]
        error = json.loads((experiment_path / "pybroker-error.json").read_bytes())
        self.assertEqual(feedback["status"], "ERROR")
        self.assertEqual(feedback["failed_stage"], "PYBROKER")
        self.assertEqual(feedback["reason_codes"], ["PYBROKER_PROCESS_FAILED"])
        self.assertIsNone(feedback["parent_strategy_id"])
        self.assertEqual(error["exit_code"], 17)
        self.assertEqual(len(error["stdout"]), strategy_lab.PROCESS_OUTPUT_LIMIT)
        self.assertTrue(error["stdout_truncated"])
        self.assertEqual(error["stderr"], "pybroker exploded")
        self.assertFalse((experiment_path / "nautilus-verdict.json").exists())
        with closing(
            sqlite3.connect(self.paths.state_path / "ledger.sqlite3")
        ) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0], 0
            )
            self.assertEqual(
                connection.execute("SELECT stage, reason_code FROM errors").fetchall(),
                [("PYBROKER", "PYBROKER_PROCESS_FAILED")],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT stage, outcome FROM stage_results ORDER BY stage",
                ).fetchall(),
                [("PyBroker completed", "ERROR")],
            )

    def test_valid_candidate_runs_real_nautilus_once_and_identical_rerun_reuses(self):
        hypothesis_path = self._hypothesis(24)
        candidate = self._candidate(24)

        with patch(
            "nautilus_quant.strategy_lab._run_bounded_process",
            side_effect=self._process(candidate),
        ) as run_process:
            first = strategy_lab.run_strategy_loop(hypothesis_path, self.paths)
            second = strategy_lab.run_strategy_loop(hypothesis_path, self.paths)

        experiment_path = self.paths.state_path / "runs" / first["experiment_id"]
        self.assertEqual(first, second)
        self.assertEqual(run_process.call_count, 1)
        argv = run_process.call_args.args[0]
        self.assertEqual(
            argv,
            [
                "research/.venv/bin/python",
                "-I",
                "research/pybroker_research.py",
                "--hypothesis",
                str(experiment_path / f"hypothesis-{first['hypothesis_id']}.json"),
                "--catalog",
                str(self.catalog_path),
                "--output",
                str(experiment_path / "candidate.json"),
            ],
        )
        self.assertEqual(
            run_process.call_args.kwargs["timeout"],
            strategy_lab.PYBROKER_TIMEOUT_SECONDS,
        )
        self.assertEqual(first["status"], "EVALUATED")
        self.assertIsNone(first["failed_stage"])
        for name in (
            f"hypothesis-{first['hypothesis_id']}.json",
            "research-result.json",
            "candidate.json",
            "nautilus-verdict.json",
            f"feedback-{first['hypothesis_id']}.json",
        ):
            self.assertTrue((experiment_path / name).is_file(), name)
        verdict = json.loads((experiment_path / "nautilus-verdict.json").read_bytes())
        self.assertEqual(verdict["status"], "EVALUATED")
        self.assertIn(verdict["decision"], {"REVISE", "RETAIN_FOR_RESEARCH"})
        with closing(
            sqlite3.connect(self.paths.state_path / "ledger.sqlite3")
        ) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0], 1
            )
            artifacts = connection.execute(
                """SELECT artifact_path, artifact_sha256 FROM hypotheses
                UNION ALL SELECT artifact_path, artifact_sha256 FROM stage_results
                UNION ALL SELECT artifact_path, artifact_sha256 FROM verdicts
                UNION ALL SELECT artifact_path, artifact_sha256 FROM errors""",
            ).fetchall()
        for artifact_path, expected_hash in artifacts:
            payload = Path(artifact_path).read_bytes()
            self.assertEqual(sha256(payload).hexdigest(), expected_hash)
        self.assertFalse(
            any(path.name.startswith(".") for path in experiment_path.iterdir()),
        )

        feedback_path = experiment_path / f"feedback-{first['hypothesis_id']}.json"
        tampered = json.loads(feedback_path.read_bytes())
        tampered["strategy_id"] = "f" * 64
        feedback_path.write_bytes(canonical_bytes(tampered))
        with self.assertRaisesRegex(ValueError, "cached feedback mismatch"):
            strategy_lab.run_strategy_loop(hypothesis_path, self.paths)

    def test_v2_controller_records_parity_before_nautilus_and_passes_recomputed_signals(self):
        hypothesis_path = self._hypothesis_v2()
        with (
            patch(
                "nautilus_quant.strategy_lab._run_bounded_process",
                side_effect=self._process_v2(),
            ) as run_process,
            patch(
                "nautilus_quant.strategy_lab.run_candidate_backtest",
                wraps=strategy_lab.run_candidate_backtest,
            ) as run_nautilus,
        ):
            feedback = strategy_lab.run_strategy_loop(hypothesis_path, self.paths)

        self.assertEqual(feedback["status"], "EVALUATED")
        argv = run_process.call_args.args[0]
        context = argv[argv.index("--evaluation-context-id") + 1]
        environment = argv[argv.index("--environment-id") + 1]
        self.assertRegex(context, r"^[0-9a-f]{64}$")
        self.assertRegex(environment, r"^[0-9a-f]{64}$")
        request = run_nautilus.call_args.args[0]
        self.assertIsNotNone(request.signal_parity)
        self.assertEqual(request.signal_parity.outcome, "PASS")
        run_directory = self.paths.state_path / "runs" / feedback["experiment_id"]
        self.assertTrue((run_directory / "signal-parity-result.json").is_file())
        with closing(sqlite3.connect(self.paths.state_path / "ledger.sqlite3")) as connection:
            parity = connection.execute(
                """SELECT outcome, reason_code, required_action,
                evaluation_context_id FROM signal_parity_results""",
            ).fetchone()
        self.assertEqual(parity, ("PASS", "SIGNAL_PARITY_MATCH", None, context))

    def test_funnel_includes_v2_composite_policy_experiments(self):
        hypothesis_path = self._hypothesis_v2()
        with patch(
            "nautilus_quant.strategy_lab._run_bounded_process",
            side_effect=self._process_v2(),
        ):
            feedback = strategy_lab.run_strategy_loop(hypothesis_path, self.paths)

        report, _markdown = strategy_lab.write_funnel_reports(self.paths)

        self.assertEqual(feedback["status"], "EVALUATED")
        self.assertEqual(report["stages"][0]["entered"], 1)
        self.assertEqual(report["stages"][2]["passed"], 1)
        self.assertEqual(report["stages"][3]["passed"], 1)
        self.assertEqual(report["stages"][4]["passed"], 1)

    def test_v2_parity_mismatch_is_fix_technical_and_never_calls_nautilus(self):
        hypothesis_path = self._hypothesis_v2()
        with (
            patch(
                "nautilus_quant.strategy_lab._run_bounded_process",
                side_effect=self._process_v2(tamper=True),
            ),
            patch("nautilus_quant.strategy_lab.run_candidate_backtest") as run_nautilus,
        ):
            feedback = strategy_lab.run_strategy_loop(hypothesis_path, self.paths)

        run_nautilus.assert_not_called()
        self.assertEqual(feedback["status"], "ERROR")
        self.assertEqual(feedback["failed_stage"], "RESEARCH")
        self.assertEqual(feedback["reason_codes"], ["SIGNAL_PARITY_MISMATCH"])
        with closing(sqlite3.connect(self.paths.state_path / "ledger.sqlite3")) as connection:
            parity = connection.execute(
                "SELECT outcome, reason_code, required_action FROM signal_parity_results",
            ).fetchone()
            verdict_count = connection.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0]
        self.assertEqual(parity, ("ERROR", "SIGNAL_PARITY_MISMATCH", "FIX_TECHNICAL"))
        self.assertEqual(verdict_count, 0)

    def test_cached_experiment_reverifies_stage_artifacts(self):
        hypothesis_path = self._hypothesis(24)
        candidate = self._candidate(24)
        with patch(
            "nautilus_quant.strategy_lab._run_bounded_process",
            side_effect=self._process(candidate),
        ) as run_process:
            feedback = strategy_lab.run_strategy_loop(hypothesis_path, self.paths)
            experiment_path = self.paths.state_path / "runs" / feedback["experiment_id"]
            (experiment_path / "research-result.json").unlink()

            with self.assertRaisesRegex(ValueError, "artifact is missing"):
                strategy_lab.run_strategy_loop(hypothesis_path, self.paths)

        self.assertEqual(run_process.call_count, 1)

    def test_same_strategy_under_a_second_hypothesis_reuses_one_experiment(self):
        first_path = self._hypothesis(24)
        alternative = valid_hypothesis()
        alternative["thesis"] = "Same strategy, independently stated hypothesis."
        second_path = self.root / "alternative-hypothesis.json"
        second_path.write_bytes(canonical_bytes(alternative))
        candidate = self._candidate(24)

        with patch(
            "nautilus_quant.strategy_lab._run_bounded_process",
            side_effect=self._process(candidate),
        ) as run_process:
            first = strategy_lab.run_strategy_loop(first_path, self.paths)
            second = strategy_lab.run_strategy_loop(second_path, self.paths)

        self.assertEqual(first["experiment_id"], second["experiment_id"])
        self.assertEqual(first["strategy_id"], second["strategy_id"])
        self.assertNotEqual(first["hypothesis_id"], second["hypothesis_id"])
        self.assertEqual(run_process.call_count, 1)
        experiment_path = self.paths.state_path / "runs" / first["experiment_id"]
        for feedback in (first, second):
            self.assertTrue(
                (experiment_path / f"feedback-{feedback['hypothesis_id']}.json").is_file(),
            )

    def test_nautilus_technical_failure_is_blocked_not_strategy_revise(self):
        hypothesis_path = self._hypothesis(24)
        candidate = self._candidate(24, source_hash="f" * 64)

        with patch(
            "nautilus_quant.strategy_lab._run_bounded_process",
            side_effect=self._process(candidate),
        ):
            feedback = strategy_lab.run_strategy_loop(hypothesis_path, self.paths)

        self.assertEqual(feedback["status"], "BLOCKED")
        self.assertEqual(feedback["failed_stage"], "NAUTILUS")
        self.assertEqual(feedback["reason_codes"], ["NAUTILUS_EVALUATION_FAILED"])
        self.assertNotIn("decision", feedback)
        with closing(
            sqlite3.connect(self.paths.state_path / "ledger.sqlite3")
        ) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0], 0
            )
            self.assertEqual(
                connection.execute("SELECT stage, reason_code FROM errors").fetchall(),
                [("NAUTILUS", "NAUTILUS_EVALUATION_FAILED")],
            )

    def test_funnel_uses_unique_versions_and_writes_both_atomic_formats(self):
        root_path = self._hypothesis(24)
        candidate = self._candidate(24)
        with patch(
            "nautilus_quant.strategy_lab._run_bounded_process",
            side_effect=self._process(candidate),
        ):
            root_feedback = strategy_lab.run_strategy_loop(root_path, self.paths)
        child_path = self._hypothesis(
            48,
            parent_strategy_id=root_feedback["strategy_id"],
            based_on_verdict_id=root_feedback["verdict_id"],
        )
        failure = strategy_lab._ProcessResult(9, b"", b"screen failed", False, False, False)
        with patch(
            "nautilus_quant.strategy_lab._run_bounded_process",
            return_value=failure,
        ):
            child_feedback = strategy_lab.run_strategy_loop(child_path, self.paths)
            strategy_lab.run_strategy_loop(child_path, self.paths)

        report, markdown = strategy_lab.write_funnel_reports(self.paths)

        self.assertEqual(
            child_feedback["parent_strategy_id"], root_feedback["strategy_id"]
        )
        self.assertEqual(
            child_feedback["based_on_verdict_id"], root_feedback["verdict_id"]
        )
        self.assertEqual(
            [stage["label"] for stage in report["stages"]],
            [
                "Proposed",
                "Contract valid",
                "PyBroker completed",
                "Research screened",
                "Nautilus replayed",
                "Robustness passed",
                "Promotion eligible",
            ],
        )
        pybroker = report["stages"][2]
        self.assertEqual(
            (pybroker["entered"], pybroker["passed"], pybroker["rejected"]),
            (2, 1, 0),
        )
        self.assertEqual(pybroker["previous_stage_survival_rate"], 0.5)
        self.assertEqual(report["stages"][5]["passed"], 0)
        self.assertEqual(report["stages"][6]["passed"], 0)
        self.assertEqual(report["funding_truth_counts"]["official"], 1)
        self.assertEqual(report["performance_claimability_counts"]["not_claimable"], 1)
        self.assertEqual(report["data_as_of_ns"], 10 * HOUR_NS)
        self.assertEqual(report["policy_version"], "strategy-loop-decision-v1")
        self.assertIn(
            "PYBROKER_PROCESS_FAILED",
            [item["reason_code"] for item in report["top_reason_codes"]],
        )
        self.assertEqual(
            (self.paths.state_path / "latest-funnel.json").read_bytes(),
            canonical_bytes(report),
        )
        self.assertEqual(
            (self.paths.state_path / "latest-funnel.md").read_text(),
            markdown,
        )
        self.assertEqual(
            tomllib.loads(Path("pyproject.toml").read_text())["project"]["scripts"][
                "nautilus-research"
            ],
            "nautilus_quant.strategy_lab:main",
        )

    def test_funnel_evidence_deduplicates_engine_replays_of_one_strategy(self):
        hypothesis_path = self._hypothesis(24)
        candidate = self._candidate(24)
        with patch(
            "nautilus_quant.strategy_lab._run_bounded_process",
            side_effect=self._process(candidate),
        ):
            strategy_lab.run_strategy_loop(hypothesis_path, self.paths)
        with (
            patch(
                "nautilus_quant.strategy_lab._run_bounded_process",
                side_effect=self._process(candidate),
            ),
            patch("nautilus_quant.strategy_lab._engine_identity", return_value="engine-replay"),
        ):
            strategy_lab.run_strategy_loop(hypothesis_path, self.paths)

        report, _markdown = strategy_lab.write_funnel_reports(self.paths)

        self.assertEqual(report["funding_truth_counts"]["official"], 1)
        self.assertEqual(report["performance_claimability_counts"]["not_claimable"], 1)

    def test_funnel_uses_one_conservative_category_when_replays_disagree(self):
        hypothesis_path = self._hypothesis(24)
        candidate = self._candidate(24)
        with patch(
            "nautilus_quant.strategy_lab._run_bounded_process",
            side_effect=self._process(candidate),
        ):
            feedback = strategy_lab.run_strategy_loop(hypothesis_path, self.paths)

        ledger = strategy_lab.StrategyLedger(self.paths.state_path / "ledger.sqlite3")
        hypothesis = load_strategy_hypothesis(hypothesis_path)
        with closing(sqlite3.connect(ledger.path)) as connection:
            data_source_id, policy_id, runtime_id = connection.execute(
                """SELECT data_source_id, policy_id, runtime_id FROM experiments
                WHERE experiment_id = ?""",
                (feedback["experiment_id"],),
            ).fetchone()
        conflicting_experiment = ledger.record_experiment(
            hypothesis.hypothesis_id,
            strategy_lab.ExperimentIdentity(
                hypothesis.strategy_id,
                data_source_id,
                policy_id,
                "conflicting-engine",
                runtime_id,
            ),
        )
        original_path = (
            self.paths.state_path
            / "runs"
            / feedback["experiment_id"]
            / "nautilus-verdict.json"
        )
        conflicting = json.loads(original_path.read_bytes())
        conflicting["funding"]["truth_status"] = "missing"
        conflicting["performance_claimable"] = False
        conflicting_path = self.paths.state_path / "conflicting-verdict.json"
        conflicting_hash = strategy_lab._publish_json(conflicting_path, conflicting)
        ledger.record_verdict(
            strategy_lab.VerdictRecord(
                conflicting_experiment,
                "SUCCESS",
                "CONFLICTING_REPLAY",
                str(conflicting_path),
                conflicting_hash,
            ),
        )

        report, _markdown = strategy_lab.write_funnel_reports(self.paths)

        self.assertEqual(report["funding_truth_counts"]["missing"], 1)
        self.assertEqual(report["funding_truth_counts"]["official"], 0)
        self.assertEqual(sum(report["funding_truth_counts"].values()), 1)


class StrategyLoopPolicyTests(unittest.TestCase):
    def test_policy_is_canonical_and_matches_configured_history_start(self):
        policy_path = Path("config/strategy_loop_policy.json")
        market_data_path = Path("config/market_data.json")

        policy_bytes = policy_path.read_bytes()
        policy = json.loads(policy_bytes)
        market_data = json.loads(market_data_path.read_bytes())

        self.assertEqual(policy_bytes, canonical_bytes(policy))
        self.assertEqual(policy["historical_start"], market_data["backtest_start"])
        self.assertEqual(
            set(policy),
            {
                "decision_policy_version",
                "fee_source",
                "fixed_quantity_btc",
                "historical_start",
                "leverage_enabled",
                "official_only_window_start",
                "schema_version",
                "signal_timing",
                "slippage_status",
                "starting_balance_usdt",
            },
        )
        self.assertEqual(policy["schema_version"], "strategy-loop-policy-v1")
        self.assertFalse(policy["leverage_enabled"])
        self.assertEqual(policy["fee_source"], "nautilus_instrument_metadata")
        self.assertEqual(policy["slippage_status"], "unmodeled")


class StrategyLoopIdentityAndConfinementTests(unittest.TestCase):
    def test_code_commit_resolves_a_linked_git_worktree(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            worktree = root / "worktree"
            subprocess.run(
                ["git", "init", "--initial-branch=main", str(repository)],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repository), "config", "user.email", "test@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repository), "config", "user.name", "Test"],
                check=True,
            )
            (repository / "tracked.txt").write_text("tracked\n")
            subprocess.run(["git", "-C", str(repository), "add", "tracked.txt"], check=True)
            subprocess.run(
                ["git", "-C", str(repository), "commit", "-m", "test"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repository), "worktree", "add", "-b", "card/test", str(worktree)],
                check=True,
                capture_output=True,
            )
            expected = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            with patch.object(strategy_lab, "_REPO_ROOT", worktree):
                self.assertEqual(strategy_lab._code_commit(), expected)

    def test_subprocess_capture_is_bounded_while_both_streams_are_drained(self):
        size = strategy_lab.PROCESS_OUTPUT_LIMIT + 10_000

        result = strategy_lab._run_bounded_process(
            [
                sys.executable,
                "-c",
                (
                    "import sys;"
                    f"sys.stdout.buffer.write(b'x'*{size});"
                    f"sys.stderr.buffer.write(b'y'*{size})"
                ),
            ],
            cwd=Path.cwd(),
            timeout=10,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(result.stdout), strategy_lab.PROCESS_OUTPUT_LIMIT)
        self.assertEqual(len(result.stderr), strategy_lab.PROCESS_OUTPUT_LIMIT)
        self.assertTrue(result.stdout_truncated)
        self.assertTrue(result.stderr_truncated)

    def test_runtime_identity_hashes_the_actual_isolated_environment(self):
        original = Path.read_bytes
        site_packages = next(Path("research/.venv/lib").glob("python*/site-packages")).resolve()
        targets = (
            Path("research/.venv/pyvenv.cfg").resolve(),
            site_packages / "pandas/__init__.py",
            site_packages / "joblib/__init__.py",
            site_packages / "numba/__init__.py",
        )
        baseline = strategy_lab._runtime_identity()
        for target in targets:
            with self.subTest(target=target):
                def changed(path: Path) -> bytes:
                    payload = original(path)
                    return payload + b"changed" if Path(path).resolve() == target else payload

                with patch("pathlib.Path.read_bytes", new=changed):
                    self.assertNotEqual(strategy_lab._runtime_identity(), baseline)

    def test_runtime_attestation_can_require_the_dedicated_interpreter(self):
        with self.assertRaisesRegex(ValueError, "attested research interpreter"):
            strategy_lab.research_runtime_identity(Path.cwd(), require_active=True)

    def test_runtime_attestation_hashes_unowned_startup_code(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = root / "research/.venv"
            site_packages = environment / "lib/python3.12/site-packages"
            metadata = site_packages / "demo-1.0.dist-info"
            metadata.mkdir(parents=True)
            (root / "research/requirements.lock").write_text("demo==1.0\n")
            (environment / "pyvenv.cfg").write_text("home = /test\n")
            python = environment / "bin/python"
            python.parent.mkdir(parents=True)
            python.write_bytes(b"python")
            (site_packages / "demo.py").write_bytes(b"VALUE = 1\n")
            (metadata / "METADATA").write_text(
                "Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n",
            )
            (metadata / "RECORD").write_text(
                "demo.py,,\n"
                "demo-1.0.dist-info/METADATA,,\n"
                "demo-1.0.dist-info/RECORD,,\n",
            )

            baseline = strategy_lab.research_runtime_identity(root)
            (site_packages / "sitecustomize.py").write_bytes(b"BEHAVIOR = 2\n")
            with_sitecustomize = strategy_lab.research_runtime_identity(root)
            (site_packages / "sitecustomize.pyc").write_bytes(b"sourceless bytecode")
            with_sourceless_sitecustomize = strategy_lab.research_runtime_identity(root)
            (site_packages / "startup.pth").write_bytes(b"import demo\n")
            with_pth = strategy_lab.research_runtime_identity(root)

        self.assertNotEqual(baseline, with_sitecustomize)
        self.assertNotEqual(with_sitecustomize, with_sourceless_sitecustomize)
        self.assertNotEqual(with_sourceless_sitecustomize, with_pth)

    def test_research_process_uses_isolated_mode_and_sanitized_environment(self):
        code = (
            "import json, os, sys;"
            "print(json.dumps({'isolated': bool(sys.flags.isolated), "
            "'pythonpath': os.environ.get('PYTHONPATH')}))"
        )
        with patch.dict(os.environ, {"PYTHONPATH": "/outside/untrusted"}, clear=False):
            result = strategy_lab._run_bounded_process(
                [sys.executable, "-I", "-c", code],
                cwd=Path.cwd(),
                timeout=10,
            )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {"isolated": True, "pythonpath": None})

    def test_bounded_process_reaps_and_closes_pipes_on_submit_or_drain_failure(self):
        class FakeStream:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class FakeProcess:
            pid = 12345

            def __init__(self):
                self.stdout = FakeStream()
                self.stderr = FakeStream()
                self.returncode = None
                self.wait_calls = 0

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                del timeout
                self.wait_calls += 1
                if self.returncode is None:
                    self.returncode = -15
                return self.returncode

            def terminate(self):
                self.returncode = -15

            def kill(self):
                self.returncode = -9

        for failure in ("submit", "drain"):
            with self.subTest(failure=failure):
                process = FakeProcess()
                patches = [
                    patch.object(strategy_lab.subprocess, "Popen", return_value=process),
                    patch.object(strategy_lab.os, "killpg"),
                ]
                if failure == "submit":
                    patches.append(
                        patch.object(
                            strategy_lab.ThreadPoolExecutor,
                            "submit",
                            side_effect=RuntimeError("submit failed"),
                        ),
                    )
                else:
                    patches.append(
                        patch.object(
                            strategy_lab,
                            "_drain_bounded",
                            side_effect=RuntimeError("drain failed"),
                        ),
                    )
                with patches[0], patches[1], patches[2]:
                    with self.assertRaisesRegex(RuntimeError, f"{failure} failed"):
                        strategy_lab._run_bounded_process(
                            ["research/.venv/bin/python", "-I"],
                            cwd=Path.cwd(),
                            timeout=10,
                        )
                self.assertGreaterEqual(process.wait_calls, 1)
                self.assertTrue(process.stdout.closed)
                self.assertTrue(process.stderr.closed)

    def test_atomic_publish_never_overwrites_an_immutable_artifact(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "state/artifact.json"
            with patch.object(strategy_lab, "_REPO_ROOT", root):
                strategy_lab._atomic_publish(path, b"first\n")

                with self.assertRaisesRegex(ValueError, "immutable artifact conflict"):
                    strategy_lab._atomic_publish(path, b"second\n")

            self.assertEqual(path.read_bytes(), b"first\n")

    def test_engine_identity_changes_with_each_executable_strategy_surface(self):
        original = Path.read_bytes
        relevant = {
            Path("research/pybroker_research.py").resolve(),
            Path("src/nautilus_quant/runtime_attestation.py").resolve(),
            Path("src/nautilus_quant/strategy_lab.py").resolve(),
            Path("src/nautilus_quant/pybroker_candidate.py").resolve(),
        }
        baseline = strategy_lab._engine_identity()

        for changed_path in relevant:
            with self.subTest(changed_path=changed_path):
                def changed(path: Path, target: Path = changed_path) -> bytes:
                    payload = original(path)
                    return payload + b"changed" if Path(path).resolve() == target else payload

                with patch("pathlib.Path.read_bytes", new=changed):
                    self.assertNotEqual(strategy_lab._engine_identity(), baseline)

    def test_data_identity_includes_fee_owning_instrument_metadata(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            market = root / "market.json"
            market.write_bytes(b"{}\n")
            catalog = root / "catalog"
            bars = catalog / "data/bars" / BAR_TYPE
            instruments = catalog / "data/instruments" / INSTRUMENT_ID
            bars.mkdir(parents=True)
            instruments.mkdir(parents=True)
            (bars / "bars.parquet").write_bytes(b"bars")
            instrument = instruments / "instrument.parquet"
            instrument.write_bytes(b"fee-v1")
            funding = root / "funding"
            funding.mkdir()
            (funding / "generation.json").write_bytes(b"funding")
            paths = strategy_lab.StrategyLoopPaths(
                market,
                root / "policy.json",
                catalog,
                funding,
                root / "state",
            )

            first = strategy_lab._hash_tree(paths)
            instrument.write_bytes(b"fee-v2")

            self.assertNotEqual(strategy_lab._hash_tree(paths), first)

    def test_atomic_publish_rejects_a_state_symlink_into_canonical_data(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            state = root / "state"
            state.symlink_to(data, target_is_directory=True)

            with patch.object(strategy_lab, "_REPO_ROOT", root):
                with self.assertRaisesRegex(ValueError, "canonical data"):
                    strategy_lab._atomic_publish(state / "artifact.json", b"{}\n")


if __name__ == "__main__":
    unittest.main()
