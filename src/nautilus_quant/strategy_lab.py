# noqa: E501  # noqa: SIZE_OK — The plan requires Tasks A and D in this focused module.
from __future__ import annotations

import argparse
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
from tempfile import NamedTemporaryFile
from typing import Final, IO, Literal, assert_never

import nautilus_trader

from .candidate_backtest import (
    CandidateBacktestRequest,
    SignalParityResult,
    run_candidate_backtest,
    run_signal_parity_gate,
)
from .pybroker_candidate import load_pybroker_candidate
from .strategy_families import (
    DEFAULT_REGISTRY,
    KERNEL_HASH,
    KERNEL_VERSION,
    FamilyKernelError,
)


type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]


_SCHEMA_VERSION: Final = "strategy-hypothesis-v1"
_SCHEMA_VERSION_V2: Final = "strategy-hypothesis-v2"
_STRATEGY_FAMILY: Final = "lookback-momentum-long-flat"
_FAMILY_VERSION: Final = "lookback-momentum-long-flat-v1"
_INSTRUMENT_ID: Final = "BTCUSDT-PERP.BINANCE"
_BAR_TYPE: Final = "BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL"
_MAX_LOOKBACK_BARS: Final = 8_760
PROCESS_OUTPUT_LIMIT: Final = 65_536
PYBROKER_TIMEOUT_SECONDS: Final = 7_200
_REPO_ROOT: Final = Path(__file__).resolve().parents[2]
_PYBROKER_COMMAND: Final = (
    "research/.venv/bin/python",
    "research/pybroker_research.py",
)
_STAGE_LABELS: Final = (
    "Proposed",
    "Contract valid",
    "PyBroker completed",
    "Research screened",
    "Nautilus replayed",
    "Robustness passed",
    "Promotion eligible",
)
_SHA256: Final = re.compile(r"[0-9a-f]{64}")
_TOP_LEVEL_FIELDS: Final = frozenset(
    {
        "bar_type",
        "based_on_verdict_id",
        "falsification",
        "instrument_id",
        "parameters",
        "parent_strategy_id",
        "schema_version",
        "strategy_family",
        "thesis",
    },
)
_TOP_LEVEL_FIELDS_V2: Final = _TOP_LEVEL_FIELDS | frozenset({"family_version"})
_PARAMETER_FIELDS: Final = frozenset({"entry_threshold", "lookback_bars"})
_FORBIDDEN_FIELDS: Final = frozenset(
    {
        "accounting",
        "cache",
        "callable",
        "code",
        "credential",
        "credentials",
        "executable",
        "fee",
        "fees",
        "funding_policy",
        "import",
        "import_path",
        "joblib",
        "leverage",
        "order",
        "order_type",
        "payload",
        "pickle",
        "pnl",
        "quantity",
    },
)
_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS strategies (
    strategy_id TEXT PRIMARY KEY,
    family TEXT NOT NULL,
    family_version TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    bar_type TEXT NOT NULL,
    identity_schema TEXT NOT NULL CHECK (
        identity_schema IN ('strategy-id-v1', 'strategy-id-v2')
    )
);
CREATE TABLE IF NOT EXISTS hypotheses (
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
CREATE TABLE IF NOT EXISTS experiments (
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
CREATE TABLE IF NOT EXISTS verdicts (
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
CREATE TABLE IF NOT EXISTS errors (
    error_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    artifact_path TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL,
    FOREIGN KEY (experiment_id) REFERENCES experiments(experiment_id)
);
CREATE TABLE IF NOT EXISTS stage_results (
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
CREATE TABLE IF NOT EXISTS signal_parity_results (
    parity_result_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    evaluation_context_id TEXT NOT NULL,
    data_snapshot_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('PASS', 'ERROR')),
    reason_code TEXT NOT NULL,
    required_action TEXT,
    artifact_path TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL,
    UNIQUE (experiment_id, candidate_id),
    CHECK (
        (outcome = 'PASS' AND required_action IS NULL)
        OR (outcome = 'ERROR' AND required_action = 'FIX_TECHNICAL')
    ),
    FOREIGN KEY (experiment_id) REFERENCES experiments(experiment_id)
);
"""
_IMMUTABLE_TABLES: Final = (
    "strategies",
    "hypotheses",
    "experiments",
    "verdicts",
    "errors",
    "stage_results",
    "signal_parity_results",
)


class StrategyLabError(ValueError):
    """Raised when a strategy-loop trust boundary rejects input."""


@dataclass(frozen=True, slots=True)
class StrategyParameters:
    values: dict[str, JsonValue]

    @property
    def lookback_bars(self) -> int:
        value = self.values.get("lookback_bars")
        if isinstance(value, bool) or not isinstance(value, int):
            raise StrategyLabError("strategy has no integer lookback_bars")
        return value

    @property
    def entry_threshold(self) -> float:
        value = self.values.get("entry_threshold")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise StrategyLabError("strategy has no numeric entry_threshold")
        return float(value)


@dataclass(frozen=True, slots=True)
class StrategyHypothesis:
    source_path: Path
    parent_strategy_id: str | None
    based_on_verdict_id: str | None
    thesis: str
    falsification: str
    parameters: StrategyParameters
    family_id: str
    family_version: str
    identity_schema: Literal["strategy-id-v1", "strategy-id-v2"]
    strategy_id: str
    hypothesis_id: str


@dataclass(frozen=True, slots=True)
class ExperimentIdentity:
    strategy_id: str
    data_source_id: str
    policy_id: str
    engine_id: str
    runtime_id: str


@dataclass(frozen=True, slots=True)
class VerdictRecord:
    experiment_id: str
    outcome: Literal["SUCCESS", "REJECTION"]
    reason_code: str
    artifact_path: str
    artifact_sha256: str


@dataclass(frozen=True, slots=True)
class ErrorRecord:
    experiment_id: str
    stage: str
    reason_code: str
    artifact_path: str
    artifact_sha256: str


@dataclass(frozen=True, slots=True)
class FunnelCounts:
    proposed: int
    contract_valid: int
    experimented: int
    succeeded: int
    rejected: int
    errors: int


@dataclass(frozen=True, slots=True)
class StrategyLoopPaths:
    market_data_path: Path
    policy_path: Path
    catalog_path: Path
    funding_path: Path
    state_path: Path


@dataclass(frozen=True, slots=True)
class StageRecord:
    experiment_id: str
    stage: Literal["PyBroker completed", "Research screened", "Nautilus replayed"]
    outcome: Literal["PASSED", "REJECTED", "ERROR"]
    reason_code: str
    artifact_path: str
    artifact_sha256: str


@dataclass(frozen=True, slots=True)
class SignalParityRecord:
    experiment_id: str
    candidate_id: str
    evaluation_context_id: str
    data_snapshot_id: str
    outcome: Literal["PASS", "ERROR"]
    reason_code: str
    required_action: Literal["FIX_TECHNICAL"] | None
    artifact_path: str
    artifact_sha256: str


@dataclass(frozen=True, slots=True)
class _ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool


DEFAULT_LOOP_PATHS: Final = StrategyLoopPaths(
    market_data_path=_REPO_ROOT / "config/market_data.json",
    policy_path=_REPO_ROOT / "config/strategy_loop_policy.json",
    catalog_path=_REPO_ROOT / "data/catalog",
    funding_path=_REPO_ROOT / "data/funding",
    state_path=_REPO_ROOT / "var/strategy-loop",
)


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    value: dict[str, JsonValue] = {}
    for key, item in pairs:
        if key in value:
            raise StrategyLabError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> JsonValue:
    raise StrategyLabError(f"hypothesis must contain only finite JSON values: {value}")


def _canonical_json(value: JsonValue) -> bytes:
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


def _atomic_publish(path: Path, payload: bytes, *, replace: bool = False) -> str:
    if path.resolve(strict=False).is_relative_to((_REPO_ROOT / "data").resolve()):
        raise StrategyLabError("runtime artifact path resolves inside canonical data")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = path.read_bytes()
        if existing == payload:
            return sha256(payload).hexdigest()
        if not replace:
            raise StrategyLabError(f"immutable artifact conflict: {path}")
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        if temporary_path.read_bytes() != payload:
            raise StrategyLabError(f"temporary artifact readback mismatch: {path.name}")
        os.replace(temporary_path, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        if path.read_bytes() != payload:
            raise StrategyLabError(f"published artifact readback mismatch: {path.name}")
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return sha256(payload).hexdigest()


def _publish_json(path: Path, value: JsonValue, *, replace: bool = False) -> str:
    return _atomic_publish(path, _canonical_json(value), replace=replace)


def _verified_artifact(path: Path, expected_hash: str) -> bytes:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise StrategyLabError(f"artifact is missing: {path}") from error
    if sha256(payload).hexdigest() != expected_hash:
        raise StrategyLabError(f"artifact hash mismatch: {path}")
    return payload


def _mapping(
    value: JsonValue, expected: frozenset[str], field: str
) -> dict[str, JsonValue]:
    if not isinstance(value, dict) or set(value) != expected:
        raise StrategyLabError(f"invalid {field} fields")
    return value


def _reject_forbidden_fields(value: JsonValue) -> None:
    match value:
        case dict():
            for key, item in value.items():
                normalized = key.lower().replace("-", "_").replace(" ", "_")
                if normalized in _FORBIDDEN_FIELDS:
                    raise StrategyLabError(f"forbidden hypothesis field: {key}")
                _reject_forbidden_fields(item)
        case list():
            for item in value:
                _reject_forbidden_fields(item)
        case str() | int() | float() | bool() | None:
            return
        case unreachable:
            assert_never(unreachable)


def _nonempty_text(value: JsonValue, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StrategyLabError(f"{field} must be a non-empty string")
    return value


def _lineage_id(value: JsonValue, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise StrategyLabError(f"{field} must be null or lowercase SHA-256")
    return value


def _document(hypothesis: StrategyHypothesis) -> dict[str, JsonValue]:
    document: dict[str, JsonValue] = {
        "bar_type": _BAR_TYPE,
        "based_on_verdict_id": hypothesis.based_on_verdict_id,
        "falsification": hypothesis.falsification,
        "instrument_id": _INSTRUMENT_ID,
        "parameters": hypothesis.parameters.values,
        "parent_strategy_id": hypothesis.parent_strategy_id,
        "schema_version": (
            _SCHEMA_VERSION
            if hypothesis.identity_schema == "strategy-id-v1"
            else _SCHEMA_VERSION_V2
        ),
        "strategy_family": hypothesis.family_id,
        "thesis": hypothesis.thesis,
    }
    if hypothesis.identity_schema == "strategy-id-v2":
        document["family_version"] = hypothesis.family_version
    return document


def load_strategy_hypothesis(path: Path) -> StrategyHypothesis:
    """Load one canonical v1/v2 hypothesis and derive immutable content IDs."""
    source_path = Path(path)
    payload = source_path.read_bytes()
    try:
        decoded: JsonValue = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StrategyLabError("hypothesis must be UTF-8 JSON") from error

    if not isinstance(decoded, dict):
        raise StrategyLabError("hypothesis must be an object")
    schema_version = decoded.get("schema_version")
    if schema_version == _SCHEMA_VERSION:
        root = _mapping(decoded, _TOP_LEVEL_FIELDS, "hypothesis")
        identity_schema: Literal["strategy-id-v1", "strategy-id-v2"] = "strategy-id-v1"
        family_version = _FAMILY_VERSION
    elif schema_version == _SCHEMA_VERSION_V2:
        root = _mapping(decoded, _TOP_LEVEL_FIELDS_V2, "hypothesis v2")
        identity_schema = "strategy-id-v2"
        family_version = _nonempty_text(root["family_version"], "family_version")
    else:
        raise StrategyLabError("unsupported hypothesis schema")

    family_id = _nonempty_text(root["strategy_family"], "strategy_family")
    if root["instrument_id"] != _INSTRUMENT_ID or root["bar_type"] != _BAR_TYPE:
        raise StrategyLabError("unsupported instrument_id or bar_type")

    _reject_forbidden_fields(root["parameters"])
    if identity_schema == "strategy-id-v1":
        if family_id != _STRATEGY_FAMILY:
            raise StrategyLabError("unsupported strategy family")
        parameters = _mapping(root["parameters"], _PARAMETER_FIELDS, "parameters")
        lookback = parameters["lookback_bars"]
        if isinstance(lookback, bool) or not isinstance(lookback, int):
            raise StrategyLabError("parameters.lookback_bars must be an integer")
        if not 1 <= lookback <= _MAX_LOOKBACK_BARS:
            raise StrategyLabError("parameters.lookback_bars must be between 1 and 8760")
        threshold = parameters["entry_threshold"]
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise StrategyLabError("parameters.entry_threshold must be a number")
        if not math.isfinite(threshold) or threshold < 0:
            raise StrategyLabError(
                "parameters.entry_threshold must be finite and non-negative"
            )
        normalized_parameters: dict[str, JsonValue] = {
            "entry_threshold": 0.0 if threshold == 0 else float(threshold),
            "lookback_bars": lookback,
        }
    else:
        if not isinstance(root["parameters"], dict):
            raise StrategyLabError("parameters must be a plain JSON object")
        try:
            definition = DEFAULT_REGISTRY.resolve(family_id, family_version)
            normalized_parameters = definition.validate_parameters(root["parameters"])
        except FamilyKernelError as error:
            raise StrategyLabError(str(error)) from error

    parent_strategy_id = _lineage_id(root["parent_strategy_id"], "parent_strategy_id")
    based_on_verdict_id = _lineage_id(
        root["based_on_verdict_id"], "based_on_verdict_id"
    )
    if (parent_strategy_id is None) != (based_on_verdict_id is None):
        raise StrategyLabError("lineage fields must both be null or both be populated")

    strategy_document: dict[str, JsonValue] = {
        "bar_type": _BAR_TYPE,
        "instrument_id": _INSTRUMENT_ID,
        "parameters": normalized_parameters,
        "strategy_family": family_id,
    }
    if identity_schema == "strategy-id-v2":
        strategy_document.update(
            {
                "family_version": family_version,
                "identity_schema": identity_schema,
            }
        )
    hypothesis = StrategyHypothesis(
        source_path=source_path,
        parent_strategy_id=parent_strategy_id,
        based_on_verdict_id=based_on_verdict_id,
        thesis=_nonempty_text(root["thesis"], "thesis"),
        falsification=_nonempty_text(root["falsification"], "falsification"),
        parameters=StrategyParameters(normalized_parameters),
        family_id=family_id,
        family_version=family_version,
        identity_schema=identity_schema,
        strategy_id=sha256(_canonical_json(strategy_document)).hexdigest(),
        hypothesis_id=sha256(payload).hexdigest(),
    )
    if payload != _canonical_json(_document(hypothesis)):
        raise StrategyLabError("hypothesis must use canonical JSON encoding")
    return hypothesis


def _content_id(value: str, field: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise StrategyLabError(f"{field} must be lowercase SHA-256")
    return value


def _identifier(value: str, field: str) -> str:
    if not value:
        raise StrategyLabError(f"{field} must be non-empty")
    return value


def _experiment_id(identity: ExperimentIdentity) -> str:
    values: dict[str, JsonValue] = {
        "data_source_id": _identifier(identity.data_source_id, "data_source_id"),
        "engine_id": _identifier(identity.engine_id, "engine_id"),
        "policy_id": _identifier(identity.policy_id, "policy_id"),
        "runtime_id": _identifier(identity.runtime_id, "runtime_id"),
        "strategy_id": _content_id(identity.strategy_id, "strategy_id"),
    }
    return sha256(_canonical_json(values)).hexdigest()


def _verdict_record_id(record: VerdictRecord) -> str:
    return sha256(
        _canonical_json(
            {
                "artifact_path": _identifier(record.artifact_path, "artifact_path"),
                "artifact_sha256": _content_id(
                    record.artifact_sha256,
                    "artifact_sha256",
                ),
                "experiment_id": _content_id(record.experiment_id, "experiment_id"),
                "outcome": record.outcome,
                "reason_code": _identifier(record.reason_code, "reason_code"),
            },
        ),
    ).hexdigest()


def _error_record_id(record: ErrorRecord) -> str:
    values: dict[str, JsonValue] = {
        "artifact_path": _identifier(record.artifact_path, "artifact_path"),
        "artifact_sha256": _content_id(record.artifact_sha256, "artifact_sha256"),
        "experiment_id": _content_id(record.experiment_id, "experiment_id"),
        "reason_code": _identifier(record.reason_code, "reason_code"),
        "stage": _identifier(record.stage, "stage"),
    }
    return sha256(_canonical_json(values)).hexdigest()


def _signal_parity_record_id(record: SignalParityRecord) -> str:
    values: dict[str, JsonValue] = {
        "artifact_path": _identifier(record.artifact_path, "artifact_path"),
        "artifact_sha256": _content_id(record.artifact_sha256, "artifact_sha256"),
        "candidate_id": _content_id(record.candidate_id, "candidate_id"),
        "data_snapshot_id": _content_id(record.data_snapshot_id, "data_snapshot_id"),
        "evaluation_context_id": _content_id(
            record.evaluation_context_id,
            "evaluation_context_id",
        ),
        "experiment_id": _content_id(record.experiment_id, "experiment_id"),
        "outcome": record.outcome,
        "reason_code": _identifier(record.reason_code, "reason_code"),
        "required_action": record.required_action,
    }
    if (record.outcome == "PASS") != (record.required_action is None):
        raise StrategyLabError("invalid signal parity outcome/action pairing")
    return sha256(_canonical_json(values)).hexdigest()


def _execute_schema(connection: sqlite3.Connection) -> None:
    statement = ""
    for line in _SCHEMA.splitlines(keepends=True):
        statement += line
        if not sqlite3.complete_statement(statement):
            continue
        sql = statement.strip()
        if sql:
            connection.execute(sql)
        statement = ""
    if statement.strip():
        raise StrategyLabError("internal ledger schema is incomplete")


def _strategy_columns(connection: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(row[1] for row in connection.execute("PRAGMA table_info(strategies)"))


def _migrate_legacy_strategies(connection: sqlite3.Connection) -> bool:
    columns = _strategy_columns(connection)
    if not columns:
        return False
    target = (
        "strategy_id",
        "family",
        "family_version",
        "parameters_json",
        "instrument_id",
        "bar_type",
        "identity_schema",
    )
    if columns == target:
        return False
    legacy = (
        "strategy_id",
        "family",
        "lookback_bars",
        "entry_threshold",
        "instrument_id",
        "bar_type",
    )
    if columns != legacy:
        raise StrategyLabError("unsupported strategies ledger schema")

    migrated: list[tuple[str, str, str, str, str, str, str]] = []
    definition = DEFAULT_REGISTRY.resolve(_STRATEGY_FAMILY, _FAMILY_VERSION)
    for (
        strategy_id,
        family,
        lookback_bars,
        entry_threshold,
        instrument_id,
        bar_type,
    ) in connection.execute("SELECT * FROM strategies ORDER BY rowid"):
        if family != _STRATEGY_FAMILY:
            raise StrategyLabError(f"unsupported legacy strategy family: {family}")
        if instrument_id != _INSTRUMENT_ID or bar_type != _BAR_TYPE:
            raise StrategyLabError("unsupported legacy strategy instrument or bar type")
        try:
            parameters = definition.validate_parameters(
                {
                    "entry_threshold": entry_threshold,
                    "lookback_bars": lookback_bars,
                },
            )
        except FamilyKernelError as error:
            raise StrategyLabError(f"invalid legacy strategy parameters: {error}") from error
        migrated.append(
            (
                _content_id(strategy_id, "legacy strategy_id"),
                family,
                definition.family_version,
                _canonical_json(parameters).decode(),
                instrument_id,
                bar_type,
                "strategy-id-v1",
            ),
        )

    for action in ("update", "delete"):
        connection.execute(f"DROP TRIGGER IF EXISTS strategies_immutable_{action}")
    connection.execute(
        """CREATE TABLE strategies_v2 (
            strategy_id TEXT PRIMARY KEY,
            family TEXT NOT NULL,
            family_version TEXT NOT NULL,
            parameters_json TEXT NOT NULL,
            instrument_id TEXT NOT NULL,
            bar_type TEXT NOT NULL,
            identity_schema TEXT NOT NULL CHECK (
                identity_schema IN ('strategy-id-v1', 'strategy-id-v2')
            )
        )""",
    )
    connection.executemany(
        "INSERT INTO strategies_v2 VALUES (?, ?, ?, ?, ?, ?, ?)",
        migrated,
    )
    connection.execute("DROP TABLE strategies")
    connection.execute("ALTER TABLE strategies_v2 RENAME TO strategies")
    if connection.execute("SELECT COUNT(*) FROM strategies").fetchone()[0] != len(migrated):
        raise StrategyLabError("legacy strategy migration row count mismatch")
    return True


@dataclass(frozen=True, slots=True)
class StrategyLedger:
    """Append-only SQLite persistence for strategy-loop identities and outcomes."""

    path: Path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN IMMEDIATE")
            try:
                migrated_strategies = _migrate_legacy_strategies(connection)
                _execute_schema(connection)
                stage_schema = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'stage_results'",
                ).fetchone()
                if stage_schema is not None and "'ERROR'" not in stage_schema[0]:
                    connection.execute("DROP TRIGGER IF EXISTS stage_results_immutable_update")
                    connection.execute("DROP TRIGGER IF EXISTS stage_results_immutable_delete")
                    connection.execute("ALTER TABLE stage_results RENAME TO legacy_stage_results")
                    connection.execute(
                        """CREATE TABLE stage_results (
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
                        )""",
                    )
                    connection.execute(
                        "INSERT INTO stage_results SELECT * FROM legacy_stage_results",
                    )
                    connection.execute("DROP TABLE legacy_stage_results")
                for table in _IMMUTABLE_TABLES:
                    for action in ("UPDATE", "DELETE"):
                        connection.execute(
                            f"""CREATE TRIGGER IF NOT EXISTS {table}_immutable_{action.lower()}
                            BEFORE {action} ON {table}
                            BEGIN SELECT RAISE(ABORT, '{table} records are immutable'); END""",
                        )
                if migrated_strategies:
                    foreign_key_errors = connection.execute(
                        "PRAGMA foreign_key_check",
                    ).fetchall()
                    if foreign_key_errors:
                        raise StrategyLabError(
                            f"legacy strategy migration broke foreign keys: {foreign_key_errors!r}",
                        )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            connection.execute("PRAGMA foreign_keys = ON")

    def record_hypothesis(self, hypothesis: StrategyHypothesis) -> None:
        _verified_artifact(hypothesis.source_path, hypothesis.hypothesis_id)
        parameters_json = _canonical_json(hypothesis.parameters.values).decode()
        expected_strategy = (
            hypothesis.family_id,
            hypothesis.family_version,
            parameters_json,
            _INSTRUMENT_ID,
            _BAR_TYPE,
            hypothesis.identity_schema,
        )
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                """INSERT INTO strategies (
                    strategy_id, family, family_version, parameters_json,
                    instrument_id, bar_type, identity_schema
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(strategy_id) DO NOTHING""",
                (
                    hypothesis.strategy_id,
                    *expected_strategy,
                ),
            )
            stored_strategy = connection.execute(
                """SELECT family, family_version, parameters_json,
                instrument_id, bar_type, identity_schema
                FROM strategies WHERE strategy_id = ?""",
                (hypothesis.strategy_id,),
            ).fetchone()
            if stored_strategy != expected_strategy:
                raise StrategyLabError("strategy record conflict")
            connection.execute(
                """INSERT INTO hypotheses VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(hypothesis_id) DO NOTHING""",
                (
                    hypothesis.hypothesis_id,
                    hypothesis.strategy_id,
                    hypothesis.parent_strategy_id,
                    hypothesis.based_on_verdict_id,
                    str(hypothesis.source_path),
                    hypothesis.hypothesis_id,
                ),
            )
            stored = connection.execute(
                """SELECT strategy_id, parent_strategy_id, based_on_verdict_id,
                artifact_path, artifact_sha256 FROM hypotheses WHERE hypothesis_id = ?""",
                (hypothesis.hypothesis_id,),
            ).fetchone()
        if stored is None or stored[:3] != (
            hypothesis.strategy_id,
            hypothesis.parent_strategy_id,
            hypothesis.based_on_verdict_id,
        ):
            raise StrategyLabError("hypothesis record conflict")
        _verified_artifact(Path(stored[3]), stored[4])

    def record_experiment(
        self, hypothesis_id: str, identity: ExperimentIdentity
    ) -> str:
        experiment_id = _experiment_id(identity)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                """INSERT INTO experiments VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(experiment_id) DO NOTHING""",
                (
                    experiment_id,
                    _content_id(hypothesis_id, "hypothesis_id"),
                    identity.strategy_id,
                    identity.data_source_id,
                    identity.policy_id,
                    identity.engine_id,
                    identity.runtime_id,
                ),
            )
        return experiment_id

    def record_verdict(self, record: VerdictRecord) -> str:
        _verified_artifact(
            Path(_identifier(record.artifact_path, "artifact_path")),
            _content_id(record.artifact_sha256, "artifact_sha256"),
        )
        verdict_id = _verdict_record_id(record)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys = ON")
            strategy = connection.execute(
                "SELECT strategy_id FROM experiments WHERE experiment_id = ?",
                (record.experiment_id,),
            ).fetchone()
            if strategy is None:
                raise StrategyLabError("verdict experiment does not exist")
            connection.execute(
                "INSERT INTO verdicts VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    verdict_id,
                    record.experiment_id,
                    strategy[0],
                    record.outcome,
                    record.reason_code,
                    record.artifact_path,
                    record.artifact_sha256,
                ),
            )
        return verdict_id

    def record_error(self, record: ErrorRecord) -> str:
        _verified_artifact(
            Path(_identifier(record.artifact_path, "artifact_path")),
            _content_id(record.artifact_sha256, "artifact_sha256"),
        )
        error_id = _error_record_id(record)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                """INSERT INTO errors VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(error_id) DO NOTHING""",
                (
                    error_id,
                    record.experiment_id,
                    record.stage,
                    record.reason_code,
                    record.artifact_path,
                    record.artifact_sha256,
                ),
            )
        return error_id

    def record_stage(self, record: StageRecord) -> None:
        artifact_path = Path(_identifier(record.artifact_path, "artifact_path"))
        _verified_artifact(
            artifact_path,
            _content_id(record.artifact_sha256, "artifact_sha256"),
        )
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys = ON")
            strategy = connection.execute(
                "SELECT strategy_id FROM experiments WHERE experiment_id = ?",
                (record.experiment_id,),
            ).fetchone()
            if strategy is None:
                raise StrategyLabError("stage experiment does not exist")
            connection.execute(
                """INSERT INTO stage_results VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(experiment_id, stage) DO NOTHING""",
                (
                    record.experiment_id,
                    strategy[0],
                    record.stage,
                    record.outcome,
                    record.reason_code,
                    record.artifact_path,
                    record.artifact_sha256,
                ),
            )
            stored = connection.execute(
                """SELECT outcome, reason_code, artifact_path, artifact_sha256
                FROM stage_results WHERE experiment_id = ? AND stage = ?""",
                (record.experiment_id, record.stage),
            ).fetchone()
        expected = (
            record.outcome,
            record.reason_code,
            record.artifact_path,
            record.artifact_sha256,
        )
        if stored != expected:
            raise StrategyLabError("stage record conflict")
        _verified_artifact(Path(stored[2]), stored[3])

    def record_signal_parity(self, record: SignalParityRecord) -> str:
        artifact_path = Path(_identifier(record.artifact_path, "artifact_path"))
        _verified_artifact(
            artifact_path,
            _content_id(record.artifact_sha256, "artifact_sha256"),
        )
        document = _load_canonical_json(artifact_path)
        if not isinstance(document, dict) or (
            document.get("schema_version") != "signal-parity-result-v1"
            or document.get("candidate_id") != record.candidate_id
            or document.get("outcome") != record.outcome
            or document.get("reason_code") != record.reason_code
            or document.get("required_action") != record.required_action
        ):
            raise StrategyLabError("signal parity artifact does not match record")
        parity_result_id = _signal_parity_record_id(record)
        expected = (
            record.experiment_id,
            record.candidate_id,
            record.evaluation_context_id,
            record.data_snapshot_id,
            record.outcome,
            record.reason_code,
            record.required_action,
            record.artifact_path,
            record.artifact_sha256,
        )
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                """INSERT INTO signal_parity_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING""",
                (parity_result_id, *expected),
            )
            stored = connection.execute(
                """SELECT experiment_id, candidate_id, evaluation_context_id,
                data_snapshot_id, outcome, reason_code, required_action,
                artifact_path, artifact_sha256
                FROM signal_parity_results WHERE parity_result_id = ?""",
                (parity_result_id,),
            ).fetchone()
            duplicate = connection.execute(
                """SELECT parity_result_id FROM signal_parity_results
                WHERE experiment_id = ? AND candidate_id = ?""",
                (record.experiment_id, record.candidate_id),
            ).fetchone()
        if stored != expected or duplicate != (parity_result_id,):
            raise StrategyLabError("parity record conflict")
        _verified_artifact(Path(stored[7]), stored[8])
        return parity_result_id

    def verify_experiment_artifacts(self, experiment_id: str) -> None:
        with closing(sqlite3.connect(self.path)) as connection:
            rows = connection.execute(
                """SELECT artifact_path, artifact_sha256 FROM stage_results
                WHERE experiment_id = ?
                UNION ALL
                SELECT artifact_path, artifact_sha256 FROM signal_parity_results
                WHERE experiment_id = ?""",
                (experiment_id, experiment_id),
            ).fetchall()
        for artifact_path, artifact_hash in rows:
            _verified_artifact(Path(artifact_path), artifact_hash)

    def funnel_counts(self) -> FunnelCounts:
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute(
                """SELECT
                (SELECT COUNT(*) FROM strategies),
                (SELECT COUNT(DISTINCT strategy_id) FROM hypotheses),
                (SELECT COUNT(DISTINCT strategy_id) FROM experiments),
                (SELECT COUNT(DISTINCT strategy_id) FROM verdicts WHERE outcome = 'SUCCESS'),
                (SELECT COUNT(DISTINCT strategy_id) FROM verdicts WHERE outcome = 'REJECTION'),
                (SELECT COUNT(DISTINCT experiment_id) FROM errors)""",
            ).fetchone()
        if row is None:
            raise StrategyLabError("funnel query returned no row")
        return FunnelCounts(*(int(value) for value in row))


@dataclass(frozen=True, slots=True)
class _ControllerRun:
    ledger: StrategyLedger
    hypothesis: StrategyHypothesis
    experiment_id: str
    directory: Path


@dataclass(frozen=True, slots=True)
class _TechnicalFailure:
    stage_label: Literal["PyBroker completed", "Research screened", "Nautilus replayed"]
    failed_stage: Literal["PYBROKER", "RESEARCH", "NAUTILUS"]
    status: Literal["ERROR", "BLOCKED"]
    reason_code: str
    artifact_name: str
    evidence: dict[str, JsonValue]


def _hash_tree(paths: StrategyLoopPaths) -> str:
    digest = sha256()
    sources = (
        ("market-data", paths.market_data_path),
        ("catalog", paths.catalog_path / "data" / "bars" / _BAR_TYPE),
        (
            "instrument",
            paths.catalog_path / "data" / "instruments" / _INSTRUMENT_ID,
        ),
        ("funding", paths.funding_path),
    )
    for label, root in sources:
        if not root.exists():
            raise StrategyLabError(f"data source is missing: {root}")
        files = (
            [root]
            if root.is_file()
            else sorted(path for path in root.rglob("*") if path.is_file())
        )
        if not files:
            raise StrategyLabError(f"data source contains no files: {root}")
        for path in files:
            relative = (
                path.name if root.is_file() else path.relative_to(root).as_posix()
            )
            digest.update(label.encode())
            digest.update(b"\0")
            digest.update(relative.encode())
            digest.update(b"\0")
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
    return digest.hexdigest()


def _policy_identity(path: Path) -> tuple[str, str]:
    payload = path.read_bytes()
    try:
        value: JsonValue = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StrategyLabError("strategy loop policy must be UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise StrategyLabError("strategy loop policy must be an object")
    version = value.get("decision_policy_version")
    if not isinstance(version, str) or not version:
        raise StrategyLabError("strategy loop policy version is invalid")
    return sha256(payload).hexdigest(), version


@lru_cache(maxsize=1)
def _runtime_identity() -> str:
    environment = _REPO_ROOT / "research/.venv"
    site_packages = sorted((environment / "lib").glob("python*/site-packages"))
    if len(site_packages) != 1:
        raise StrategyLabError("isolated research site-packages is ambiguous")
    dependency_roots = [
        site_packages[0] / package
        for package in ("pybroker", "pandas", "numpy", "pyarrow")
    ]
    for pattern in (
        "lib_pybroker-*.dist-info",
        "pandas-*.dist-info",
        "numpy-*.dist-info",
        "pyarrow-*.dist-info",
    ):
        dependency_roots.extend(sorted(site_packages[0].glob(pattern)))
    roots = (
        _REPO_ROOT / "research/requirements.lock",
        environment / "pyvenv.cfg",
        (environment / "bin/python").resolve(),
        *dependency_roots,
    )
    digest = sha256()
    for root in roots:
        if not root.exists():
            raise StrategyLabError(f"isolated research runtime is missing: {root}")
        files = [root] if root.is_file() else sorted(
            path
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix != ".pyc"
            and "__pycache__" not in path.parts
        )
        for path in files:
            digest.update(path.relative_to(root.parent).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _engine_identity() -> str:
    digest = sha256(nautilus_trader.__version__.encode())
    for path in (
        _REPO_ROOT / "research/pybroker_research.py",
        _REPO_ROOT / "src/nautilus_quant/candidate_backtest.py",
        _REPO_ROOT / "src/nautilus_quant/funding_observation.py",
        _REPO_ROOT / "src/nautilus_quant/pybroker_candidate.py",
        _REPO_ROOT / "src/nautilus_quant/strategy_families.py",
        _REPO_ROOT / "src/nautilus_quant/strategy_lab.py",
    ):
        digest.update(path.relative_to(_REPO_ROOT).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return f"nautilus-{nautilus_trader.__version__}-{digest.hexdigest()}"


def _evaluation_context_id(
    hypothesis: StrategyHypothesis,
    *,
    data_source_id: str,
    policy_id: str,
    engine_id: str,
    runtime_id: str,
    code_commit: str,
) -> str:
    return sha256(
        _canonical_json(
            {
                "code_commit": _identifier(code_commit, "code_commit"),
                "data_source_id": _content_id(data_source_id, "data_source_id"),
                "engine_id": _identifier(engine_id, "engine_id"),
                "family_id": hypothesis.family_id,
                "family_version": hypothesis.family_version,
                "kernel_hash": KERNEL_HASH,
                "kernel_version": KERNEL_VERSION,
                "runtime_id": _content_id(runtime_id, "runtime_id"),
                "schema_version": "evaluation-context-v1",
                "screen_policy_id": _content_id(policy_id, "screen_policy_id"),
                "strategy_id": hypothesis.strategy_id,
            },
        ),
    ).hexdigest()


def _code_commit() -> str:
    head = (_REPO_ROOT / ".git/HEAD").read_text().strip()
    if not head.startswith("ref: "):
        return head
    reference = _REPO_ROOT / ".git" / head.removeprefix("ref: ")
    if reference.is_file():
        return reference.read_text().strip()
    for line in (_REPO_ROOT / ".git/packed-refs").read_text().splitlines():
        commit, separator, name = line.partition(" ")
        if separator and name == head.removeprefix("ref: "):
            return commit
    raise StrategyLabError("git HEAD reference cannot be resolved")


def _load_canonical_json(path: Path) -> JsonValue:
    payload = path.read_bytes()
    try:
        value: JsonValue = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StrategyLabError(f"artifact must be UTF-8 JSON: {path}") from error
    if payload != _canonical_json(value):
        raise StrategyLabError(f"artifact must use canonical JSON encoding: {path}")
    return value


def _existing_feedback(
    ledger: StrategyLedger,
    experiment_id: str,
    feedback_path: Path,
    hypothesis: StrategyHypothesis,
) -> dict[str, JsonValue] | None:
    with closing(sqlite3.connect(ledger.path)) as connection:
        verdict = connection.execute(
            """SELECT verdict_id, artifact_path, artifact_sha256
            FROM verdicts WHERE experiment_id = ?""",
            (experiment_id,),
        ).fetchone()
        error = connection.execute(
            """SELECT error_id, stage, reason_code, artifact_path, artifact_sha256
            FROM errors WHERE experiment_id = ? ORDER BY error_id LIMIT 1""",
            (experiment_id,),
        ).fetchone()
    if verdict is None and error is None:
        return None
    ledger.verify_experiment_artifacts(experiment_id)
    base: dict[str, JsonValue] = {
        "based_on_verdict_id": hypothesis.based_on_verdict_id,
        "experiment_id": experiment_id,
        "hypothesis_id": hypothesis.hypothesis_id,
        "parent_strategy_id": hypothesis.parent_strategy_id,
        "schema_version": "strategy-feedback-v1",
        "strategy_id": hypothesis.strategy_id,
    }
    if verdict is not None:
        verdict_id, artifact_path, artifact_hash = verdict
        _verified_artifact(Path(artifact_path), artifact_hash)
        verdict_document = _load_canonical_json(Path(artifact_path))
        if not isinstance(verdict_document, dict):
            raise StrategyLabError("cached verdict artifact must be an object")
        expected: dict[str, JsonValue] = {
            **base,
            "decision": verdict_document.get("decision"),
            "error_id": None,
            "failed_stage": None,
            "reason_codes": verdict_document.get("reason_codes"),
            "status": "EVALUATED",
            "verdict_id": verdict_id,
        }
    else:
        error_id, stage, reason_code, artifact_path, artifact_hash = error
        _verified_artifact(Path(artifact_path), artifact_hash)
        expected = {
            **base,
            "error_id": error_id,
            "failed_stage": stage,
            "reason_codes": [reason_code],
            "status": "BLOCKED" if stage == "NAUTILUS" else "ERROR",
            "verdict_id": None,
        }
    if feedback_path.exists():
        value = _load_canonical_json(feedback_path)
        if not isinstance(value, dict):
            raise StrategyLabError("feedback artifact must be an object")
        if value != expected:
            raise StrategyLabError("cached feedback mismatch")
        return value
    _publish_json(feedback_path, expected)
    return expected


def _feedback_base(run: _ControllerRun) -> dict[str, JsonValue]:
    return {
        "based_on_verdict_id": run.hypothesis.based_on_verdict_id,
        "experiment_id": run.experiment_id,
        "hypothesis_id": run.hypothesis.hypothesis_id,
        "parent_strategy_id": run.hypothesis.parent_strategy_id,
        "schema_version": "strategy-feedback-v1",
        "strategy_id": run.hypothesis.strategy_id,
    }


def _feedback_path(run: _ControllerRun) -> Path:
    return run.directory / f"feedback-{run.hypothesis.hypothesis_id}.json"


def _finish_failure(
    run: _ControllerRun,
    failure: _TechnicalFailure,
) -> dict[str, JsonValue]:
    error_document: dict[str, JsonValue] = {
        "experiment_id": run.experiment_id,
        "reason_code": failure.reason_code,
        "schema_version": "strategy-loop-error-v1",
        "stage": failure.failed_stage,
        **failure.evidence,
    }
    artifact_path = run.directory / failure.artifact_name
    artifact_hash = _publish_json(artifact_path, error_document)
    error_record = ErrorRecord(
        run.experiment_id,
        failure.failed_stage,
        failure.reason_code,
        str(artifact_path),
        artifact_hash,
    )
    feedback: dict[str, JsonValue] = {
        **_feedback_base(run),
        "error_id": _error_record_id(error_record),
        "failed_stage": failure.failed_stage,
        "reason_codes": [failure.reason_code],
        "status": failure.status,
        "verdict_id": None,
    }
    _publish_json(_feedback_path(run), feedback)
    run.ledger.record_stage(
        StageRecord(
            run.experiment_id,
            failure.stage_label,
            "ERROR",
            failure.reason_code,
            str(artifact_path),
            artifact_hash,
        ),
    )
    run.ledger.record_error(error_record)
    return feedback


def _drain_bounded(stream: IO[bytes]) -> tuple[bytes, bool]:
    captured = bytearray()
    truncated = False
    while chunk := stream.read(8_192):
        remaining = PROCESS_OUTPUT_LIMIT - len(captured)
        if remaining > 0:
            captured.extend(chunk[:remaining])
        truncated = truncated or len(chunk) > remaining
    return bytes(captured), truncated


def _run_bounded_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: int,
) -> _ProcessResult:
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.stdout is None or process.stderr is None:
        process.kill()
        raise StrategyLabError("PyBroker process pipes were not created")
    with ThreadPoolExecutor(max_workers=2) as executor:
        stdout_future = executor.submit(_drain_bounded, process.stdout)
        stderr_future = executor.submit(_drain_bounded, process.stderr)
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            process.wait()
        stdout, stdout_truncated = stdout_future.result()
        stderr, stderr_truncated = stderr_future.result()
    process.stdout.close()
    process.stderr.close()
    return _ProcessResult(
        process.returncode if process.returncode is not None else -1,
        stdout,
        stderr,
        stdout_truncated,
        stderr_truncated,
        timed_out,
    )


def _research_result(payload: bytes) -> dict[str, JsonValue]:
    try:
        value: JsonValue = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StrategyLabError("PyBroker stdout must be one JSON result") from error
    root = _mapping(
        value,
        frozenset({"candidate_id", "provisional_metrics"}),
        "research result",
    )
    _content_id(_nonempty_text(root["candidate_id"], "candidate_id"), "candidate_id")
    metrics = _mapping(
        root["provisional_metrics"],
        frozenset({"orders", "signals"}),
        "provisional_metrics",
    )
    for field in ("orders", "signals"):
        metric = metrics[field]
        if isinstance(metric, bool) or not isinstance(metric, int) or metric < 0:
            raise StrategyLabError(f"provisional_metrics.{field} must be non-negative")
    return root


def _candidate_matches_hypothesis(
    candidate: dict[str, JsonValue],
    hypothesis: StrategyHypothesis,
    evaluation_context_id: str | None,
) -> None:
    strategy = candidate.get("strategy")
    if not isinstance(strategy, dict):
        raise StrategyLabError("validated candidate strategy is invalid")
    parameters = strategy.get("parameters")
    if not isinstance(parameters, dict):
        raise StrategyLabError("validated candidate parameters are invalid")
    common_mismatch = (
        candidate.get("instrument_id") != _INSTRUMENT_ID
        or candidate.get("bar_type") != _BAR_TYPE
        or parameters != hypothesis.parameters.values
    )
    if hypothesis.identity_schema == "strategy-id-v1":
        mismatch = (
            candidate.get("schema_version") != "pybroker-candidate-v1"
            or strategy.get("name") != hypothesis.family_id
            or common_mismatch
        )
    else:
        mismatch = (
            candidate.get("schema_version") != "pybroker-candidate-v2"
            or candidate.get("evaluation_context_id") != evaluation_context_id
            or strategy.get("family_id") != hypothesis.family_id
            or strategy.get("family_version") != hypothesis.family_version
            or strategy.get("kernel_version") != KERNEL_VERSION
            or strategy.get("kernel_hash") != KERNEL_HASH
            or common_mismatch
        )
    if mismatch:
        raise StrategyLabError("candidate does not match immutable hypothesis")


def run_strategy_loop(
    hypothesis_path: Path,
    paths: StrategyLoopPaths = DEFAULT_LOOP_PATHS,
) -> dict[str, JsonValue]:
    """Run or deterministically reuse one PyBroker-to-Nautilus experiment."""
    source_hypothesis = load_strategy_hypothesis(Path(hypothesis_path))
    data_source_id = _hash_tree(paths)
    policy_id, _policy_version = _policy_identity(paths.policy_path)
    base_engine_id = _engine_identity()
    runtime_id = _runtime_identity()
    code_commit = _code_commit()
    evaluation_context_id: str | None = None
    engine_id = base_engine_id
    if source_hypothesis.identity_schema == "strategy-id-v2":
        evaluation_context_id = _evaluation_context_id(
            source_hypothesis,
            data_source_id=data_source_id,
            policy_id=policy_id,
            engine_id=base_engine_id,
            runtime_id=runtime_id,
            code_commit=code_commit,
        )
        engine_id = f"{base_engine_id}-evaluation-{evaluation_context_id}"
    identity = ExperimentIdentity(
        strategy_id=source_hypothesis.strategy_id,
        data_source_id=data_source_id,
        policy_id=policy_id,
        engine_id=engine_id,
        runtime_id=runtime_id,
    )
    experiment_id = _experiment_id(identity)
    ledger = StrategyLedger(paths.state_path / "ledger.sqlite3")
    ledger.initialize()
    directory = paths.state_path / "runs" / experiment_id
    hypothesis_artifact = directory / f"hypothesis-{source_hypothesis.hypothesis_id}.json"
    _atomic_publish(hypothesis_artifact, Path(hypothesis_path).read_bytes())
    hypothesis = load_strategy_hypothesis(hypothesis_artifact)
    ledger.record_hypothesis(hypothesis)
    ledger.record_experiment(hypothesis.hypothesis_id, identity)
    run = _ControllerRun(ledger, hypothesis, experiment_id, directory)
    existing = _existing_feedback(
        ledger,
        experiment_id,
        _feedback_path(run),
        hypothesis,
    )
    if existing is not None:
        return existing

    candidate_path = directory / "candidate.json"
    argv = [
        *_PYBROKER_COMMAND,
        "--hypothesis",
        str(hypothesis_artifact),
        "--catalog",
        str(paths.catalog_path),
        "--output",
        str(candidate_path),
    ]
    if evaluation_context_id is not None:
        argv.extend(
            [
                "--evaluation-context-id",
                evaluation_context_id,
                "--environment-id",
                runtime_id,
            ],
        )
    try:
        completed = _run_bounded_process(
            argv,
            cwd=_REPO_ROOT,
            timeout=PYBROKER_TIMEOUT_SECONDS,
        )
    except OSError as error:
        return _finish_failure(
            run,
            _TechnicalFailure(
                "PyBroker completed",
                "PYBROKER",
                "ERROR",
                "PYBROKER_PROCESS_LAUNCH_FAILED",
                "pybroker-error.json",
                {
                    "argv": argv,
                    "detail": str(error),
                    "exit_code": None,
                    "stderr": "",
                    "stderr_truncated": False,
                    "stdout": "",
                    "stdout_truncated": False,
                },
            ),
        )

    stdout = completed.stdout.decode("utf-8", errors="replace")
    stderr = completed.stderr.decode("utf-8", errors="replace")
    process_evidence: dict[str, JsonValue] = {
        "argv": argv,
        "exit_code": completed.returncode,
        "stderr": stderr,
        "stderr_truncated": completed.stderr_truncated,
        "stdout": stdout,
        "stdout_truncated": completed.stdout_truncated,
    }
    if completed.timed_out:
        return _finish_failure(
            run,
            _TechnicalFailure(
                "PyBroker completed",
                "PYBROKER",
                "ERROR",
                "PYBROKER_PROCESS_TIMEOUT",
                "pybroker-error.json",
                process_evidence,
            ),
        )
    if completed.returncode != 0:
        return _finish_failure(
            run,
            _TechnicalFailure(
                "PyBroker completed",
                "PYBROKER",
                "ERROR",
                "PYBROKER_PROCESS_FAILED",
                "pybroker-error.json",
                process_evidence,
            ),
        )
    try:
        research_result = _research_result(completed.stdout)
    except StrategyLabError as error:
        return _finish_failure(
            run,
            _TechnicalFailure(
                "PyBroker completed",
                "PYBROKER",
                "ERROR",
                "PYBROKER_RESULT_INVALID",
                "pybroker-error.json",
                {**process_evidence, "detail": str(error)},
            ),
        )
    research_path = directory / "research-result.json"
    research_hash = _publish_json(research_path, research_result)
    ledger.record_stage(
        StageRecord(
            experiment_id,
            "PyBroker completed",
            "PASSED",
            "PYBROKER_COMPLETED",
            str(research_path),
            research_hash,
        ),
    )

    try:
        _candidate, candidate_id = load_pybroker_candidate(candidate_path)
        if research_result["candidate_id"] != candidate_id:
            raise StrategyLabError("research result candidate_id mismatch")
        candidate_document = _load_canonical_json(candidate_path)
        if not isinstance(candidate_document, dict):
            raise StrategyLabError("validated candidate must be an object")
        _candidate_matches_hypothesis(
            candidate_document,
            hypothesis,
            evaluation_context_id,
        )
    except (OSError, ValueError) as error:
        return _finish_failure(
            run,
            _TechnicalFailure(
                "Research screened",
                "RESEARCH",
                "ERROR",
                "RESEARCH_CANDIDATE_INVALID",
                "research-error.json",
                {"detail": str(error)},
            ),
        )
    candidate_hash = candidate_id
    _verified_artifact(candidate_path, candidate_hash)
    signal_parity: SignalParityResult | None = None
    if evaluation_context_id is not None:
        try:
            signal_parity = run_signal_parity_gate(candidate_path, paths.catalog_path)
            parity_path = directory / "signal-parity-result.json"
            parity_hash = _atomic_publish(parity_path, signal_parity.canonical_bytes)
            if parity_hash != signal_parity.artifact_sha256:
                raise StrategyLabError("signal parity artifact hash mismatch")
            source = candidate_document.get("source")
            if not isinstance(source, dict):
                raise StrategyLabError("validated candidate source is invalid")
            data_snapshot_id = source.get("data_snapshot_id")
            if not isinstance(data_snapshot_id, str):
                raise StrategyLabError("validated candidate data snapshot is invalid")
            parity_record = SignalParityRecord(
                experiment_id=experiment_id,
                candidate_id=candidate_id,
                evaluation_context_id=evaluation_context_id,
                data_snapshot_id=data_snapshot_id,
                outcome=signal_parity.outcome,
                reason_code=signal_parity.reason_code,
                required_action=signal_parity.required_action,
                artifact_path=str(parity_path),
                artifact_sha256=parity_hash,
            )
            parity_result_id = ledger.record_signal_parity(parity_record)
        except (ArithmeticError, LookupError, OSError, RuntimeError, ValueError) as error:
            return _finish_failure(
                run,
                _TechnicalFailure(
                    "Research screened",
                    "RESEARCH",
                    "ERROR",
                    "SIGNAL_PARITY_GATE_FAILED",
                    "research-error.json",
                    {
                        "detail": str(error),
                        "required_action": "FIX_TECHNICAL",
                    },
                ),
            )
        if signal_parity.outcome != "PASS":
            return _finish_failure(
                run,
                _TechnicalFailure(
                    "Research screened",
                    "RESEARCH",
                    "ERROR",
                    signal_parity.reason_code,
                    "research-error.json",
                    {
                        "parity_artifact_path": str(parity_path),
                        "parity_artifact_sha256": parity_hash,
                        "parity_result_id": parity_result_id,
                        "required_action": "FIX_TECHNICAL",
                    },
                ),
            )
    ledger.record_stage(
        StageRecord(
            experiment_id,
            "Research screened",
            "PASSED",
            "RESEARCH_SCREEN_PASSED",
            str(candidate_path),
            candidate_hash,
        ),
    )

    try:
        result = run_candidate_backtest(
            CandidateBacktestRequest(
                candidate_path=candidate_path,
                catalog_path=paths.catalog_path,
                funding_path=paths.funding_path,
                policy_path=paths.policy_path,
                hypothesis_id=hypothesis.hypothesis_id,
                strategy_id=hypothesis.strategy_id,
                experiment_id=experiment_id,
                code_commit=code_commit,
                signal_parity=signal_parity,
            ),
        )
    except (ArithmeticError, LookupError, OSError, RuntimeError, ValueError) as error:
        return _finish_failure(
            run,
            _TechnicalFailure(
                "Nautilus replayed",
                "NAUTILUS",
                "BLOCKED",
                "NAUTILUS_EVALUATION_FAILED",
                "nautilus-error.json",
                {"detail": str(error), "error_type": type(error).__name__},
            ),
        )

    verdict_path = directory / "nautilus-verdict.json"
    verdict_hash = _atomic_publish(verdict_path, result.canonical_bytes)
    if verdict_hash != result.verdict_id:
        raise StrategyLabError("Nautilus verdict hash mismatch")
    decision = result.verdict.get("decision")
    reason_codes = result.verdict.get("reason_codes")
    if decision not in {"REVISE", "RETAIN_FOR_RESEARCH"}:
        raise StrategyLabError("Nautilus verdict decision is invalid")
    if (
        not isinstance(reason_codes, list)
        or not reason_codes
        or not all(isinstance(reason, str) and reason for reason in reason_codes)
    ):
        raise StrategyLabError("Nautilus verdict reason_codes are invalid")
    outcome: Literal["SUCCESS", "REJECTION"] = (
        "SUCCESS" if decision == "RETAIN_FOR_RESEARCH" else "REJECTION"
    )
    verdict_record = VerdictRecord(
        experiment_id,
        outcome,
        reason_codes[0],
        str(verdict_path),
        verdict_hash,
    )
    feedback: dict[str, JsonValue] = {
        **_feedback_base(run),
        "decision": decision,
        "error_id": None,
        "failed_stage": None,
        "reason_codes": reason_codes,
        "status": "EVALUATED",
        "verdict_id": _verdict_record_id(verdict_record),
    }
    _publish_json(_feedback_path(run), feedback)
    ledger.record_stage(
        StageRecord(
            experiment_id,
            "Nautilus replayed",
            "PASSED",
            "NAUTILUS_REPLAY_COMPLETED",
            str(verdict_path),
            verdict_hash,
        ),
    )
    ledger.record_verdict(verdict_record)
    return feedback


def _stage_projection(
    connection: sqlite3.Connection,
    stage: str,
    data_source_id: str,
    policy_id: str,
) -> tuple[int, int, int]:
    row = connection.execute(
        """WITH per_strategy AS (
            SELECT
                stage_results.strategy_id,
                MAX(outcome = 'PASSED') AS passed,
                MAX(outcome = 'REJECTED') AS rejected
            FROM stage_results
            JOIN experiments USING (experiment_id, strategy_id)
            WHERE stage = ? AND data_source_id = ? AND policy_id = ?
            GROUP BY stage_results.strategy_id
        )
        SELECT
            COUNT(*),
            COALESCE(SUM(passed), 0),
            COALESCE(SUM(rejected AND NOT passed), 0)
        FROM per_strategy""",
        (stage, data_source_id, policy_id),
    ).fetchone()
    if row is None:
        raise StrategyLabError(f"funnel stage query returned no row: {stage}")
    return tuple(int(value) for value in row)


def _survival(passed: int, denominator: int) -> float:
    return round(passed / denominator, 6) if denominator else 0.0


def _funnel_markdown(report: dict[str, JsonValue]) -> str:
    cohort = report["cohort"]
    if not isinstance(cohort, dict):
        raise StrategyLabError("funnel cohort is invalid")
    lines = [
        "# Strategy Loop Funnel",
        "",
        f"Policy version: `{report['policy_version']}`  ",
        f"Policy ID: `{cohort['policy_id']}`  ",
        f"Data source ID: `{cohort['data_source_id']}`  ",
        f"Data as of (ns): `{report['data_as_of_ns']}`",
        "",
        "| Stage | Entered | Passed | Rejected | Previous survival | Cumulative survival |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    stages = report["stages"]
    if not isinstance(stages, list):
        raise StrategyLabError("funnel stages are invalid")
    for stage in stages:
        if not isinstance(stage, dict):
            raise StrategyLabError("funnel stage is invalid")
        lines.append(
            f"| {stage['label']} | {stage['entered']} | {stage['passed']} | "
            f"{stage['rejected']} | {stage['previous_stage_survival_rate']:.6f} | "
            f"{stage['cumulative_survival_rate']:.6f} |"
        )
    lines.extend(["", "## Top reason codes", ""])
    reasons = report["top_reason_codes"]
    if not isinstance(reasons, list):
        raise StrategyLabError("funnel reasons are invalid")
    if reasons:
        for reason in reasons:
            if not isinstance(reason, dict):
                raise StrategyLabError("funnel reason is invalid")
            lines.append(f"- `{reason['reason_code']}`: {reason['count']}")
    else:
        lines.append("- None")
    funding = report["funding_truth_counts"]
    claimability = report["performance_claimability_counts"]
    if not isinstance(funding, dict) or not isinstance(claimability, dict):
        raise StrategyLabError("funnel evidence counts are invalid")
    lines.extend(
        [
            "",
            "## Evidence",
            "",
            "Funding truth: "
            + ", ".join(f"{key}={funding[key]}" for key in sorted(funding)),
            "Performance claimability: "
            + ", ".join(f"{key}={claimability[key]}" for key in sorted(claimability)),
            "",
        ],
    )
    return "\n".join(lines)


def write_funnel_reports(
    paths: StrategyLoopPaths = DEFAULT_LOOP_PATHS,
) -> tuple[dict[str, JsonValue], str]:
    """Derive and atomically publish both v1 funnel formats."""
    ledger = StrategyLedger(paths.state_path / "ledger.sqlite3")
    ledger.initialize()
    data_source_id = _hash_tree(paths)
    policy_id, policy_version = _policy_identity(paths.policy_path)
    with closing(sqlite3.connect(ledger.path)) as connection:
        proposed = connection.execute(
            """SELECT COUNT(DISTINCT strategy_id) FROM experiments
            WHERE data_source_id = ? AND policy_id = ?""",
            (data_source_id, policy_id),
        ).fetchone()[0]
        contract_valid = connection.execute(
            """SELECT COUNT(DISTINCT experiments.strategy_id)
            FROM experiments JOIN hypotheses USING (hypothesis_id, strategy_id)
            WHERE data_source_id = ? AND policy_id = ?""",
            (data_source_id, policy_id),
        ).fetchone()[0]
        operational = {
            label: _stage_projection(connection, label, data_source_id, policy_id)
            for label in _STAGE_LABELS[2:5]
        }
        reason_rows = connection.execute(
            """SELECT errors.reason_code, experiments.strategy_id
            FROM errors JOIN experiments USING (experiment_id)
            WHERE data_source_id = ? AND policy_id = ?
            UNION ALL
            SELECT verdicts.reason_code, experiments.strategy_id
            FROM verdicts JOIN experiments USING (experiment_id, strategy_id)
            WHERE data_source_id = ? AND policy_id = ?""",
            (data_source_id, policy_id, data_source_id, policy_id),
        ).fetchall()
        verdict_rows = connection.execute(
            """SELECT experiments.strategy_id, verdicts.artifact_path, verdicts.artifact_sha256
            FROM verdicts JOIN experiments USING (experiment_id, strategy_id)
            WHERE data_source_id = ? AND policy_id = ?""",
            (data_source_id, policy_id),
        ).fetchall()

    stage_counts = [
        (int(proposed), int(proposed), 0),
        (int(proposed), int(contract_valid), int(proposed) - int(contract_valid)),
        operational["PyBroker completed"],
        operational["Research screened"],
        operational["Nautilus replayed"],
        (0, 0, 0),
        (0, 0, 0),
    ]
    stages: list[JsonValue] = []
    proposed_count = stage_counts[0][1]
    previous_passed = proposed_count
    for label, (entered, passed, rejected) in zip(
        _STAGE_LABELS, stage_counts, strict=True
    ):
        stages.append(
            {
                "cumulative_survival_rate": _survival(passed, proposed_count),
                "entered": entered,
                "label": label,
                "passed": passed,
                "previous_stage_survival_rate": _survival(passed, previous_passed),
                "rejected": rejected,
            },
        )
        previous_passed = passed

    reason_strategies: dict[str, set[str]] = {}
    for reason_code, strategy_id in reason_rows:
        reason_strategies.setdefault(str(reason_code), set()).add(str(strategy_id))
    top_reason_codes: list[JsonValue] = [
        {"count": len(strategy_ids), "reason_code": reason_code}
        for reason_code, strategy_ids in sorted(
            reason_strategies.items(),
            key=lambda item: (-len(item[1]), item[0]),
        )[:5]
    ]
    funding_precedence = {
        "official": 0,
        "modeled_funding": 1,
        "mixed": 2,
        "missing": 3,
    }
    funding_truth_by_strategy: dict[str, str] = {}
    performance_by_strategy: dict[str, bool] = {}
    data_as_of_ns: int | None = None
    for strategy_id, artifact_path, artifact_hash in verdict_rows:
        path = Path(str(artifact_path))
        _verified_artifact(path, str(artifact_hash))
        value = _load_canonical_json(path)
        if not isinstance(value, dict):
            raise StrategyLabError("Nautilus verdict artifact must be an object")
        funding = value.get("funding")
        source = value.get("source")
        claimable = value.get("performance_claimable")
        if not isinstance(funding, dict) or not isinstance(source, dict):
            raise StrategyLabError("Nautilus verdict evidence is invalid")
        truth_status = funding.get("truth_status")
        if not isinstance(truth_status, str) or truth_status not in funding_precedence:
            raise StrategyLabError("Nautilus verdict Funding truth is invalid")
        strategy_key = str(strategy_id)
        prior_truth = funding_truth_by_strategy.get(strategy_key)
        if prior_truth is None or funding_precedence[truth_status] > funding_precedence[prior_truth]:
            funding_truth_by_strategy[strategy_key] = truth_status
        if not isinstance(claimable, bool):
            raise StrategyLabError("Nautilus verdict claimability is invalid")
        performance_by_strategy[strategy_key] = (
            performance_by_strategy.get(strategy_key, True) and claimable
        )
        last_timestamp = source.get("last_ts_event_ns")
        if isinstance(last_timestamp, bool) or not isinstance(last_timestamp, int):
            raise StrategyLabError("Nautilus verdict data-as-of is invalid")
        data_as_of_ns = max(data_as_of_ns or last_timestamp, last_timestamp)

    report: dict[str, JsonValue] = {
        "cohort": {"data_source_id": data_source_id, "policy_id": policy_id},
        "data_as_of_ns": data_as_of_ns,
        "funding_truth_counts": {
            key: sum(value == key for value in funding_truth_by_strategy.values())
            for key in funding_precedence
        },
        "performance_claimability_counts": {
            "claimable": sum(performance_by_strategy.values()),
            "not_claimable": sum(not value for value in performance_by_strategy.values()),
        },
        "policy_version": policy_version,
        "schema_version": "strategy-funnel-v1",
        "stages": stages,
        "top_reason_codes": top_reason_codes,
    }
    markdown = _funnel_markdown(report)
    _publish_json(paths.state_path / "latest-funnel.json", report, replace=True)
    _atomic_publish(
        paths.state_path / "latest-funnel.md",
        markdown.encode("utf-8"),
        replace=True,
    )
    return report, markdown


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the deterministic strategy loop")
    commands = parser.add_subparsers(dest="command", required=True)
    run_command = commands.add_parser("run")
    run_command.add_argument("--hypothesis", type=Path, required=True)
    funnel_command = commands.add_parser("funnel")
    funnel_command.add_argument(
        "--format", choices=("json", "markdown"), default="json"
    )
    arguments = parser.parse_args(argv)
    match arguments.command:
        case "run":
            feedback = run_strategy_loop(arguments.hypothesis)
            print(_canonical_json(feedback).decode(), end="")
            return 0 if feedback["status"] == "EVALUATED" else 1
        case "funnel":
            report, markdown = write_funnel_reports()
            if arguments.format == "json":
                print(_canonical_json(report).decode(), end="")
            else:
                print(markdown, end="")
            return 0
        case unreachable:
            assert_never(unreachable)
