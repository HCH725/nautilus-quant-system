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
                    'Candidate specified', 'Research screened', 'Nautilus evaluated'
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
            ("6" * 64, experiment_id, "CANDIDATE", "OLD_ERROR", "/legacy/error.json", "7" * 64),
        )
        connection.execute(
            "INSERT INTO stage_results VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                experiment_id,
                strategy_id,
                "Nautilus evaluated",
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


    def test_strategy_version_is_deduplicated_by_content_id(self):
        self.ledger.initialize()
        hypothesis = self._hypothesis(24)

        self.ledger.record_hypothesis(hypothesis)
        self.ledger.record_hypothesis(hypothesis)

        with closing(sqlite3.connect(self.ledger.path)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM strategies").fetchone()[0]
        self.assertEqual(count, 1)


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
                "Candidate specified",
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
                    "Candidate specified",
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
                "Nautilus evaluated",
                "REJECTED",
                "ENGINE_FAILED",
                str(artifact),
                artifact_hash,
            ),
        )
        self.ledger.record_stage(
            strategy_lab.StageRecord(
                experiments[1],
                "Nautilus evaluated",
                "PASSED",
                "NAUTILUS_EVALUATED",
                str(artifact),
                artifact_hash,
            ),
        )

        with closing(sqlite3.connect(self.ledger.path)) as connection:
            projection = strategy_lab._stage_projection(
                connection,
                "Nautilus evaluated",
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
                experiments[2], "CANDIDATE", "PROCESS_FAILED", error_path, error_hash
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
                experiment_id, "CANDIDATE", "FIRST", first_path, first_hash
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

    def test_paper_admission_is_derived_from_persisted_advance_chain(self):
        self.ledger.initialize()
        hypothesis = self._hypothesis(24)
        self.ledger.record_hypothesis(hypothesis)
        experiment_id = self._experiment(hypothesis, "paper-admission")
        verdict_path, verdict_hash = self._artifact("robustness-verdict.json", "a")
        feedback_path, feedback_hash = self._artifact("robustness-feedback.json", "b")
        action_path, action_hash = self._artifact("robustness-action.json", "c")
        candidate_path = self.root / "candidate-v2.json"
        candidate_path.write_bytes(b"{}\n")
        robustness_verdict_id = "d" * 64
        candidate_id = "e" * 64
        historical_verdict_id = "f" * 64
        base_identity = strategy_lab.ExperimentIdentity(
            hypothesis.strategy_id,
            "1" * 64,
            "2" * 64,
            "historical-engine",
            "3" * 64,
            10,
            "4" * 64,
        )
        survivor = strategy_lab.RobustnessSurvivorContext(
            base_identity,
            experiment_id,
            historical_verdict_id,
            hypothesis.hypothesis_id,
            hypothesis.source_path,
            hypothesis.strategy_id,
            candidate_id,
            candidate_path,
            "5" * 64,
            "6" * 40,
        )
        robustness_verdict = {
            "candidate_id": candidate_id,
            "code_commit": survivor.code_commit,
            "data_as_of_ns": base_identity.data_as_of_ns,
            "data_snapshot_id": base_identity.data_snapshot_id,
            "hypothesis_id": hypothesis.hypothesis_id,
            "robustness_verdict_id": robustness_verdict_id,
            "strategy_id": hypothesis.strategy_id,
            "technical_status": "PASS",
            "economic_status": "PASS",
        }
        action = {
            "action": "ADVANCE",
            "campaign_id": "7" * 64,
            "robustness_verdict_id": robustness_verdict_id,
        }
        candidate = {
            "bar_type": "BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",
            "instrument_id": "BTCUSDT-PERP.BINANCE",
            "strategy": {
                "family_id": hypothesis.family_id,
                "family_version": hypothesis.family_version,
                "kernel_hash": "8" * 64,
                "kernel_version": "strategy-family-kernel-v1",
                "parameters": hypothesis.parameters.values,
            },
        }
        with closing(sqlite3.connect(self.ledger.path)) as connection, connection:
            connection.execute(
                "INSERT INTO robustness_results VALUES (?, ?, ?, ?, ?, 'PASS', 'PASS', 'PASS', 'ADVANCE', ?, ?, ?, ?, ?, ?, ?)",
                (
                    "9" * 64,
                    experiment_id,
                    hypothesis.strategy_id,
                    "a" * 64,
                    "b" * 64,
                    canonical_bytes(["ROBUSTNESS_PASS"]).decode(),
                    verdict_path,
                    verdict_hash,
                    feedback_path,
                    feedback_hash,
                    action_path,
                    action_hash,
                ),
            )

        with (
            patch.object(strategy_lab.StrategyLedger, "robustness_survivor_context", return_value=survivor),
            patch.object(strategy_lab, "load_robustness_verdict_v2", return_value=robustness_verdict),
            patch.object(strategy_lab, "load_action_v1", return_value=action),
            patch.object(strategy_lab, "load_strategy_candidate", return_value=(candidate, candidate_id)),
        ):
            paper_admission = self.ledger.paper_admission(
                robustness_verdict_id,
                code_commit="b" * 40,
                runtime_id="c" * 64,
                instrument_metadata_id="d" * 64,
            )

        self.assertEqual(paper_admission["historical_verdict_id"], historical_verdict_id)
        self.assertEqual(paper_admission["robustness_action"], "ADVANCE")
        self.assertEqual(paper_admission["kernel_hash"], "8" * 64)
        self.assertEqual(paper_admission["runtime_id"], "c" * 64)
        self.assertEqual(paper_admission["code_commit"], "b" * 40)


class StrategyLoopControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.catalog_path = self.root / "catalog"
        self.catalog_path.mkdir()
        catalog = ParquetDataCatalog(str(self.catalog_path))
        catalog.write_instruments([_instrument()])
        closes = [1000, 1000, 1010, 1010, 1000, 1000, 1010, 1010, 1010, 1010]
        catalog.write_bars([
            make_bar(
                instrument_id=INSTRUMENT_ID, interval="1h", price_type="LAST",
                price_precision=2, size_precision=3,
                open_=str(close), high=str(close), low=str(close), close=str(close),
                volume="10", close_ms=hour * HOUR_MS,
            )
            for hour, close in zip(range(1, 11), closes, strict=True)
        ])
        self.funding_path = self.root / "funding"
        migrate_funding_observations(
            client=_FundingClient(modeled_first=False),
            funding_path=self.funding_path, symbols=("BTCUSDT", "ETHUSDT"),
            start_ms=5 * HOUR_MS, end_ms=13 * HOUR_MS,
        )
        self.market_data_path = self.root / "market-data.json"
        self.market_data_path.write_bytes(Path("config/market_data.json").read_bytes())
        policy = json.loads(Path("config/strategy_loop_policy.json").read_bytes())
        policy["historical_start"] = "1970-01-01T00:00:00Z"
        self.policy_path = self.root / "strategy-loop-policy.json"
        self.policy_path.write_bytes(canonical_bytes(policy))
        self.paths = strategy_lab.StrategyLoopPaths(
            market_data_path=self.market_data_path, policy_path=self.policy_path,
            catalog_path=self.catalog_path, funding_path=self.funding_path,
            state_path=self.root / "strategy-loop",
        )

    def _hypothesis(self, lookback_bars: int, *, parent_strategy_id=None, based_on_verdict_id=None) -> Path:
        document = valid_hypothesis()
        document["parameters"]["lookback_bars"] = lookback_bars
        document["parent_strategy_id"] = parent_strategy_id
        document["based_on_verdict_id"] = based_on_verdict_id
        path = self.root / f"hypothesis-{lookback_bars}-{sha256(canonical_bytes(document)).hexdigest()[:8]}.json"
        path.write_bytes(canonical_bytes(document))
        return path

    def _hypothesis_v2(self, lookback_bars: int = 2) -> Path:
        document = valid_hypothesis_v2()
        document["parameters"]["entry_threshold"] = 0.0
        document["parameters"]["lookback_bars"] = lookback_bars
        path = self.root / f"hypothesis-v2-{lookback_bars}.json"
        path.write_bytes(canonical_bytes(document))
        return path

    def test_valid_hypothesis_runs_real_nautilus_and_identical_rerun_reuses(self):
        path = self._hypothesis(2)
        first = strategy_lab.run_strategy_loop(path, self.paths)
        second = strategy_lab.run_strategy_loop(path, self.paths)
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "EVALUATED")
        run_dir = self.paths.state_path / "runs" / first["experiment_id"]
        for name in ("candidate.json", "nautilus-verdict.json", f"feedback-{first['hypothesis_id']}.json"):
            self.assertTrue((run_dir / name).is_file(), name)
        candidate, candidate_id = strategy_lab.load_strategy_candidate(run_dir / "candidate.json")
        self.assertEqual(candidate["schema_version"], "strategy-candidate-v1")
        self.assertEqual(candidate_id, sha256((run_dir / "candidate.json").read_bytes()).hexdigest())
        with closing(sqlite3.connect(self.paths.state_path / "ledger.sqlite3")) as connection:
            stages = connection.execute(
                "SELECT stage, outcome FROM stage_results ORDER BY stage"
            ).fetchall()
        self.assertEqual(stages, [
            ("Candidate specified", "PASSED"),
            ("Nautilus evaluated", "PASSED"),
            ("Research screened", "PASSED"),
        ])

    def test_v2_hypothesis_uses_shared_family_identity_without_parity_handoff(self):
        feedback = strategy_lab.run_strategy_loop(self._hypothesis_v2(), self.paths)
        self.assertEqual(feedback["status"], "EVALUATED")
        run_dir = self.paths.state_path / "runs" / feedback["experiment_id"]
        candidate, _ = strategy_lab.load_strategy_candidate(run_dir / "candidate.json")
        strategy = candidate["strategy"]
        self.assertEqual(strategy["family_id"], "lookback-momentum-long-flat")
        self.assertEqual(strategy["family_version"], "lookback-momentum-long-flat-v1")
        self.assertEqual(strategy["kernel_hash"], KERNEL_HASH)

    def test_cached_experiment_reverifies_candidate_artifact(self):
        path = self._hypothesis(2)
        feedback = strategy_lab.run_strategy_loop(path, self.paths)
        candidate = self.paths.state_path / "runs" / feedback["experiment_id"] / "candidate.json"
        candidate.write_bytes(b"{}\n")
        with self.assertRaisesRegex((ValueError, RuntimeError), "artifact|candidate"):
            strategy_lab.run_strategy_loop(path, self.paths)

    def test_same_strategy_under_second_hypothesis_reuses_experiment(self):
        first_path = self._hypothesis(2)
        alternative = valid_hypothesis()
        alternative["parameters"]["lookback_bars"] = 2
        alternative["thesis"] = "Same strategy, independently stated hypothesis."
        second_path = self.root / "alternative-hypothesis.json"
        second_path.write_bytes(canonical_bytes(alternative))
        first = strategy_lab.run_strategy_loop(first_path, self.paths)
        second = strategy_lab.run_strategy_loop(second_path, self.paths)
        self.assertEqual(first["experiment_id"], second["experiment_id"])
        self.assertEqual(first["strategy_id"], second["strategy_id"])
        self.assertNotEqual(first["hypothesis_id"], second["hypothesis_id"])

    def test_nautilus_technical_failure_is_blocked_not_strategy_rejection(self):
        path = self._hypothesis(2)
        with patch.object(strategy_lab, "run_candidate_backtest", side_effect=RuntimeError("forced Nautilus failure")):
            feedback = strategy_lab.run_strategy_loop(path, self.paths)
        self.assertEqual(feedback["status"], "BLOCKED")
        self.assertEqual(feedback["failed_stage"], "NAUTILUS")
        self.assertEqual(feedback["reason_codes"], ["NAUTILUS_EVALUATION_FAILED"])
        with closing(sqlite3.connect(self.paths.state_path / "ledger.sqlite3")) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT stage FROM errors").fetchone(), ("NAUTILUS",))

    def test_funnel_uses_nautilus_native_stage_labels(self):
        strategy_lab.run_strategy_loop(self._hypothesis(2), self.paths)
        report, markdown = strategy_lab.write_funnel_reports(self.paths)
        self.assertEqual([stage["label"] for stage in report["stages"]], [
            "Proposed", "Contract valid", "Candidate specified", "Research screened",
            "Nautilus evaluated", "Robustness passed", "Promotion eligible",
        ])
        self.assertIn("Candidate specified", markdown)
        self.assertIn("Nautilus evaluated", markdown)

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
            root = Path(temporary); repository = root / "repository"; worktree = root / "worktree"
            subprocess.run(["git", "init", "--initial-branch=main", str(repository)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repository), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repository), "config", "user.name", "Test"], check=True)
            (repository / "tracked.txt").write_text("tracked\n")
            subprocess.run(["git", "-C", str(repository), "add", "tracked.txt"], check=True)
            subprocess.run(["git", "-C", str(repository), "commit", "-m", "test"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repository), "worktree", "add", "-b", "card/test", str(worktree)], check=True, capture_output=True)
            expected = subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            with patch.object(strategy_lab, "_REPO_ROOT", worktree):
                self.assertEqual(strategy_lab._code_commit(), expected)

    def test_root_runtime_identity_is_nautilus_only_and_stable(self):
        first = strategy_lab._runtime_identity(); second = strategy_lab._runtime_identity()
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertEqual(first, second)

    def test_atomic_publish_never_overwrites_an_immutable_artifact(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); path = root / "state/artifact.json"
            with patch.object(strategy_lab, "_REPO_ROOT", root):
                strategy_lab._atomic_publish(path, b"first\n")
                with self.assertRaisesRegex(ValueError, "immutable artifact conflict"):
                    strategy_lab._atomic_publish(path, b"second\n")
            self.assertEqual(path.read_bytes(), b"first\n")

    def test_engine_identity_changes_with_current_executable_surfaces(self):
        original = Path.read_bytes
        relevant = {
            Path("src/nautilus_quant/candidate_backtest.py").resolve(),
            Path("src/nautilus_quant/strategy_candidate.py").resolve(),
            Path("src/nautilus_quant/strategy_families.py").resolve(),
            Path("src/nautilus_quant/strategy_lab.py").resolve(),
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
            root=Path(tmp); market=root/"market.json"; market.write_bytes(b"{}\n")
            catalog=root/"catalog"; bars=catalog/"data/bars"/BAR_TYPE; instruments=catalog/"data/instruments"/INSTRUMENT_ID
            bars.mkdir(parents=True); instruments.mkdir(parents=True)
            (bars/"bars.parquet").write_bytes(b"bars"); instrument=instruments/"instrument.parquet"; instrument.write_bytes(b"fee-v1")
            funding=root/"funding"; funding.mkdir(); (funding/"generation.json").write_bytes(b"funding")
            paths=strategy_lab.StrategyLoopPaths(market, root/"policy.json", catalog, funding, root/"state")
            first=strategy_lab._hash_tree(paths); instrument.write_bytes(b"fee-v2")
            self.assertNotEqual(strategy_lab._hash_tree(paths), first)

    def test_atomic_publish_rejects_a_state_symlink_into_canonical_data(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp); data=root/"data"; data.mkdir(); state=root/"state"; state.symlink_to(data, target_is_directory=True)
            with patch.object(strategy_lab, "_REPO_ROOT", root):
                with self.assertRaisesRegex(ValueError, "canonical data"):
                    strategy_lab._atomic_publish(state/"artifact.json", b"{}\n")


if __name__ == "__main__":
    unittest.main()
