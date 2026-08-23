# noqa: E501  # noqa: SIZE_OK — The plan requires Tasks A and D in this focused module.
from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from concurrent.futures import TimeoutError as FuturesTimeoutError
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
import fcntl
from hashlib import sha256
import json
import math
import os
import platform
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import Final, IO, Literal, assert_never

import nautilus_trader
from nautilus_trader.persistence import ParquetDataCatalog

from .candidate_backtest import (
    CandidateBacktestError,
    CandidateBacktestRequest,
    SignalParityResult,
    candidate_signal_decisions,
    load_signal_parity_result,
    load_candidate_backtest_verdict,
    run_candidate_backtest,
    run_signal_parity_gate,
    validated_candidate_source_bars,
)
from .pybroker_candidate import load_pybroker_candidate
from .runtime_attestation import research_runtime_identity
from .strategy_campaign import (
    CampaignAttempt,
    CampaignPreflight,
    CampaignSpec,
    CampaignTechnicalError,
    CampaignTrial,
    ScreenPolicy,
    StrategyCampaignError,
    TerminalStatus,
    TrialEvidence,
    expand_campaign,
    load_campaign_spec,
    load_research_result_v2,
    load_screen_policy,
    load_screen_result_v1,
    run_campaign,
    screen_research_result,
    screened_result_document,
)
from .strategy_families import (
    DEFAULT_REGISTRY,
    FamilyDecision,
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
_SQLITE_INTEGER_MAX: Final = (1 << 63) - 1
PROCESS_OUTPUT_LIMIT: Final = 65_536
PYBROKER_TIMEOUT_SECONDS: Final = 7_200
_REPO_ROOT: Final = Path(__file__).resolve().parents[2]
_PYBROKER_COMMAND: Final = (
    "research/.venv/bin/python",
    "-I",
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
    UNIQUE (experiment_id),
    FOREIGN KEY (experiment_id) REFERENCES experiments(experiment_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS errors_one_terminal_per_experiment
ON errors(experiment_id);
CREATE TRIGGER IF NOT EXISTS verdicts_reject_existing_error
BEFORE INSERT ON verdicts
WHEN EXISTS (SELECT 1 FROM errors WHERE experiment_id = NEW.experiment_id)
BEGIN
    SELECT RAISE(ABORT, 'experiment already has error evidence');
END;
CREATE TRIGGER IF NOT EXISTS errors_reject_existing_verdict
BEFORE INSERT ON errors
WHEN EXISTS (SELECT 1 FROM verdicts WHERE experiment_id = NEW.experiment_id)
BEGIN
    SELECT RAISE(ABORT, 'experiment already has verdict evidence');
END;
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
CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id TEXT PRIMARY KEY,
    document_json TEXT NOT NULL,
    screen_policy_id TEXT NOT NULL,
    data_as_of_ns INTEGER NOT NULL CHECK (data_as_of_ns >= 0),
    data_source_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS campaign_trials (
    campaign_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    strategy_id TEXT,
    candidate_id TEXT,
    terminal_status TEXT NOT NULL CHECK (
        terminal_status IN ('DUPLICATE_SUPPRESSED', 'TECHNICAL_INVALID', 'SCREEN_REJECTED', 'SURVIVED')
    ),
    execution_started INTEGER NOT NULL CHECK (execution_started IN (0, 1)),
    experiment_id TEXT,
    reason_codes_json TEXT NOT NULL,
    PRIMARY KEY (campaign_id, ordinal),
    CHECK (execution_started = 0 OR experiment_id IS NOT NULL),
    CHECK (candidate_id IS NULL OR experiment_id IS NOT NULL),
    CHECK (
        terminal_status NOT IN ('SCREEN_REJECTED', 'SURVIVED')
        OR (experiment_id IS NOT NULL AND candidate_id IS NOT NULL)
    ),
    FOREIGN KEY (campaign_id) REFERENCES campaigns(campaign_id),
    FOREIGN KEY (experiment_id, strategy_id)
        REFERENCES experiments(experiment_id, strategy_id)
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
    "campaigns",
    "campaign_trials",
)


class StrategyLabError(ValueError):
    """Raised when a strategy-loop trust boundary rejects input."""


class _ExistingNonterminalExperiment(StrategyLabError):
    """Raised after a claimed execution identity has incomplete ledger evidence."""


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
    research_policy_path: Path | None = None


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
    decisions: tuple[FamilyDecision, ...] = ()


@dataclass(frozen=True, slots=True)
class _ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool


@dataclass(frozen=True, slots=True)
class _PreparedExecution:
    identity: ExperimentIdentity
    evaluation_context_id: str | None
    screen_policy: ScreenPolicy | None
    data_as_of_ns: int | None
    runtime_id: str
    code_commit: str
    base_engine_id: str | None = None


@dataclass(slots=True)
class _ExecutionClaim:
    prepared: _PreparedExecution | None = None
    launched: bool = False
    reused: bool = False


DEFAULT_LOOP_PATHS: Final = StrategyLoopPaths(
    market_data_path=_REPO_ROOT / "config/market_data.json",
    policy_path=_REPO_ROOT / "config/strategy_loop_policy.json",
    catalog_path=_REPO_ROOT / "data/catalog",
    funding_path=_REPO_ROOT / "data/funding",
    state_path=_REPO_ROOT / "var/strategy-loop",
    research_policy_path=_REPO_ROOT / "config/strategy_research_policy.json",
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
                errors_table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'errors'",
                ).fetchone()
                if errors_table is not None:
                    duplicate_errors = connection.execute(
                        """SELECT experiment_id FROM errors
                        GROUP BY experiment_id HAVING COUNT(*) > 1 LIMIT 1""",
                    ).fetchone()
                    if duplicate_errors is not None:
                        raise StrategyLabError(
                            "legacy ledger has duplicate terminal errors for one experiment",
                        )
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

    def record_campaign(self, spec: CampaignSpec, preflight: CampaignPreflight) -> None:
        """Insert one immutable campaign specification with readback checks."""
        document = _canonical_json(spec.document).decode()
        expected = (
            document,
            _content_id(preflight.screen_policy_id, "screen_policy_id"),
            preflight.data_as_of_ns,
            _content_id(preflight.data_source_id, "data_source_id"),
        )
        if (
            isinstance(preflight.data_as_of_ns, bool)
            or not isinstance(preflight.data_as_of_ns, int)
            or not 0 <= preflight.data_as_of_ns <= _SQLITE_INTEGER_MAX
        ):
            raise StrategyLabError(
                "campaign data_as_of_ns must be a non-negative signed 64-bit integer",
            )
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                """INSERT INTO campaigns VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(campaign_id) DO NOTHING""",
                (spec.campaign_id, *expected),
            )
            stored = connection.execute(
                """SELECT document_json, screen_policy_id, data_as_of_ns, data_source_id
                FROM campaigns WHERE campaign_id = ?""",
                (spec.campaign_id,),
            ).fetchone()
        if stored != expected:
            raise StrategyLabError("campaign record conflict")

    @staticmethod
    def _campaign_trial_row(
        attempt: CampaignAttempt,
        evidence: TrialEvidence,
    ) -> tuple[object, ...]:
        if attempt.strategy_id is not None:
            _content_id(attempt.strategy_id, "strategy_id")
        if evidence.candidate_id is not None:
            _content_id(evidence.candidate_id, "candidate_id")
        if evidence.experiment_id is not None:
            _content_id(evidence.experiment_id, "experiment_id")
        if not evidence.reason_codes or not all(
            isinstance(reason, str) and reason for reason in evidence.reason_codes
        ):
            raise StrategyLabError("campaign trial requires non-empty reason codes")
        if not isinstance(evidence.execution_started, bool):
            raise StrategyLabError("execution_started must be boolean")
        if evidence.execution_started and evidence.experiment_id is None:
            raise StrategyLabError("started execution requires experiment_id")
        if evidence.candidate_id is not None and evidence.experiment_id is None:
            raise StrategyLabError("candidate_id requires experiment_id")
        if evidence.experiment_id is not None and attempt.strategy_id is None:
            raise StrategyLabError("experiment_id requires strategy_id")
        if evidence.terminal_status in {
            TerminalStatus.SCREEN_REJECTED,
            TerminalStatus.SURVIVED,
        } and (evidence.experiment_id is None or evidence.candidate_id is None):
            raise StrategyLabError(
                "screened terminal evidence requires experiment_id and candidate_id",
            )
        return (
            attempt.campaign_id,
            attempt.ordinal,
            attempt.strategy_id,
            evidence.candidate_id,
            evidence.terminal_status.value,
            int(evidence.execution_started),
            evidence.experiment_id,
            _canonical_json(list(evidence.reason_codes)).decode(),
        )

    def _validate_campaign_terminal_trial(
        self,
        campaign_id: str,
        strategy_id: str | None,
        evidence: TrialEvidence,
        *,
        allow_foreign_key_rejection: bool = False,
    ) -> None:
        """Require a complete V2 terminal chain at the immutable trial boundary."""
        experiment_id = evidence.experiment_id
        if strategy_id is None or evidence.candidate_id is None or experiment_id is None:
            raise StrategyLabError("campaign trial lacks a terminal chain")
        with closing(sqlite3.connect(self.path)) as connection:
            experiment = connection.execute(
                """SELECT hypothesis_id, strategy_id FROM experiments
                WHERE experiment_id = ?""",
                (experiment_id,),
            ).fetchone()
            verdict_rows = connection.execute(
                """SELECT verdict_id, outcome, reason_code, artifact_path, artifact_sha256
                FROM verdicts WHERE experiment_id = ?""",
                (experiment_id,),
            ).fetchall()
            error_count = connection.execute(
                "SELECT COUNT(*) FROM errors WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()[0]
            nautilus_count = connection.execute(
                """SELECT COUNT(*) FROM stage_results
                WHERE experiment_id = ? AND stage = 'Nautilus replayed'""",
                (experiment_id,),
            ).fetchone()[0]
            parity_count = connection.execute(
                "SELECT COUNT(*) FROM signal_parity_results WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()[0]
            pybroker = connection.execute(
                """SELECT outcome, reason_code, artifact_path, artifact_sha256
                FROM stage_results
                WHERE experiment_id = ? AND stage = 'PyBroker completed'""",
                (experiment_id,),
            ).fetchone()
            screen = connection.execute(
                """SELECT outcome, reason_code, artifact_path, artifact_sha256
                FROM stage_results
                WHERE experiment_id = ? AND stage = 'Research screened'""",
                (experiment_id,),
            ).fetchone()
            stage_count = connection.execute(
                "SELECT COUNT(*) FROM stage_results WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()[0]
            campaign = connection.execute(
                "SELECT document_json, screen_policy_id FROM campaigns WHERE campaign_id = ?",
                (campaign_id,),
            ).fetchone()
        if (
            experiment is None
            or experiment[1] != strategy_id
        ):
            # Let SQLite's campaign FK reject nonexistent or cross-strategy links.
            if allow_foreign_key_rejection:
                return
            raise StrategyLabError("campaign trial has an invalid terminal chain")
        try:
            campaign_document = json.loads(str(campaign[0])) if campaign is not None else None
        except json.JSONDecodeError as error:
            raise StrategyLabError("campaign trial has an invalid terminal chain") from error
        if (
            not isinstance(campaign_document, dict)
            or campaign_document.get("screen_policy_id") != campaign[1]
        ):
            raise StrategyLabError("campaign trial has an invalid terminal chain")
        if evidence.terminal_status is TerminalStatus.SCREEN_REJECTED:
            if (
                verdict_rows
                or error_count != 0
                or nautilus_count != 0
                or parity_count != 0
                or stage_count != 2
                or pybroker is None
                or pybroker[0:2] != ("PASSED", "PYBROKER_COMPLETED")
                or screen is None
                or screen[0] != "REJECTED"
            ):
                raise StrategyLabError("campaign trial has an invalid terminal chain")
            try:
                raw_path = Path(str(pybroker[2]))
                raw_payload = _verified_artifact(raw_path, str(pybroker[3]))
                result = load_research_result_v2(raw_payload)
                candidate_path = raw_path.with_name("candidate.json")
                _verified_artifact(candidate_path, result.candidate_id)
                candidate, candidate_id = load_pybroker_candidate(candidate_path)
                screen_payload = _verified_artifact(Path(str(screen[2])), str(screen[3]))
                document = load_screen_result_v1(screen_payload)
            except (OSError, CandidateBacktestError, StrategyCampaignError, TypeError, ValueError) as error:
                raise StrategyLabError("campaign trial has an invalid terminal chain") from error
            reasons = tuple(reason for reason in evidence.reason_codes if reason != "REUSED_EXECUTION")
            expected_metrics = {
                "max_drawdown": result.metrics.max_drawdown,
                "signal_count": result.metrics.signal_count,
                "total_return": result.metrics.total_return,
                "trade_count": result.metrics.trade_count,
                "turnover": result.metrics.turnover,
            }
            candidate_signals = candidate.get("signals")
            if (
                candidate_id != result.candidate_id
                or candidate_id != evidence.candidate_id
                or not isinstance(candidate_signals, list)
                or len(candidate_signals) != result.metrics.signal_count
                or document.get("candidate_id") != candidate_id
                or document.get("provisional_metrics") != expected_metrics
                or document.get("screen_policy_id") != campaign[1]
                or document.get("screen_reason_codes") != list(reasons)
                or screen[1] != reasons[0]
            ):
                raise StrategyLabError("campaign trial has an invalid terminal chain")
            return
        if (
            len(verdict_rows) != 1
            or error_count != 0
            or nautilus_count != 1
            or parity_count != 1
        ):
            raise StrategyLabError("campaign trial has an invalid terminal chain")
        verdict = verdict_rows[0]
        record = VerdictRecord(
            experiment_id,
            str(verdict[1]),
            str(verdict[2]),
            str(verdict[3]),
            str(verdict[4]),
        )
        document = self.validated_verdict_document(record)
        if document is None or document.get("candidate_id") != evidence.candidate_id:
            raise StrategyLabError("campaign trial has an invalid terminal chain")

    def record_campaign_trial(
        self,
        attempt: CampaignAttempt,
        evidence: TrialEvidence,
    ) -> None:
        """Insert one immutable campaign membership row and verify its readback."""
        self.record_campaign_trials(((attempt, evidence),))

    def record_campaign_trials(
        self,
        trials: tuple[tuple[CampaignAttempt, TrialEvidence], ...],
    ) -> None:
        """Atomically publish one complete immutable campaign census."""
        expected_rows = tuple(
            self._campaign_trial_row(attempt, evidence) for attempt, evidence in trials
        )
        for (attempt, evidence), _expected in zip(trials, expected_rows, strict=True):
            if evidence.terminal_status in {
                TerminalStatus.SCREEN_REJECTED,
                TerminalStatus.SURVIVED,
            }:
                self._validate_campaign_terminal_trial(
                    attempt.campaign_id,
                    attempt.strategy_id,
                    evidence,
                    allow_foreign_key_rejection=True,
                )
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys = ON")
            for expected in expected_rows:
                connection.execute(
                    """INSERT INTO campaign_trials (
                        campaign_id, ordinal, strategy_id, candidate_id,
                        terminal_status, execution_started, experiment_id,
                        reason_codes_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(campaign_id, ordinal) DO NOTHING""",
                    expected,
                )
                stored = connection.execute(
                    """SELECT campaign_id, ordinal, strategy_id, candidate_id,
                        terminal_status, execution_started, experiment_id,
                        reason_codes_json
                    FROM campaign_trials WHERE campaign_id = ? AND ordinal = ?""",
                    (expected[0], expected[1]),
                ).fetchone()
                if stored != expected:
                    raise StrategyLabError("campaign trial record conflict")

    def campaign_trials(self, campaign_id: str) -> tuple[CampaignTrial, ...]:
        """Read the complete immutable campaign membership census."""
        _content_id(campaign_id, "campaign_id")
        with closing(sqlite3.connect(self.path)) as connection:
            rows = connection.execute(
                """SELECT campaign_id, ordinal, strategy_id, candidate_id,
                    terminal_status, execution_started, experiment_id,
                    reason_codes_json
                FROM campaign_trials WHERE campaign_id = ? ORDER BY ordinal""",
                (campaign_id,),
            ).fetchall()
        trials: list[CampaignTrial] = []
        for row in rows:
            try:
                status = TerminalStatus(str(row[4]))
            except ValueError as error:
                raise StrategyLabError("campaign trial status is invalid") from error
            reasons = json.loads(str(row[7]))
            if (
                not isinstance(reasons, list)
                or not all(isinstance(reason, str) and reason for reason in reasons)
            ):
                raise StrategyLabError("campaign trial reason codes are invalid")
            trials.append(
                CampaignTrial(
                    campaign_id=str(row[0]),
                    ordinal=int(row[1]),
                    strategy_id=str(row[2]) if row[2] is not None else None,
                    candidate_id=str(row[3]) if row[3] is not None else None,
                    evidence=TrialEvidence(
                        terminal_status=status,
                        execution_started=bool(row[5]),
                        reason_codes=tuple(reasons),
                        experiment_id=str(row[6]) if row[6] is not None else None,
                        candidate_id=str(row[3]) if row[3] is not None else None,
                    ),
                ),
            )
        for trial in trials:
            if trial.evidence.terminal_status in {
                TerminalStatus.SCREEN_REJECTED,
                TerminalStatus.SURVIVED,
            }:
                self._validate_campaign_terminal_trial(
                    trial.campaign_id,
                    trial.strategy_id,
                    trial.evidence,
                )
        return tuple(trials)

    def verify_campaign_trial(
        self,
        trial: CampaignTrial,
        screen_policy: ScreenPolicy | None,
    ) -> None:
        """Revalidate one stored campaign membership against immutable execution evidence."""
        experiment_id = trial.evidence.experiment_id
        if experiment_id is None:
            return
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute(
                """SELECT strategy_id, data_source_id, policy_id, engine_id, runtime_id
                FROM experiments WHERE experiment_id = ?""",
                (experiment_id,),
            ).fetchone()
        if row is None or trial.strategy_id != str(row[0]):
            raise StrategyLabError("campaign trial experiment identity mismatch")
        identity = ExperimentIdentity(
            strategy_id=str(row[0]),
            data_source_id=str(row[1]),
            policy_id=str(row[2]),
            engine_id=str(row[3]),
            runtime_id=str(row[4]),
        )
        actual = self.existing_execution(identity, screen_policy)
        if actual is None:
            raise StrategyLabError("campaign trial references non-terminal execution")
        if trial.candidate_id != actual.candidate_id:
            raise StrategyLabError("campaign trial candidate evidence mismatch")
        if trial.evidence.terminal_status is TerminalStatus.DUPLICATE_SUPPRESSED:
            if trial.evidence.execution_started:
                raise StrategyLabError("duplicate trial cannot start execution")
            return
        campaign_drift = (
            trial.evidence.terminal_status is TerminalStatus.TECHNICAL_INVALID
            and bool(trial.evidence.reason_codes)
            and trial.evidence.reason_codes[0] == "CAMPAIGN_PREFLIGHT_DRIFT"
            and set(trial.evidence.reason_codes[1:])
            <= {"DUPLICATE_CONTENT_ID", "REUSED_EXECUTION"}
        )
        if campaign_drift:
            reused = any(
                reason in {"DUPLICATE_CONTENT_ID", "REUSED_EXECUTION"}
                for reason in trial.evidence.reason_codes[1:]
            )
            if (reused and trial.evidence.execution_started) or (
                not reused and trial.evidence.execution_started != actual.execution_started
            ):
                raise StrategyLabError("campaign trial execution_started mismatch")
            return
        if trial.evidence.terminal_status is not actual.terminal_status:
            raise StrategyLabError("campaign trial terminal status mismatch")
        stored_reasons = tuple(
            reason
            for reason in trial.evidence.reason_codes
            if reason != "REUSED_EXECUTION"
        )
        if stored_reasons != actual.reason_codes:
            raise StrategyLabError("campaign trial reason evidence mismatch")
        reused = "REUSED_EXECUTION" in trial.evidence.reason_codes
        if (reused and trial.evidence.execution_started) or (
            not reused and trial.evidence.execution_started != actual.execution_started
        ):
            raise StrategyLabError("campaign trial execution_started mismatch")

    def existing_experiment_id(self, identity: ExperimentIdentity) -> str | None:
        """Return the exact immutable experiment identity, including non-terminal rows."""
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute(
                """SELECT experiment_id FROM experiments
                WHERE strategy_id = ? AND data_source_id = ? AND policy_id = ?
                  AND engine_id = ? AND runtime_id = ?""",
                (
                    identity.strategy_id,
                    identity.data_source_id,
                    identity.policy_id,
                    identity.engine_id,
                    identity.runtime_id,
                ),
            ).fetchone()
        if row is None:
            return None
        experiment_id = str(row[0])
        if experiment_id != _experiment_id(identity):
            raise StrategyLabError("stored experiment identity is inconsistent")
        return experiment_id

    def existing_execution(
        self,
        identity: ExperimentIdentity,
        screen_policy: ScreenPolicy | None = None,
        *,
        hypothesis: StrategyHypothesis | None = None,
        prepared: _PreparedExecution | None = None,
    ) -> TrialEvidence | None:
        """Return terminal evidence for the exact existing execution identity."""
        with closing(sqlite3.connect(self.path)) as connection:
            experiment = connection.execute(
                """SELECT experiment_id FROM experiments
                WHERE strategy_id = ? AND data_source_id = ? AND policy_id = ?
                  AND engine_id = ? AND runtime_id = ?""",
                (
                    identity.strategy_id,
                    identity.data_source_id,
                    identity.policy_id,
                    identity.engine_id,
                    identity.runtime_id,
                ),
            ).fetchone()
            if experiment is None:
                return None
            experiment_id = str(experiment[0])
            verdict = connection.execute(
                """SELECT verdict_id, outcome, reason_code, artifact_path, artifact_sha256
                FROM verdicts WHERE experiment_id = ?""",
                (experiment_id,),
            ).fetchone()
            screen = connection.execute(
                """SELECT outcome, reason_code, artifact_path, artifact_sha256
                FROM stage_results
                WHERE experiment_id = ? AND stage = 'Research screened'""",
                (experiment_id,),
            ).fetchone()
            error = connection.execute(
                """SELECT stage, reason_code, artifact_path, artifact_sha256
                FROM errors WHERE experiment_id = ?
                ORDER BY error_id LIMIT 1""",
                (experiment_id,),
            ).fetchone()
            pybroker_artifact = connection.execute(
                """SELECT outcome, reason_code, artifact_path FROM stage_results
                WHERE experiment_id = ? AND stage = 'PyBroker completed'""",
                (experiment_id,),
            ).fetchone()
            parity_rows = connection.execute(
                """SELECT candidate_id, evaluation_context_id, data_snapshot_id,
                outcome, reason_code, required_action, artifact_path, artifact_sha256
                FROM signal_parity_results WHERE experiment_id = ?""",
                (experiment_id,),
            ).fetchall()
        self.require_terminal_consistency(experiment_id)
        if prepared is not None and prepared.identity != identity:
            raise StrategyLabError("cached execution prepared identity mismatch")
        if hypothesis is not None and hypothesis.strategy_id != identity.strategy_id:
            raise StrategyLabError("cached execution hypothesis mismatch")
        self.verify_experiment_artifacts(experiment_id)
        candidate_id: str | None = None
        candidate_document: dict[str, JsonValue] | None = None
        research_result = None
        if pybroker_artifact is not None and pybroker_artifact[0] == "PASSED":
            raw_path = Path(str(pybroker_artifact[2]))
            research_result = load_research_result_v2(raw_path.read_bytes())
            candidate_path = raw_path.with_name("candidate.json")
            _verified_artifact(candidate_path, research_result.candidate_id)
            candidate, candidate_id = load_pybroker_candidate(candidate_path)
            candidate_document = candidate
            if candidate_id != research_result.candidate_id:
                raise StrategyLabError("cached research candidate_id mismatch")
            signals = candidate.get("signals")
            if (
                not isinstance(signals, list)
                or research_result.metrics.signal_count != len(signals)
            ):
                raise StrategyLabError("cached research signal_count mismatch")
            if hypothesis is not None and prepared is not None:
                _candidate_matches_hypothesis(
                    candidate_document,
                    hypothesis,
                    prepared.evaluation_context_id,
                    prepared.data_as_of_ns,
                    prepared.runtime_id,
                )
        if len(parity_rows) > 1:
            raise StrategyLabError("cached execution has ambiguous signal parity evidence")
        parity = parity_rows[0] if parity_rows else None
        if parity is not None:
            if candidate_document is None or candidate_id is None:
                raise StrategyLabError("cached signal parity has no validated candidate")
            source = candidate_document.get("source")
            evaluation_context_id = candidate_document.get("evaluation_context_id")
            if (
                not isinstance(source, dict)
                or parity[0] != candidate_id
                or parity[1] != evaluation_context_id
                or parity[2] != source.get("data_snapshot_id")
            ):
                raise StrategyLabError("cached signal parity evidence is inconsistent")
            try:
                parity_payload = _verified_artifact(Path(str(parity[6])), str(parity[7]))
                loaded_parity = load_signal_parity_result(
                    parity_payload,
                    candidate_id=candidate_id,
                    candidate_signal_count=len(signals),
                    recomputed_decisions=candidate_signal_decisions(candidate_document),
                    artifact_sha256=str(parity[7]),
                )
            except (CandidateBacktestError, TypeError, ValueError) as error:
                raise StrategyLabError("cached signal parity evidence is inconsistent") from error
            if (
                loaded_parity.outcome != parity[3]
                or loaded_parity.reason_code != parity[4]
                or loaded_parity.required_action != parity[5]
            ):
                raise StrategyLabError("cached signal parity evidence is inconsistent")
        if error is not None:
            error_document = _load_canonical_json(Path(str(error[2])))
            if (
                not isinstance(error_document, dict)
                or error_document.get("schema_version") != "strategy-loop-error-v1"
                or error_document.get("experiment_id") != experiment_id
                or error_document.get("stage") != error[0]
                or error_document.get("reason_code") != error[1]
            ):
                raise StrategyLabError("cached error artifact does not match error record")
        if screen is not None and screen[0] == "ERROR":
            if (
                verdict is not None
                or error is None
                or error[0] != "RESEARCH"
                or screen[1] != error[1]
                or screen[2] != error[2]
                or screen[3] != error[3]
            ):
                raise StrategyLabError("cached research error evidence is inconsistent")
        elif screen is not None:
            if screen_policy is None or research_result is None:
                raise StrategyLabError("cached screen artifact requires frozen policy and result")
            try:
                screen_document = load_screen_result_v1(Path(str(screen[2])).read_bytes())
            except (OSError, StrategyCampaignError) as error:
                raise StrategyLabError(
                    "cached screen artifact does not match frozen policy",
                ) from error
            decision = screen_research_result(research_result, screen_policy)
            expected_document = screened_result_document(
                research_result,
                decision,
                screen_policy,
            )
            expected_outcome = (
                "REJECTED" if decision.outcome == "SCREEN_REJECTED" else "PASSED"
            )
            expected_reason = (
                decision.reason_codes[0]
                if decision.reason_codes
                else "SCREEN_PASSED"
            )
            if (
                screen_document != expected_document
                or screen[0] != expected_outcome
                or screen[1] != expected_reason
            ):
                raise StrategyLabError("cached screen artifact does not match frozen policy")
        started = pybroker_artifact is not None and not (
            pybroker_artifact[0] == "ERROR"
            and pybroker_artifact[1] == "PYBROKER_PROCESS_LAUNCH_FAILED"
        )
        requires_pass_parity = (
            verdict is not None
            or (screen is not None and screen[0] == "PASSED")
            or (error is not None and error[0] == "NAUTILUS")
        )
        if requires_pass_parity and (
            parity is None or parity[3] != "PASS" or parity[5] is not None
        ):
            raise StrategyLabError("cached terminal execution has no PASS parity evidence")
        if screen is not None and screen[0] == "REJECTED" and parity is not None:
            raise StrategyLabError("cached rejected screen has unexpected parity evidence")
        if verdict is not None:
            if error is not None or candidate_id is None or screen is None or screen[0] != "PASSED":
                raise StrategyLabError("cached verdict has no validated candidate")
            record = VerdictRecord(
                experiment_id,
                str(verdict[1]),
                str(verdict[2]),
                str(verdict[3]),
                str(verdict[4]),
            )
            if verdict[0] != _verdict_record_id(record):
                raise StrategyLabError("cached Nautilus verdict identity is inconsistent")
            self.validated_verdict_document(record, hypothesis=hypothesis, prepared=prepared)
            return TrialEvidence(
                TerminalStatus.SURVIVED,
                started,
                ("SCREEN_PASSED",),
                experiment_id,
                candidate_id,
            )
        if screen is not None and screen[0] == "REJECTED":
            assert research_result is not None
            decision = screen_research_result(research_result, screen_policy)
            return TrialEvidence(
                TerminalStatus.SCREEN_REJECTED,
                started,
                decision.reason_codes,
                experiment_id,
                candidate_id,
            )
        if error is not None:
            return TrialEvidence(
                TerminalStatus.TECHNICAL_INVALID,
                started,
                (str(error[1]),),
                experiment_id,
                candidate_id,
            )
        return None

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
        self.validated_verdict_document(record)
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

    def validated_verdict_document(
        self,
        record: VerdictRecord,
        *,
        hypothesis: StrategyHypothesis | None = None,
        prepared: _PreparedExecution | None = None,
    ) -> dict[str, JsonValue] | None:
        """Validate the complete V2 screen→parity→Nautilus terminal chain."""
        artifact_path = Path(_identifier(record.artifact_path, "artifact_path"))
        artifact_hash = _content_id(record.artifact_sha256, "artifact_sha256")
        _verified_artifact(artifact_path, artifact_hash)
        with closing(sqlite3.connect(self.path)) as connection:
            experiment = connection.execute(
                """SELECT experiments.hypothesis_id, experiments.strategy_id,
                experiments.data_source_id, experiments.policy_id, experiments.engine_id,
                experiments.runtime_id, hypotheses.artifact_path, hypotheses.artifact_sha256,
                strategies.identity_schema
                FROM experiments
                JOIN hypotheses USING (hypothesis_id, strategy_id)
                JOIN strategies USING (strategy_id)
                WHERE experiments.experiment_id = ?""",
                (_content_id(record.experiment_id, "experiment_id"),),
            ).fetchone()
            if experiment is None:
                raise StrategyLabError("verdict experiment does not exist")
            if experiment[8] != "strategy-id-v2":
                return None
            nautilus = connection.execute(
                """SELECT outcome, reason_code, artifact_path, artifact_sha256
                FROM stage_results
                WHERE experiment_id = ? AND stage = 'Nautilus replayed'""",
                (record.experiment_id,),
            ).fetchone()
            pybroker = connection.execute(
                """SELECT outcome, reason_code, artifact_path, artifact_sha256
                FROM stage_results
                WHERE experiment_id = ? AND stage = 'PyBroker completed'""",
                (record.experiment_id,),
            ).fetchone()
            screen = connection.execute(
                """SELECT outcome, reason_code, artifact_path, artifact_sha256
                FROM stage_results
                WHERE experiment_id = ? AND stage = 'Research screened'""",
                (record.experiment_id,),
            ).fetchone()
            parity_rows = connection.execute(
                """SELECT candidate_id, evaluation_context_id, data_snapshot_id,
                outcome, reason_code, required_action, artifact_path, artifact_sha256
                FROM signal_parity_results WHERE experiment_id = ?""",
                (record.experiment_id,),
            ).fetchall()
        if nautilus is None:
            raise StrategyLabError("Nautilus replay stage is missing for verdict")
        if nautilus != (
            "PASSED",
            "NAUTILUS_REPLAY_COMPLETED",
            record.artifact_path,
            record.artifact_sha256,
        ):
            raise StrategyLabError("Nautilus replay stage does not match verdict")
        if pybroker is None or pybroker[0:2] != ("PASSED", "PYBROKER_COMPLETED"):
            raise StrategyLabError("Nautilus verdict has no passed PyBroker stage")
        if screen is None or screen[0] != "PASSED":
            raise StrategyLabError("Nautilus verdict has no passed research screen")
        if len(parity_rows) != 1:
            raise StrategyLabError("Nautilus verdict has ambiguous signal parity evidence")
        parity = parity_rows[0]
        if parity[3:6] != ("PASS", "SIGNAL_PARITY_MATCH", None):
            raise StrategyLabError("Nautilus verdict has no PASS parity evidence")

        _verified_artifact(Path(str(experiment[6])), str(experiment[7]))
        stored_hypothesis = load_strategy_hypothesis(Path(str(experiment[6])))
        if (
            stored_hypothesis.hypothesis_id != experiment[0]
            or stored_hypothesis.strategy_id != experiment[1]
            or stored_hypothesis.identity_schema != "strategy-id-v2"
        ):
            raise StrategyLabError("Nautilus verdict hypothesis identity is inconsistent")
        if hypothesis is not None and (
            hypothesis.hypothesis_id != stored_hypothesis.hypothesis_id
            or hypothesis.strategy_id != stored_hypothesis.strategy_id
        ):
            raise StrategyLabError("Nautilus verdict hypothesis does not match caller")
        identity = ExperimentIdentity(
            str(experiment[1]),
            str(experiment[2]),
            str(experiment[3]),
            str(experiment[4]),
            str(experiment[5]),
        )
        if prepared is not None and prepared.identity != identity:
            raise StrategyLabError("Nautilus verdict prepared identity mismatch")

        raw_path = Path(str(pybroker[2]))
        _verified_artifact(raw_path, str(pybroker[3]))
        try:
            research_result = load_research_result_v2(raw_path.read_bytes())
        except (OSError, StrategyCampaignError) as error:
            raise StrategyLabError("Nautilus verdict research result is invalid") from error
        candidate_path = raw_path.with_name("candidate.json")
        _verified_artifact(candidate_path, research_result.candidate_id)
        candidate, candidate_id = load_pybroker_candidate(candidate_path)
        if candidate_id != research_result.candidate_id or candidate_id != parity[0]:
            raise StrategyLabError("Nautilus verdict candidate identity is inconsistent")
        candidate_source = candidate.get("source")
        candidate_runtime = candidate.get("runtime")
        if not isinstance(candidate_source, dict) or not isinstance(candidate_runtime, dict):
            raise StrategyLabError("Nautilus verdict candidate evidence is invalid")
        if (
            candidate.get("evaluation_context_id") != parity[1]
            or candidate_source.get("data_snapshot_id") != parity[2]
        ):
            raise StrategyLabError("Nautilus verdict candidate context is inconsistent")
        if prepared is not None:
            _candidate_matches_hypothesis(
                candidate,
                stored_hypothesis,
                prepared.evaluation_context_id,
                prepared.data_as_of_ns,
                prepared.runtime_id,
            )
        try:
            candidate_signals = candidate.get("signals")
            if not isinstance(candidate_signals, list):
                raise CandidateBacktestError("candidate signals are invalid")
            parity_payload = _verified_artifact(Path(str(parity[6])), str(parity[7]))
            loaded_parity = load_signal_parity_result(
                parity_payload,
                candidate_id=candidate_id,
                candidate_signal_count=len(candidate_signals),
                recomputed_decisions=candidate_signal_decisions(candidate),
                artifact_sha256=str(parity[7]),
            )
        except (CandidateBacktestError, TypeError, ValueError) as error:
            raise StrategyLabError("Nautilus verdict signal parity artifact is invalid") from error
        if (
            loaded_parity.outcome != parity[3]
            or loaded_parity.reason_code != parity[4]
            or loaded_parity.required_action != parity[5]
        ):
            raise StrategyLabError("Nautilus verdict signal parity evidence is inconsistent")

        try:
            document = load_candidate_backtest_verdict(artifact_path.read_bytes())
        except (OSError, CandidateBacktestError) as error:
            raise StrategyLabError("Nautilus verdict artifact is invalid") from error
        reason_codes = document["reason_codes"]
        source = document["source"]
        versions = document["runtime_versions"]
        signal_parity = document.get("signal_parity")
        expected_outcome = (
            "SUCCESS" if document["decision"] == "RETAIN_FOR_RESEARCH" else "REJECTION"
        )
        if (
            document["experiment_id"] != record.experiment_id
            or document["hypothesis_id"] != stored_hypothesis.hypothesis_id
            or document["strategy_id"] != stored_hypothesis.strategy_id
            or document["candidate_id"] != candidate_id
            or expected_outcome != record.outcome
            or not isinstance(reason_codes, list)
            or reason_codes[0] != record.reason_code
            or not isinstance(source, dict)
            or source
            != {
                "first_ts_event_ns": candidate_source.get("first_ts_event_ns"),
                "last_ts_event_ns": candidate_source.get("last_ts_event_ns"),
                "row_count": candidate_source.get("row_count"),
                "sha256": candidate_source.get("sha256"),
            }
            or not isinstance(versions, dict)
            or versions.get("pybroker") != candidate_runtime.get("pybroker_version")
            or versions.get("research_python") != candidate_runtime.get("python_version")
            or versions.get("nautilus_python") != platform.python_version()
            or signal_parity
            != {
                "artifact_sha256": parity[7],
                "outcome": parity[3],
                "reason_code": parity[4],
            }
        ):
            raise StrategyLabError("Nautilus verdict artifact identity is inconsistent")
        if prepared is not None and document["code_commit"] != prepared.code_commit:
            raise StrategyLabError("Nautilus verdict code commit is inconsistent")
        return document

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
        artifact_hash = _content_id(record.artifact_sha256, "artifact_sha256")
        payload = _verified_artifact(
            artifact_path,
            artifact_hash,
        )
        try:
            loaded = load_signal_parity_result(
                payload,
                candidate_id=record.candidate_id,
                candidate_signal_count=len(record.decisions),
                recomputed_decisions=record.decisions,
                artifact_sha256=artifact_hash,
            )
        except CandidateBacktestError as error:
            raise StrategyLabError("signal parity artifact does not match record") from error
        if (
            loaded.outcome != record.outcome
            or loaded.reason_code != record.reason_code
            or loaded.required_action != record.required_action
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

    def require_terminal_consistency(self, experiment_id: str | None = None) -> None:
        """Fail closed on contradictory or duplicated legacy terminal evidence."""
        parameters: tuple[str, ...] = ()
        predicate = ""
        if experiment_id is not None:
            predicate = " WHERE experiment_id = ?"
            parameters = (_content_id(experiment_id, "experiment_id"),)
        with closing(sqlite3.connect(self.path)) as connection:
            duplicate_error = connection.execute(
                """SELECT experiment_id FROM errors"""
                + predicate
                + " GROUP BY experiment_id HAVING COUNT(*) > 1 LIMIT 1",
                parameters,
            ).fetchone()
            if duplicate_error is not None:
                raise StrategyLabError("duplicate terminal error evidence")
            contradiction = connection.execute(
                """SELECT verdicts.experiment_id
                FROM verdicts JOIN errors USING (experiment_id)"""
                + (" WHERE verdicts.experiment_id = ?" if experiment_id is not None else "")
                + " LIMIT 1",
                parameters,
            ).fetchone()
        if contradiction is not None:
            raise StrategyLabError("contradictory terminal evidence")

    def verify_experiment_artifacts(self, experiment_id: str) -> None:
        self.require_terminal_consistency(experiment_id)
        with closing(sqlite3.connect(self.path)) as connection:
            rows = connection.execute(
                """SELECT artifact_path, artifact_sha256 FROM stage_results
                WHERE experiment_id = ?
                UNION ALL
                SELECT artifact_path, artifact_sha256 FROM signal_parity_results
                WHERE experiment_id = ?
                UNION ALL
                SELECT artifact_path, artifact_sha256 FROM verdicts
                WHERE experiment_id = ?
                UNION ALL
                SELECT artifact_path, artifact_sha256 FROM errors
                WHERE experiment_id = ?""",
                (experiment_id, experiment_id, experiment_id, experiment_id),
            ).fetchall()
        for artifact_path, artifact_hash in rows:
            _verified_artifact(Path(artifact_path), artifact_hash)

    def funnel_counts(self) -> FunnelCounts:
        self.require_terminal_consistency()
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


def _runtime_identity() -> str:
    try:
        return research_runtime_identity(_REPO_ROOT)
    except (OSError, ValueError) as error:
        raise StrategyLabError(str(error)) from error


def _engine_identity() -> str:
    root_python = platform.python_version()
    digest = sha256(f"{nautilus_trader.__version__}\0{root_python}".encode())
    for path in (
        _REPO_ROOT / "research/pybroker_research.py",
        _REPO_ROOT / "src/nautilus_quant/candidate_backtest.py",
        _REPO_ROOT / "src/nautilus_quant/funding_observation.py",
        _REPO_ROOT / "src/nautilus_quant/pybroker_candidate.py",
        _REPO_ROOT / "src/nautilus_quant/runtime_attestation.py",
        _REPO_ROOT / "src/nautilus_quant/strategy_campaign.py",
        _REPO_ROOT / "src/nautilus_quant/strategy_families.py",
        _REPO_ROOT / "src/nautilus_quant/strategy_lab.py",
    ):
        digest.update(path.relative_to(_REPO_ROOT).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return f"nautilus-{nautilus_trader.__version__}-python-{root_python}-{digest.hexdigest()}"


def _evaluation_context_id(
    hypothesis: StrategyHypothesis,
    *,
    data_source_id: str,
    policy_id: str,
    engine_id: str,
    runtime_id: str,
    code_commit: str,
    screen_policy_id: str | None = None,
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
                "screen_policy_id": _content_id(
                    screen_policy_id or policy_id,
                    "screen_policy_id",
                ),
                "strategy_id": hypothesis.strategy_id,
            },
        ),
    ).hexdigest()


def _prepare_execution(
    hypothesis: StrategyHypothesis,
    paths: StrategyLoopPaths,
) -> _PreparedExecution:
    """Derive the exact execution identity shared by run and campaign reuse."""
    data_as_of_ns: int | None = None
    if hypothesis.identity_schema == "strategy-id-v2":
        data_source_id, data_as_of_ns = _stable_data_snapshot(paths, _BAR_TYPE)
    else:
        data_source_id = _hash_tree(paths)
    accounting_policy_id, _policy_version = _policy_identity(paths.policy_path)
    screen_policy: ScreenPolicy | None = None
    policy_id = accounting_policy_id
    if hypothesis.identity_schema == "strategy-id-v2":
        research_policy_path = paths.research_policy_path or (
            _REPO_ROOT / "config/strategy_research_policy.json"
        )
        screen_policy = load_screen_policy(research_policy_path)
        policy_id = sha256(
            _canonical_json(
                {
                    "accounting_policy_id": accounting_policy_id,
                    "screen_policy_id": screen_policy.policy_id,
                },
            ),
        ).hexdigest()
    base_engine_id = _engine_identity()
    runtime_id = _runtime_identity()
    code_commit = _code_commit()
    evaluation_context_id: str | None = None
    engine_id = base_engine_id
    if hypothesis.identity_schema == "strategy-id-v2":
        evaluation_context_id = _evaluation_context_id(
            hypothesis,
            data_source_id=data_source_id,
            policy_id=policy_id,
            engine_id=base_engine_id,
            runtime_id=runtime_id,
            code_commit=code_commit,
            screen_policy_id=screen_policy.policy_id if screen_policy is not None else None,
        )
        engine_id = f"{base_engine_id}-evaluation-{evaluation_context_id}"
    return _PreparedExecution(
        identity=ExperimentIdentity(
            strategy_id=hypothesis.strategy_id,
            data_source_id=data_source_id,
            policy_id=policy_id,
            engine_id=engine_id,
            runtime_id=runtime_id,
        ),
        evaluation_context_id=evaluation_context_id,
        screen_policy=screen_policy,
        data_as_of_ns=data_as_of_ns,
        runtime_id=runtime_id,
        code_commit=code_commit,
        base_engine_id=base_engine_id,
    )


def _require_execution_snapshot(
    hypothesis: StrategyHypothesis,
    paths: StrategyLoopPaths,
    prepared: _PreparedExecution,
) -> None:
    if _prepare_execution(hypothesis, paths) != prepared:
        raise StrategyLabError("execution data or runtime snapshot changed")


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
    prepared: _PreparedExecution | None = None,
) -> dict[str, JsonValue] | None:
    ledger.require_terminal_consistency(experiment_id)
    if hypothesis.identity_schema == "strategy-id-v2" and prepared is not None:
        ledger.existing_execution(
            prepared.identity,
            prepared.screen_policy,
            hypothesis=hypothesis,
            prepared=prepared,
        )
    with closing(sqlite3.connect(ledger.path)) as connection:
        verdict = connection.execute(
            """SELECT verdict_id, outcome, reason_code, artifact_path, artifact_sha256
            FROM verdicts WHERE experiment_id = ?""",
            (experiment_id,),
        ).fetchone()
        error = connection.execute(
            """SELECT error_id, stage, reason_code, artifact_path, artifact_sha256
            FROM errors WHERE experiment_id = ? ORDER BY error_id LIMIT 1""",
            (experiment_id,),
        ).fetchone()
        screen = connection.execute(
            """SELECT outcome, artifact_path, artifact_sha256
            FROM stage_results
            WHERE experiment_id = ? AND stage = 'Research screened'
            AND outcome = 'REJECTED'""",
            (experiment_id,),
        ).fetchone()
    if verdict is not None and error is not None:
        raise StrategyLabError("contradictory terminal evidence")
    if verdict is None and error is None and screen is None:
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
        verdict_id, outcome, reason_code, artifact_path, artifact_hash = verdict
        _verified_artifact(Path(artifact_path), artifact_hash)
        record = VerdictRecord(
            experiment_id,
            outcome,
            reason_code,
            artifact_path,
            artifact_hash,
        )
        if verdict_id != _verdict_record_id(record):
            raise StrategyLabError("cached Nautilus verdict identity is inconsistent")
        verdict_document = ledger.validated_verdict_document(
            record,
            hypothesis=hypothesis,
            prepared=prepared,
        )
        if verdict_document is None:
            value = _load_canonical_json(Path(artifact_path))
            if not isinstance(value, dict):
                raise StrategyLabError("cached verdict artifact must be an object")
            verdict_document = value
        decision = verdict_document.get("decision")
        reason_codes = verdict_document.get("reason_codes")
        if decision not in {"REVISE", "RETAIN_FOR_RESEARCH"} or (
            not isinstance(reason_codes, list)
            or not reason_codes
            or not all(isinstance(reason, str) and reason for reason in reason_codes)
        ):
            raise StrategyLabError("cached Nautilus verdict decision is invalid")
        expected: dict[str, JsonValue] = {
            **base,
            "decision": decision,
            "error_id": None,
            "failed_stage": None,
            "reason_codes": reason_codes,
            "status": "EVALUATED",
            "verdict_id": verdict_id,
        }
    elif error is not None:
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
    else:
        _outcome, artifact_path, artifact_hash = screen
        _verified_artifact(Path(artifact_path), artifact_hash)
        screen_document = _load_canonical_json(Path(artifact_path))
        if not isinstance(screen_document, dict):
            raise StrategyLabError("cached screen artifact must be an object")
        reason_codes = screen_document.get("screen_reason_codes")
        if not isinstance(reason_codes, list) or not all(
            isinstance(reason, str) and reason for reason in reason_codes
        ):
            raise StrategyLabError("cached screen reason codes are invalid")
        expected = {
            **base,
            "error_id": None,
            "failed_stage": "RESEARCH",
            "reason_codes": reason_codes,
            "status": "SCREEN_REJECTED",
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


_PROCESS_CLEANUP_TIMEOUT_SECONDS: Final = 1.0


def _sanitized_process_environment() -> dict[str, str]:
    """Return the only environment a research child is allowed to inherit."""
    return {
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": os.defpath,
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "PYTHONUNBUFFERED": "1",
    }


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    """Terminate, kill, and boundedly reap one process group."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError, TypeError):
        if process.poll() is None:
            try:
                process.terminate()
            except (OSError, ProcessLookupError):
                pass
    try:
        process.wait(timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS / 2)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError, TypeError):
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass
    try:
        process.wait(timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS / 2)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass
        try:
            process.wait(timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS / 2)
        except subprocess.TimeoutExpired:
            pass


def _run_bounded_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: int,
) -> _ProcessResult:
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=_sanitized_process_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    executor: ThreadPoolExecutor | None = None
    stdout_future = None
    stderr_future = None
    stdout: bytes = b""
    stderr: bytes = b""
    stdout_truncated = False
    stderr_truncated = False
    timed_out = False
    drained = False
    try:
        if process.stdout is None or process.stderr is None:
            raise StrategyLabError("PyBroker process pipes were not created")
        executor = ThreadPoolExecutor(max_workers=2)
        stdout_future = executor.submit(_drain_bounded, process.stdout)
        stderr_future = executor.submit(_drain_bounded, process.stderr)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process_group(process)
        try:
            stdout, stdout_truncated = stdout_future.result(
                timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS,
            )
            stderr, stderr_truncated = stderr_future.result(
                timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS,
            )
        except FuturesTimeoutError:
            _terminate_process_group(process)
            stdout, stdout_truncated = stdout_future.result(
                timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS,
            )
            stderr, stderr_truncated = stderr_future.result(
                timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS,
            )
        drained = True
        return _ProcessResult(
            process.returncode if process.returncode is not None else -1,
            stdout,
            stderr,
            stdout_truncated,
            stderr_truncated,
            timed_out,
        )
    except BaseException:
        if process is not None:
            _terminate_process_group(process)
        raise
    finally:
        if process is not None and not drained:
            _terminate_process_group(process)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except BaseException:
                    pass
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)


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
        frozenset({"candidate_id", "provisional_metrics"})
        if not isinstance(value, dict) or value.get("schema_version") != "research-result-v2"
        else frozenset({"candidate_id", "provisional_metrics", "schema_version", "truth_status"}),
        "research result",
    )
    if root.get("schema_version") == "research-result-v2":
        load_research_result_v2(payload)
        return root
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
    data_as_of_ns: int | None,
    runtime_id: str,
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
        source = candidate.get("source")
        runtime = candidate.get("runtime")
        mismatch = (
            candidate.get("schema_version") != "pybroker-candidate-v2"
            or candidate.get("evaluation_context_id") != evaluation_context_id
            or not isinstance(source, dict)
            or source.get("data_as_of_ns") != data_as_of_ns
            or not isinstance(runtime, dict)
            or runtime.get("environment_id") != runtime_id
            or strategy.get("family_id") != hypothesis.family_id
            or strategy.get("family_version") != hypothesis.family_version
            or strategy.get("kernel_version") != KERNEL_VERSION
            or strategy.get("kernel_hash") != KERNEL_HASH
            or common_mismatch
        )
    if mismatch:
        raise StrategyLabError("candidate does not match immutable hypothesis")


def _catalog_last_timestamp(catalog_path: Path, bar_type: str) -> int:
    """Read the actual tail of one requested bar cohort before execution."""
    catalog = ParquetDataCatalog(str(catalog_path))
    bars = catalog.query_bars([bar_type])
    timestamps = [bar.ts_event for bar in bars]
    if not timestamps or timestamps != sorted(set(timestamps)):
        raise StrategyLabError("campaign bar cohort is empty or not strictly ordered")
    return int(timestamps[-1])


def _stable_data_snapshot(paths: StrategyLoopPaths, bar_type: str) -> tuple[str, int]:
    """Bind cohort hash and tail to one stable read window."""
    before = _hash_tree(paths)
    data_as_of_ns = _catalog_last_timestamp(paths.catalog_path, bar_type)
    after = _hash_tree(paths)
    if before != after:
        raise StrategyLabError("campaign data snapshot changed during preflight")
    return before, data_as_of_ns


def _campaign_hypothesis_document(
    spec: CampaignSpec,
    attempt: CampaignAttempt,
) -> dict[str, JsonValue]:
    """Build a canonical generated V2 hypothesis from the tracked family registry."""
    if attempt.strategy_id is None:
        raise CampaignTechnicalError("INVALID_FAMILY_OR_PARAMETERS", False)
    try:
        definition = DEFAULT_REGISTRY.resolve(spec.family_id, spec.family_version)
    except FamilyKernelError as error:
        raise CampaignTechnicalError("INVALID_FAMILY_OR_PARAMETERS", False) from error
    if not definition.thesis or not definition.falsification:
        raise CampaignTechnicalError("FAMILY_RATIONALE_MISSING", False)
    document: dict[str, JsonValue] = {
        "bar_type": spec.approved_bar_types[0],
        "based_on_verdict_id": None,
        "falsification": definition.falsification,
        "family_version": spec.family_version,
        "instrument_id": spec.approved_instruments[0],
        "parameters": attempt.parameters,
        "parent_strategy_id": None,
        "schema_version": "strategy-hypothesis-v2",
        "strategy_family": spec.family_id,
        "thesis": definition.thesis,
    }
    return document


def _with_campaign_hypothesis(
    spec: CampaignSpec,
    attempt: CampaignAttempt,
    callback: Callable[[Path, StrategyHypothesis], TrialEvidence],
) -> TrialEvidence:
    """Run a callback with one isolated generated hypothesis artifact."""
    document = _campaign_hypothesis_document(spec, attempt)
    with TemporaryDirectory(prefix="strategy-campaign-input-") as temporary:
        hypothesis_path = Path(temporary) / "hypothesis.json"
        _atomic_publish(hypothesis_path, _canonical_json(document))
        hypothesis = load_strategy_hypothesis(hypothesis_path)
        if hypothesis.strategy_id != attempt.strategy_id:
            raise CampaignTechnicalError("STRATEGY_ID_MISMATCH", False)
        return callback(hypothesis_path, hypothesis)


def _campaign_reference_execution(
    spec: CampaignSpec,
    paths: StrategyLoopPaths,
    attempt: CampaignAttempt,
) -> tuple[StrategyHypothesis, _PreparedExecution]:
    document = _campaign_hypothesis_document(spec, attempt)
    with TemporaryDirectory(prefix="strategy-campaign-reference-") as temporary:
        hypothesis_path = Path(temporary) / "hypothesis.json"
        _atomic_publish(hypothesis_path, _canonical_json(document))
        hypothesis = load_strategy_hypothesis(hypothesis_path)
        return hypothesis, _prepare_execution(hypothesis, paths)


def _campaign_execution_signature(prepared: _PreparedExecution) -> tuple[object, ...]:
    return (
        prepared.identity.data_source_id,
        prepared.identity.policy_id,
        prepared.identity.runtime_id,
        prepared.data_as_of_ns,
        prepared.runtime_id,
        prepared.code_commit,
        prepared.base_engine_id,
        prepared.screen_policy.policy_id if prepared.screen_policy is not None else None,
    )


def _require_campaign_preflight(
    prepared: _PreparedExecution,
    preflight: CampaignPreflight,
    reference: _PreparedExecution | None = None,
) -> None:
    if (
        prepared.identity.data_source_id != preflight.data_source_id
        or prepared.data_as_of_ns != preflight.data_as_of_ns
        or prepared.screen_policy is None
        or prepared.screen_policy.policy_id != preflight.screen_policy_id
        or (
            reference is not None
            and _campaign_execution_signature(prepared)
            != _campaign_execution_signature(reference)
        )
    ):
        raise CampaignTechnicalError("CAMPAIGN_PREFLIGHT_DRIFT", False)


def _reconcile_campaign_preflight(
    paths: StrategyLoopPaths,
    research_policy_path: Path,
    preflight: CampaignPreflight,
    reference_hypothesis: StrategyHypothesis | None = None,
    reference_prepared: _PreparedExecution | None = None,
) -> None:
    """Re-read campaign-wide identities before publishing its immutable census."""
    try:
        if reference_hypothesis is not None and reference_prepared is not None:
            _require_execution_snapshot(reference_hypothesis, paths, reference_prepared)
        policy = load_screen_policy(research_policy_path)
        data_source_id, data_as_of_ns = _stable_data_snapshot(paths, _BAR_TYPE)
    except (OSError, RuntimeError, StrategyCampaignError, StrategyLabError, ValueError) as error:
        raise CampaignTechnicalError("CAMPAIGN_PREFLIGHT_DRIFT", False) from error
    if (
        policy.policy_id != preflight.screen_policy_id
        or data_source_id != preflight.data_source_id
        or data_as_of_ns != preflight.data_as_of_ns
    ):
        raise CampaignTechnicalError("CAMPAIGN_PREFLIGHT_DRIFT", False)


def _campaign_reuse_lookup(
    spec: CampaignSpec,
    paths: StrategyLoopPaths,
    ledger: StrategyLedger,
    preflight: CampaignPreflight,
    attempt: CampaignAttempt,
    reference: _PreparedExecution | None = None,
) -> TrialEvidence | None:
    """Find only a terminal experiment with exactly matching execution semantics."""
    def lookup(_path: Path, hypothesis: StrategyHypothesis) -> TrialEvidence:
        initial = _prepare_execution(hypothesis, paths)
        experiment_id = _experiment_id(initial.identity)
        lock_directory = paths.state_path / "experiment-locks"
        lock_directory.mkdir(parents=True, exist_ok=True)
        with (lock_directory / f"{experiment_id}.lock").open("a+b") as experiment_lock:
            fcntl.flock(experiment_lock, fcntl.LOCK_EX)
            prepared = _prepare_execution(hypothesis, paths)
            if prepared != initial:
                raise CampaignTechnicalError("CAMPAIGN_PREFLIGHT_DRIFT", False)
            _require_campaign_preflight(prepared, preflight, reference)
            prior = ledger.existing_execution(
                prepared.identity,
                prepared.screen_policy,
                hypothesis=hypothesis,
                prepared=prepared,
            )
            if prior is None:
                if ledger.existing_experiment_id(prepared.identity) is not None:
                    raise CampaignTechnicalError("CAMPAIGN_EXISTING_NONTERMINAL", False)
                raise CampaignTechnicalError("NO_PRIOR_EXECUTION", False)
            try:
                _require_execution_snapshot(hypothesis, paths, prepared)
            except StrategyLabError as error:
                raise CampaignTechnicalError("CAMPAIGN_PREFLIGHT_DRIFT", False) from error
            return TrialEvidence(
                prior.terminal_status,
                False,
                prior.reason_codes,
                prior.experiment_id,
                prior.candidate_id,
            )

    try:
        return _with_campaign_hypothesis(spec, attempt, lookup)
    except CampaignTechnicalError as error:
        if error.reason_code == "NO_PRIOR_EXECUTION":
            return None
        raise
    except (OSError, StrategyCampaignError, StrategyLabError, RuntimeError) as error:
        raise CampaignTechnicalError("CAMPAIGN_IDENTITY_INVALID", False) from error


def _campaign_execute(
    spec: CampaignSpec,
    paths: StrategyLoopPaths,
    ledger: StrategyLedger,
    preflight: CampaignPreflight,
    attempt: CampaignAttempt,
    reference: _PreparedExecution | None = None,
) -> TrialEvidence:
    """Execute one generated attempt through the real strategy-loop entry point."""
    claim = _ExecutionClaim()
    candidate_id: str | None = None

    def reused(evidence: TrialEvidence) -> TrialEvidence:
        reasons = evidence.reason_codes
        if "REUSED_EXECUTION" not in reasons:
            reasons = (*reasons, "REUSED_EXECUTION")
        return TrialEvidence(
            evidence.terminal_status,
            False,
            reasons,
            evidence.experiment_id,
            evidence.candidate_id,
        )

    def execute(hypothesis_path: Path, hypothesis: StrategyHypothesis) -> TrialEvidence:
        nonlocal candidate_id
        try:
            feedback = run_strategy_loop(
                hypothesis_path,
                paths,
                _claim=claim,
                _require_prepared=lambda claimed: _require_campaign_preflight(
                    claimed,
                    preflight,
                    reference,
                ),
            )
        except _ExistingNonterminalExperiment as error:
            raise CampaignTechnicalError(
                "CAMPAIGN_EXISTING_NONTERMINAL",
                False,
            ) from error
        claimed = claim.prepared
        if claimed is None:
            raise CampaignTechnicalError("CAMPAIGN_EXECUTION_UNRECORDED", False)
        expected_experiment_id = _experiment_id(claimed.identity)
        experiment_id = feedback.get("experiment_id")
        if not isinstance(experiment_id, str) or experiment_id != expected_experiment_id:
            raise CampaignTechnicalError("CAMPAIGN_EXECUTION_UNRECORDED", False)
        evidence = ledger.existing_execution(
            claimed.identity,
            claimed.screen_policy,
            hypothesis=hypothesis,
            prepared=claimed,
        )
        if evidence is None:
            raise CampaignTechnicalError(
                "CAMPAIGN_EXECUTION_UNRECORDED",
                False,
                experiment_id,
            )
        candidate_id = evidence.candidate_id
        try:
            _require_execution_snapshot(hypothesis, paths, claimed)
        except StrategyLabError as error:
            raise CampaignTechnicalError(
                "CAMPAIGN_PREFLIGHT_DRIFT",
                evidence.execution_started,
                experiment_id,
                candidate_id,
            ) from error
        if claim.reused:
            return reused(evidence)
        return evidence

    try:
        return _with_campaign_hypothesis(spec, attempt, execute)
    except CampaignTechnicalError:
        raise
    except Exception as error:
        experiment_id: str | None = None
        expected_experiment_id = (
            _experiment_id(claim.prepared.identity) if claim.prepared is not None else None
        )
        if expected_experiment_id is not None:
            with closing(sqlite3.connect(ledger.path)) as connection:
                exists = connection.execute(
                    "SELECT 1 FROM experiments WHERE experiment_id = ?",
                    (expected_experiment_id,),
                ).fetchone()
            if exists is not None:
                experiment_id = expected_experiment_id
                candidate_path = paths.state_path / "runs" / experiment_id / "candidate.json"
                if candidate_path.is_file():
                    try:
                        _candidate, candidate_id = load_pybroker_candidate(candidate_path)
                    except (OSError, ValueError):
                        candidate_id = None
        raise CampaignTechnicalError(
            "CAMPAIGN_EXECUTION_INVALID",
            claim.launched and experiment_id is not None,
            experiment_id,
            candidate_id,
        ) from error


def run_strategy_campaign(
    spec: CampaignSpec,
    paths: StrategyLoopPaths | None = None,
) -> dict[str, JsonValue]:
    """Run one bounded campaign through StrategyLedger and run_strategy_loop."""
    # Expansion is intentionally first: budget failure creates no ledger or data side effect.
    expand_campaign(spec)
    resolved_paths = paths or DEFAULT_LOOP_PATHS
    resolved_paths.state_path.mkdir(parents=True, exist_ok=True)
    # ponytail: one campaign-level flock serializes immutable census formation;
    # run_strategy_loop also claims each experiment against standalone callers.
    with (resolved_paths.state_path / "campaign.lock").open("a+b") as campaign_lock:
        fcntl.flock(campaign_lock, fcntl.LOCK_EX)
        ledger = StrategyLedger(resolved_paths.state_path / "ledger.sqlite3")
        ledger.initialize()
        attempts = expand_campaign(spec)
        all_attempts_invalid = bool(attempts) and all(
            attempt.strategy_id is None for attempt in attempts
        )
        research_policy_path = resolved_paths.research_policy_path or (
            _REPO_ROOT / "config/strategy_research_policy.json"
        )
        actual_screen_policy: ScreenPolicy | None = None
        reference_hypothesis: StrategyHypothesis | None = None
        reference_prepared: _PreparedExecution | None = None
        preflight_reasons: list[str] = []
        if all_attempts_invalid:
            actual_policy_id = spec.screen_policy_id
            actual_data_as_of_ns = spec.data_as_of_ns
            actual_data_source_id = "0" * 64
        else:
            try:
                reference_attempt = next(
                    attempt for attempt in attempts if attempt.strategy_id is not None
                )
                reference_hypothesis, reference_prepared = _campaign_reference_execution(
                    spec,
                    resolved_paths,
                    reference_attempt,
                )
                actual_screen_policy = reference_prepared.screen_policy
                if actual_screen_policy is None or reference_prepared.data_as_of_ns is None:
                    raise StrategyLabError("campaign reference execution is not V2")
                actual_policy_id = actual_screen_policy.policy_id
                actual_data_source_id = reference_prepared.identity.data_source_id
                actual_data_as_of_ns = reference_prepared.data_as_of_ns
            except (
                OSError,
                RuntimeError,
                StopIteration,
                StrategyCampaignError,
                StrategyLabError,
                ValueError,
            ):
                actual_policy_id = "0" * 64
                actual_data_as_of_ns = 0
                actual_data_source_id = "0" * 64
                preflight_reasons.append("CAMPAIGN_PREFLIGHT_DRIFT")
        preflight = CampaignPreflight(
            actual_policy_id,
            actual_data_as_of_ns,
            actual_data_source_id,
            tuple(dict.fromkeys(preflight_reasons)),
        )

        def validate_trial(trial: CampaignTrial) -> None:
            ledger.verify_campaign_trial(trial, actual_screen_policy)
            if (
                trial.evidence.experiment_id is None
                or (
                    trial.evidence.terminal_status is TerminalStatus.TECHNICAL_INVALID
                    and trial.evidence.reason_codes
                    and trial.evidence.reason_codes[0] == "CAMPAIGN_PREFLIGHT_DRIFT"
                )
            ):
                return
            if not 0 <= trial.ordinal < len(attempts):
                raise StrategyLabError("campaign trial ordinal is invalid")
            _hypothesis, current = _campaign_reference_execution(
                spec,
                resolved_paths,
                attempts[trial.ordinal],
            )
            _require_campaign_preflight(current, preflight, reference_prepared)
            if _experiment_id(current.identity) != trial.evidence.experiment_id:
                raise StrategyLabError("campaign trial execution identity is stale")

        return run_campaign(
            spec,
            ledger=ledger,
            preflight=preflight,
            execute=lambda attempt: _campaign_execute(
                spec,
                resolved_paths,
                ledger,
                preflight,
                attempt,
                reference_prepared,
            ),
            reuse=lambda attempt: _campaign_reuse_lookup(
                spec,
                resolved_paths,
                ledger,
                preflight,
                attempt,
                reference_prepared,
            ),
            validate_stored=validate_trial,
            reconcile=lambda: _reconcile_campaign_preflight(
                resolved_paths,
                research_policy_path,
                preflight,
                reference_hypothesis,
                reference_prepared,
            ),
        )


def run_strategy_loop(
    hypothesis_path: Path,
    paths: StrategyLoopPaths = DEFAULT_LOOP_PATHS,
    *,
    _claim: _ExecutionClaim | None = None,
    _require_prepared: Callable[[_PreparedExecution], None] | None = None,
) -> dict[str, JsonValue]:
    """Run or deterministically reuse one claimed PyBroker-to-Nautilus experiment."""
    source_hypothesis = load_strategy_hypothesis(Path(hypothesis_path))
    initial = _prepare_execution(source_hypothesis, paths)
    experiment_id = _experiment_id(initial.identity)
    lock_directory = paths.state_path / "experiment-locks"
    lock_directory.mkdir(parents=True, exist_ok=True)
    with (lock_directory / f"{experiment_id}.lock").open("a+b") as experiment_lock:
        fcntl.flock(experiment_lock, fcntl.LOCK_EX)
        prepared = _prepare_execution(source_hypothesis, paths)
        if prepared != initial:
            raise StrategyLabError("execution identity changed before claim")
        if _claim is not None:
            _claim.prepared = prepared
        if _require_prepared is not None:
            _require_prepared(prepared)
        return _run_strategy_loop_locked(
            Path(hypothesis_path),
            paths,
            source_hypothesis,
            prepared,
            _claim,
        )


def _run_strategy_loop_locked(
    hypothesis_path: Path,
    paths: StrategyLoopPaths,
    source_hypothesis: StrategyHypothesis,
    prepared: _PreparedExecution,
    claim: _ExecutionClaim | None = None,
) -> dict[str, JsonValue]:
    """Execute while the exact experiment identity is exclusively claimed."""
    identity = prepared.identity
    experiment_id = _experiment_id(identity)
    evaluation_context_id = prepared.evaluation_context_id
    screen_policy = prepared.screen_policy
    runtime_id = prepared.runtime_id
    code_commit = prepared.code_commit
    ledger = StrategyLedger(paths.state_path / "ledger.sqlite3")
    ledger.initialize()
    preexisting_experiment_id = ledger.existing_experiment_id(identity)
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
        prepared,
    )
    if existing is not None:
        _require_execution_snapshot(source_hypothesis, paths, prepared)
        if claim is not None:
            claim.reused = True
        return existing
    if preexisting_experiment_id is not None:
        raise _ExistingNonterminalExperiment("existing experiment is non-terminal")

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
    if claim is not None:
        claim.launched = True
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
    except (StrategyCampaignError, StrategyLabError) as error:
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
    is_v2 = hypothesis.identity_schema == "strategy-id-v2"
    raw_research_path = directory / (
        "research-result-v2-raw.json" if is_v2 else "research-result.json"
    )
    research_path = directory / (
        "research-screen-result-v1.json" if is_v2 else "research-result.json"
    )
    research_hash = _publish_json(raw_research_path, research_result)
    ledger.record_stage(
        StageRecord(
            experiment_id,
            "PyBroker completed",
            "PASSED",
            "PYBROKER_COMPLETED",
            str(raw_research_path),
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
            prepared.data_as_of_ns,
            runtime_id,
        )
        if hypothesis.identity_schema == "strategy-id-v2":
            validated_candidate_source_bars(candidate_document, paths.catalog_path)
            _require_execution_snapshot(hypothesis, paths, prepared)
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
    research_screen_recorded = False
    screen_decision = None
    screen_artifact_hash: str | None = None
    if screen_policy is not None:
        try:
            parsed_result = load_research_result_v2(completed.stdout)
            if parsed_result.candidate_id != candidate_id:
                raise StrategyLabError("research result candidate_id mismatch")
            candidate_signals = candidate_document.get("signals")
            if (
                not isinstance(candidate_signals, list)
                or parsed_result.metrics.signal_count != len(candidate_signals)
            ):
                raise StrategyLabError("research result signal_count mismatch")
            screen_decision = screen_research_result(parsed_result, screen_policy)
            screen_document = screened_result_document(
                parsed_result,
                screen_decision,
                screen_policy,
            )
            screen_artifact_hash = _publish_json(research_path, screen_document)
        except (StrategyCampaignError, StrategyLabError) as error:
            return _finish_failure(
                run,
                _TechnicalFailure(
                    "Research screened",
                    "RESEARCH",
                    "ERROR",
                    "RESEARCH_RESULT_INVALID",
                    "research-error.json",
                    {"detail": str(error)},
                ),
            )
        if screen_decision.outcome == "SCREEN_REJECTED":
            ledger.record_stage(
                StageRecord(
                    experiment_id,
                    "Research screened",
                    "REJECTED",
                    screen_decision.reason_codes[0],
                    str(research_path),
                    screen_artifact_hash,
                ),
            )
            feedback = {
                **_feedback_base(run),
                "error_id": None,
                "failed_stage": "RESEARCH",
                "reason_codes": list(screen_decision.reason_codes),
                "status": "SCREEN_REJECTED",
                "verdict_id": None,
            }
            _publish_json(_feedback_path(run), feedback)
            return feedback
    signal_parity: SignalParityResult | None = None
    if evaluation_context_id is not None:
        try:
            signal_parity = run_signal_parity_gate(candidate_path, paths.catalog_path)
            parity_document = json.loads(signal_parity.canonical_bytes)
            if (
                not isinstance(parity_document, dict)
                or not isinstance(candidate_signals, list)
                or parity_document.get("candidate_signal_count") != len(candidate_signals)
                or parity_document.get("recomputed_signal_count") != len(candidate_signals)
            ):
                raise StrategyLabError("signal parity counts do not match candidate")
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
                decisions=signal_parity.decisions,
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
    if screen_policy is not None and screen_decision is not None and screen_artifact_hash is not None:
        ledger.record_stage(
            StageRecord(
                experiment_id,
                "Research screened",
                "PASSED",
                "SCREEN_PASSED",
                str(research_path),
                screen_artifact_hash,
            ),
        )
        research_screen_recorded = True
    if not research_screen_recorded:
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
        if hypothesis.identity_schema == "strategy-id-v2":
            _require_execution_snapshot(hypothesis, paths, prepared)
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
    policy_ids: str | tuple[str, str],
) -> tuple[int, int, int]:
    policy_pair = (policy_ids, policy_ids) if isinstance(policy_ids, str) else policy_ids
    row = connection.execute(
        """WITH per_strategy AS (
            SELECT
                stage_results.strategy_id,
                MAX(outcome = 'PASSED') AS passed,
                MAX(outcome = 'REJECTED') AS rejected
            FROM stage_results
            JOIN experiments USING (experiment_id, strategy_id)
            WHERE stage = ? AND data_source_id = ? AND policy_id IN (?, ?)
            GROUP BY stage_results.strategy_id
        )
        SELECT
            COUNT(*),
            COALESCE(SUM(passed), 0),
            COALESCE(SUM(rejected AND NOT passed), 0)
        FROM per_strategy""",
        (stage, data_source_id, *policy_pair),
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
    ledger.require_terminal_consistency()
    data_source_id = _hash_tree(paths)
    policy_id, policy_version = _policy_identity(paths.policy_path)
    policy_ids = (policy_id, policy_id)
    research_policy_path = paths.research_policy_path or (
        _REPO_ROOT / "config/strategy_research_policy.json"
    )
    try:
        screen_policy = load_screen_policy(research_policy_path)
    except (OSError, StrategyCampaignError):
        pass
    else:
        policy_ids = (
            policy_id,
            sha256(
                _canonical_json(
                    {
                        "accounting_policy_id": policy_id,
                        "screen_policy_id": screen_policy.policy_id,
                    },
                ),
            ).hexdigest(),
        )
    with closing(sqlite3.connect(ledger.path)) as connection:
        proposed = connection.execute(
            """SELECT COUNT(DISTINCT strategy_id) FROM experiments
            WHERE data_source_id = ? AND policy_id IN (?, ?)""",
            (data_source_id, *policy_ids),
        ).fetchone()[0]
        contract_valid = connection.execute(
            """SELECT COUNT(DISTINCT experiments.strategy_id)
            FROM experiments JOIN hypotheses USING (hypothesis_id, strategy_id)
            WHERE data_source_id = ? AND policy_id IN (?, ?)""",
            (data_source_id, *policy_ids),
        ).fetchone()[0]
        operational = {
            label: _stage_projection(connection, label, data_source_id, policy_ids)
            for label in _STAGE_LABELS[2:5]
        }
        reason_rows = connection.execute(
            """SELECT errors.reason_code, experiments.strategy_id
            FROM errors JOIN experiments USING (experiment_id)
            WHERE data_source_id = ? AND policy_id IN (?, ?)
            UNION ALL
            SELECT verdicts.reason_code, experiments.strategy_id
            FROM verdicts JOIN experiments USING (experiment_id, strategy_id)
            WHERE data_source_id = ? AND policy_id IN (?, ?)""",
            (data_source_id, *policy_ids, data_source_id, *policy_ids),
        ).fetchall()
        verdict_rows = connection.execute(
            """SELECT experiments.strategy_id, verdicts.artifact_path, verdicts.artifact_sha256
            FROM verdicts JOIN experiments USING (experiment_id, strategy_id)
            WHERE data_source_id = ? AND policy_id IN (?, ?)""",
            (data_source_id, *policy_ids),
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
    campaign_command = commands.add_parser("campaign")
    campaign_command.add_argument("--spec", type=Path, required=True)
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
        case "campaign":
            spec = load_campaign_spec(arguments.spec)
            summary = run_strategy_campaign(spec)
            print(_canonical_json(summary).decode(), end="")
            return 0
        case unreachable:
            assert_never(unreachable)
