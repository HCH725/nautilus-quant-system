# noqa: E501  # noqa: SIZE_OK — Card 4 keeps prospective runtime/evidence in one trust-boundary module.
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass
from decimal import Decimal
from hashlib import sha256
import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
from tempfile import NamedTemporaryFile
import time
from typing import Any, Literal, Sequence

from nautilus_trader.adapters.binance import (
    BinanceDataClientConfig,
    BinanceDataClientFactory,
    BinanceEnvironment,
    BinanceInstrumentProviderConfig,
    BinanceProductType,
)
from nautilus_trader.adapters.sandbox import (
    SandboxExecutionClientConfig,
    SandboxExecutionClientFactory,
)
from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.common import Environment, LogLevel
from nautilus_trader.config import BacktestEngineConfig, LoggerConfig
from nautilus_trader.live import LiveNode, LiveRiskEngineConfig
from nautilus_trader.model import (
    AccountType,
    CryptoPerpetual,
    Currency,
    InstrumentId,
    Money,
    OmsType,
    Price,
    Quantity,
    Symbol,
    TraderId,
    Venue,
)

from .live_strategy import FamilyStrategy, RiskExecutionPolicy
from .nautilus_io import make_bar
from .strategy_families import ClosedBar, FamilyDecision, canonical_decision_bytes


_SHA256 = re.compile(r"[0-9a-f]{64}")
_GIT_COMMIT = re.compile(r"[0-9a-f]{40}")
_REPO_ROOT = Path(__file__).resolve().parents[2]
_USDT = Currency.from_str("USDT")
_BINANCE = Venue("BINANCE")


class PaperRuntimeError(ValueError):
    """Raised when Paper admission or runtime evidence fails closed."""


@dataclass(frozen=True, slots=True)
class PaperPolicy:
    policy_id: str
    schema_version: str
    minimum_completed_bars: int
    minimum_wall_clock_seconds: int
    minimum_restart_count: int
    bounded_smoke_completed_bars: int
    bounded_smoke_timeout_seconds: int
    allow_gaps: bool
    allow_revised_bars: bool
    require_reconciliation: bool
    require_instrument_metadata: bool
    require_mark_price_metadata: bool
    require_fee_metadata: bool


@dataclass(frozen=True, slots=True)
class StrategyFreeze:
    freeze_id: str
    strategy_id: str
    hypothesis_id: str
    candidate_id: str
    family_id: str
    family_version: str
    parameters: dict[str, Any]
    kernel_version: str
    kernel_hash: str
    code_commit: str
    instrument_id: str
    bar_type: str
    historical_verdict_id: str
    robustness_verdict_id: str
    inspected_data_boundary_ns: int
    data_snapshot_id: str
    runtime_id: str
    instrument_metadata_id: str
    paper_policy_id: str
    risk_policy_id: str


@dataclass(frozen=True, slots=True)
class RuntimeRun:
    run_id: str
    freeze_id: str
    tier: str
    environment: str
    cohort_start_ns: int
    instrument_metadata_id: str
    mark_price_metadata_id: str
    fee_metadata_id: str
    artifact_path: str
    artifact_sha256: str


@dataclass(frozen=True, slots=True)
class RuntimeVerdict:
    verdict_id: str
    run_id: str
    paper_policy_id: str
    technical_status: str
    strategy_outcome: str
    reason_codes: tuple[str, ...]
    cohort_end_ns: int
    completed_bars: int
    missing_bars: int
    revised_bars: int
    terminal_flat: bool
    open_order_count: int
    restart_count: int
    artifact_path: str
    artifact_sha256: str


@dataclass(frozen=True, slots=True)
class NodeComposition:
    node: LiveNode
    execution_registered: bool
    risk_config: LiveRiskEngineConfig
    data_environment: str
    execution_environment: str | None


def _canonical_json(value: object) -> bytes:
    try:
        return (json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode()
    except (TypeError, ValueError) as error:
        raise PaperRuntimeError("value must be finite plain JSON") from error


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PaperRuntimeError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _content_id(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise PaperRuntimeError(f"{field} must be lowercase SHA-256")
    return value


def _positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PaperRuntimeError(f"{field} must be a positive integer")
    return value


def load_paper_policy(path: Path | bytes) -> PaperPolicy:
    payload = path if isinstance(path, bytes) else Path(path).read_bytes()
    try:
        value = json.loads(payload, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PaperRuntimeError("paper policy must be UTF-8 JSON") from error
    expected = {
        "allow_gaps", "allow_revised_bars", "bounded_smoke_completed_bars",
        "bounded_smoke_timeout_seconds", "minimum_completed_bars",
        "minimum_restart_count", "minimum_wall_clock_seconds", "require_fee_metadata",
        "require_instrument_metadata", "require_mark_price_metadata",
        "require_reconciliation", "schema_version",
    }
    if not isinstance(value, dict) or set(value) != expected or payload != _canonical_json(value):
        raise PaperRuntimeError("paper policy must use canonical schema bytes")
    if value["schema_version"] != "strategy-paper-policy-v1":
        raise PaperRuntimeError("unsupported paper policy schema")
    for field in ("allow_gaps", "allow_revised_bars"):
        if value[field] is not False:
            raise PaperRuntimeError(f"{field} must fail closed")
    for field in ("require_fee_metadata", "require_instrument_metadata", "require_mark_price_metadata", "require_reconciliation"):
        if value[field] is not True:
            raise PaperRuntimeError(f"{field} is required")
    return PaperPolicy(
        policy_id=sha256(payload).hexdigest(),
        schema_version=value["schema_version"],
        minimum_completed_bars=_positive_integer(value["minimum_completed_bars"], "minimum_completed_bars"),
        minimum_wall_clock_seconds=_positive_integer(value["minimum_wall_clock_seconds"], "minimum_wall_clock_seconds"),
        minimum_restart_count=_positive_integer(value["minimum_restart_count"], "minimum_restart_count"),
        bounded_smoke_completed_bars=_positive_integer(value["bounded_smoke_completed_bars"], "bounded_smoke_completed_bars"),
        bounded_smoke_timeout_seconds=_positive_integer(value["bounded_smoke_timeout_seconds"], "bounded_smoke_timeout_seconds"),
        allow_gaps=False,
        allow_revised_bars=False,
        require_reconciliation=True,
        require_instrument_metadata=True,
        require_mark_price_metadata=True,
        require_fee_metadata=True,
    )


def build_strategy_freeze(admission: dict[str, object], paper_policy: PaperPolicy, risk_policy: RiskExecutionPolicy) -> StrategyFreeze:
    expected = {
        "bar_type", "candidate_id", "code_commit", "data_as_of_ns", "data_snapshot_id",
        "family_id", "family_version", "historical_verdict_id", "hypothesis_id",
        "instrument_id", "instrument_metadata_id", "kernel_hash", "kernel_version",
        "parameters", "robustness_action", "robustness_verdict_id", "runtime_id",
        "strategy_id",
    }
    if set(admission) != expected:
        raise PaperRuntimeError("strategy freeze admission fields are incomplete")
    if admission["robustness_action"] != "ADVANCE":
        raise PaperRuntimeError("strategy freeze requires robustness ADVANCE")
    for field in (
        "candidate_id", "data_snapshot_id", "historical_verdict_id", "hypothesis_id",
        "instrument_metadata_id", "kernel_hash", "robustness_verdict_id", "runtime_id",
        "strategy_id",
    ):
        _content_id(admission[field], field)
    if not isinstance(admission["code_commit"], str) or _GIT_COMMIT.fullmatch(admission["code_commit"]) is None:
        raise PaperRuntimeError("code_commit must be a full Git commit")
    boundary = admission["data_as_of_ns"]
    if isinstance(boundary, bool) or not isinstance(boundary, int) or boundary < 0:
        raise PaperRuntimeError("data_as_of_ns must be non-negative")
    for field in ("bar_type", "family_id", "family_version", "instrument_id", "kernel_version"):
        if not isinstance(admission[field], str) or not admission[field]:
            raise PaperRuntimeError(f"{field} must be non-empty")
    if not isinstance(admission["parameters"], dict):
        raise PaperRuntimeError("parameters must be a plain JSON object")
    document = {**admission, "paper_policy_id": paper_policy.policy_id, "risk_policy_id": risk_policy.policy_id, "schema_version": "strategy-freeze-v1"}
    del document["robustness_action"]
    freeze_id = sha256(_canonical_json(document)).hexdigest()
    return StrategyFreeze(
        freeze_id=freeze_id,
        strategy_id=str(admission["strategy_id"]),
        hypothesis_id=str(admission["hypothesis_id"]),
        candidate_id=str(admission["candidate_id"]),
        family_id=str(admission["family_id"]),
        family_version=str(admission["family_version"]),
        parameters=dict(admission["parameters"]),
        kernel_version=str(admission["kernel_version"]),
        kernel_hash=str(admission["kernel_hash"]),
        code_commit=str(admission["code_commit"]),
        instrument_id=str(admission["instrument_id"]),
        bar_type=str(admission["bar_type"]),
        historical_verdict_id=str(admission["historical_verdict_id"]),
        robustness_verdict_id=str(admission["robustness_verdict_id"]),
        inspected_data_boundary_ns=boundary,
        data_snapshot_id=str(admission["data_snapshot_id"]),
        runtime_id=str(admission["runtime_id"]),
        instrument_metadata_id=str(admission["instrument_metadata_id"]),
        paper_policy_id=paper_policy.policy_id,
        risk_policy_id=risk_policy.policy_id,
    )


def _atomic_publish(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise PaperRuntimeError(f"immutable artifact conflict: {path}")
        return sha256(payload).hexdigest()
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as target:
            temporary = Path(target.name)
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        if path.read_bytes() != payload:
            raise PaperRuntimeError("runtime artifact readback mismatch")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return sha256(payload).hexdigest()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_freezes (freeze_id TEXT PRIMARY KEY, document_json TEXT NOT NULL, artifact_path TEXT NOT NULL, artifact_sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runtime_runs (run_id TEXT PRIMARY KEY, freeze_id TEXT NOT NULL, tier TEXT NOT NULL CHECK(tier IN ('SHADOW','PAPER')), environment TEXT NOT NULL, cohort_start_ns INTEGER NOT NULL, instrument_metadata_id TEXT NOT NULL, mark_price_metadata_id TEXT NOT NULL, fee_metadata_id TEXT NOT NULL, artifact_path TEXT NOT NULL, artifact_sha256 TEXT NOT NULL, FOREIGN KEY(freeze_id) REFERENCES strategy_freezes(freeze_id));
CREATE TABLE IF NOT EXISTS runtime_verdicts (verdict_id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE, paper_policy_id TEXT NOT NULL, technical_status TEXT NOT NULL CHECK(technical_status IN ('PASS','ERROR')), strategy_outcome TEXT NOT NULL CHECK(strategy_outcome IN ('PASS','FAIL','NOT_APPLICABLE')), reason_codes_json TEXT NOT NULL, cohort_end_ns INTEGER NOT NULL, completed_bars INTEGER NOT NULL, missing_bars INTEGER NOT NULL, revised_bars INTEGER NOT NULL, terminal_flat INTEGER NOT NULL CHECK(terminal_flat IN (0,1)), open_order_count INTEGER NOT NULL, restart_count INTEGER NOT NULL, artifact_path TEXT NOT NULL, artifact_sha256 TEXT NOT NULL, FOREIGN KEY(run_id) REFERENCES runtime_runs(run_id));
"""


@dataclass(frozen=True, slots=True)
class RuntimeEvidenceStore:
    path: Path
    artifact_directory: Path

    def __post_init__(self) -> None:
        for value in (self.path, self.artifact_directory):
            if Path(value).resolve(strict=False).is_relative_to((_REPO_ROOT / "data").resolve()):
                raise PaperRuntimeError("runtime evidence cannot use canonical data paths")

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(_SCHEMA)
            for table in ("strategy_freezes", "runtime_runs", "runtime_verdicts"):
                for action in ("UPDATE", "DELETE"):
                    connection.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_immutable_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, '{table} records are immutable'); END")

    def record_freeze(self, freeze: StrategyFreeze) -> None:
        document = asdict(freeze)
        payload = _canonical_json(document)
        preimage = {
            "bar_type": freeze.bar_type,
            "candidate_id": freeze.candidate_id,
            "code_commit": freeze.code_commit,
            "data_as_of_ns": freeze.inspected_data_boundary_ns,
            "data_snapshot_id": freeze.data_snapshot_id,
            "family_id": freeze.family_id,
            "family_version": freeze.family_version,
            "historical_verdict_id": freeze.historical_verdict_id,
            "hypothesis_id": freeze.hypothesis_id,
            "instrument_id": freeze.instrument_id,
            "instrument_metadata_id": freeze.instrument_metadata_id,
            "kernel_hash": freeze.kernel_hash,
            "kernel_version": freeze.kernel_version,
            "paper_policy_id": freeze.paper_policy_id,
            "parameters": freeze.parameters,
            "risk_policy_id": freeze.risk_policy_id,
            "robustness_verdict_id": freeze.robustness_verdict_id,
            "runtime_id": freeze.runtime_id,
            "schema_version": "strategy-freeze-v1",
            "strategy_id": freeze.strategy_id,
        }
        if sha256(_canonical_json(preimage)).hexdigest() != freeze.freeze_id:
            raise PaperRuntimeError("strategy freeze hash mismatch")
        path = self.artifact_directory / "freezes" / freeze.freeze_id / "strategy-freeze-v1.json"
        artifact_hash = _atomic_publish(path, payload)
        expected = (freeze.freeze_id, payload.decode(), str(path), artifact_hash)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("INSERT INTO strategy_freezes VALUES (?, ?, ?, ?) ON CONFLICT(freeze_id) DO NOTHING", expected)
            stored = connection.execute("SELECT freeze_id, document_json, artifact_path, artifact_sha256 FROM strategy_freezes WHERE freeze_id = ?", (freeze.freeze_id,)).fetchone()
        if stored != expected:
            raise PaperRuntimeError("strategy freeze readback mismatch")

    def start_run(self, freeze: StrategyFreeze, *, tier: Literal["SHADOW", "PAPER"], environment: str, cohort_start_ns: int, instrument_metadata_id: str, mark_price_metadata_id: str, fee_metadata_id: str) -> RuntimeRun:
        if self.read_freeze(freeze.freeze_id) != freeze:
            raise PaperRuntimeError("runtime run requires a stored strategy freeze")
        for value, field in ((instrument_metadata_id, "instrument_metadata_id"), (mark_price_metadata_id, "mark_price_metadata_id"), (fee_metadata_id, "fee_metadata_id")):
            _content_id(value, field)
        if instrument_metadata_id != freeze.instrument_metadata_id:
            raise PaperRuntimeError("runtime instrument metadata differs from strategy freeze")
        if tier not in {"SHADOW", "PAPER"} or not environment or cohort_start_ns <= freeze.inspected_data_boundary_ns:
            raise PaperRuntimeError("runtime run is not prospective or has invalid tier")
        document = {"cohort_start_ns": cohort_start_ns, "environment": environment, "fee_metadata_id": fee_metadata_id, "freeze_id": freeze.freeze_id, "instrument_metadata_id": instrument_metadata_id, "mark_price_metadata_id": mark_price_metadata_id, "schema_version": "strategy-runtime-run-v1", "tier": tier}
        run_id = sha256(_canonical_json(document)).hexdigest()
        path = self.artifact_directory / "runs" / run_id / "runtime-run-v1.json"
        artifact_hash = _atomic_publish(path, _canonical_json({**document, "run_id": run_id}))
        run = RuntimeRun(run_id, freeze.freeze_id, tier, environment, cohort_start_ns, instrument_metadata_id, mark_price_metadata_id, fee_metadata_id, str(path), artifact_hash)
        expected = tuple(asdict(run).values())
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("INSERT INTO runtime_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(run_id) DO NOTHING", expected)
            stored = connection.execute("SELECT * FROM runtime_runs WHERE run_id = ?", (run_id,)).fetchone()
        if stored != expected:
            raise PaperRuntimeError("runtime run readback mismatch")
        return run

    def finish_run(self, run_id: str, *, paper_policy: PaperPolicy, technical_status: Literal["PASS", "ERROR"], strategy_outcome: Literal["PASS", "FAIL", "NOT_APPLICABLE"], reason_codes: tuple[str, ...], cohort_end_ns: int, completed_bars: int, missing_bars: int, revised_bars: int, terminal_flat: bool, open_order_count: int, restart_count: int) -> RuntimeVerdict:
        run = self.read_run(run_id)
        freeze = self.read_freeze(run.freeze_id)
        if paper_policy.policy_id != freeze.paper_policy_id:
            raise PaperRuntimeError("runtime verdict paper policy differs from strategy freeze")
        if cohort_end_ns < run.cohort_start_ns or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (completed_bars, missing_bars, revised_bars, open_order_count, restart_count)):
            raise PaperRuntimeError("runtime verdict counts or boundary are invalid")
        if not reason_codes or not all(isinstance(value, str) and value for value in reason_codes):
            raise PaperRuntimeError("runtime verdict requires reason codes")
        if technical_status == "PASS" and (not terminal_flat or open_order_count or missing_bars or revised_bars):
            raise PaperRuntimeError("passing runtime must be reconciled, flat, and gap-free")
        if run.tier == "PAPER" and technical_status == "PASS" and strategy_outcome == "PASS" and (
            completed_bars < paper_policy.minimum_completed_bars
            or cohort_end_ns - run.cohort_start_ns < paper_policy.minimum_wall_clock_seconds * 1_000_000_000
            or restart_count < paper_policy.minimum_restart_count
        ):
            raise PaperRuntimeError("prospective cohort requirements are incomplete")
        document = {"cohort_end_ns": cohort_end_ns, "completed_bars": completed_bars, "missing_bars": missing_bars, "open_order_count": open_order_count, "paper_policy_id": paper_policy.policy_id, "reason_codes": list(reason_codes), "restart_count": restart_count, "revised_bars": revised_bars, "run_id": run_id, "schema_version": "strategy-runtime-verdict-v1", "strategy_outcome": strategy_outcome, "technical_status": technical_status, "terminal_flat": terminal_flat}
        verdict_id = sha256(_canonical_json(document)).hexdigest()
        path = self.artifact_directory / "runs" / run_id / "runtime-verdict-v1.json"
        artifact_hash = _atomic_publish(path, _canonical_json({**document, "verdict_id": verdict_id}))
        verdict = RuntimeVerdict(verdict_id, run_id, paper_policy.policy_id, technical_status, strategy_outcome, reason_codes, cohort_end_ns, completed_bars, missing_bars, revised_bars, terminal_flat, open_order_count, restart_count, str(path), artifact_hash)
        expected = (verdict_id, run_id, paper_policy.policy_id, technical_status, strategy_outcome, _canonical_json(list(reason_codes)).decode(), cohort_end_ns, completed_bars, missing_bars, revised_bars, int(terminal_flat), open_order_count, restart_count, str(path), artifact_hash)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("INSERT INTO runtime_verdicts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", expected)
            stored = connection.execute("SELECT * FROM runtime_verdicts WHERE verdict_id = ?", (verdict_id,)).fetchone()
        if stored != expected:
            raise PaperRuntimeError("runtime verdict readback mismatch")
        return verdict

    def read_freeze(self, freeze_id: str) -> StrategyFreeze:
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute("SELECT document_json, artifact_path, artifact_sha256 FROM strategy_freezes WHERE freeze_id = ?", (freeze_id,)).fetchone()
        if row is None:
            raise PaperRuntimeError("strategy freeze is missing")
        payload = Path(row[1]).read_bytes()
        if sha256(payload).hexdigest() != row[2] or payload.decode() != row[0]:
            raise PaperRuntimeError("strategy freeze artifact mismatch")
        return StrategyFreeze(**json.loads(payload))

    def read_run(self, run_id: str) -> RuntimeRun:
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute("SELECT * FROM runtime_runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise PaperRuntimeError("runtime run is missing")
        run = RuntimeRun(*row)
        if sha256(Path(run.artifact_path).read_bytes()).hexdigest() != run.artifact_sha256:
            raise PaperRuntimeError("runtime run artifact mismatch")
        return run

    def read_verdict(self, verdict_id: str) -> RuntimeVerdict:
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute("SELECT * FROM runtime_verdicts WHERE verdict_id = ?", (verdict_id,)).fetchone()
        if row is None:
            raise PaperRuntimeError("runtime verdict is missing")
        reasons = json.loads(row[5])
        verdict = RuntimeVerdict(row[0], row[1], row[2], row[3], row[4], tuple(reasons), row[6], row[7], row[8], row[9], bool(row[10]), row[11], row[12], row[13], row[14])
        if sha256(Path(verdict.artifact_path).read_bytes()).hexdigest() != verdict.artifact_sha256:
            raise PaperRuntimeError("runtime verdict artifact mismatch")
        return verdict


def _risk_config(policy: RiskExecutionPolicy) -> LiveRiskEngineConfig:
    return LiveRiskEngineConfig(
        bypass=False,
        max_order_submit_rate=policy.maximum_order_rate,
        max_order_modify_rate=policy.maximum_order_rate,
        max_notional_per_order={"BTCUSDT-PERP.BINANCE": str(policy.maximum_notional)},
    )


def _node_builder(policy: RiskExecutionPolicy):
    risk = _risk_config(policy)
    builder = LiveNode.builder("PAPER-PROSPECTIVE-V1", TraderId("PAPER-001"), Environment.LIVE).with_risk_engine_config(risk)
    builder.add_data_client(
        "BINANCE",
        BinanceDataClientFactory(),
        BinanceDataClientConfig(
            product_type=BinanceProductType.USD_M,
            environment=BinanceEnvironment.LIVE,
            instrument_provider=BinanceInstrumentProviderConfig(load_all=False, load_ids=["BTCUSDT-PERP.BINANCE"]),
        ),
    )
    return builder, risk


def build_shadow_node(policy: RiskExecutionPolicy) -> NodeComposition:
    builder, risk = _node_builder(policy)
    return NodeComposition(builder.build(), False, risk, "LIVE", None)


def build_paper_node(policy: RiskExecutionPolicy) -> NodeComposition:
    builder, risk = _node_builder(policy)
    builder.add_simulated_exec_client(
        "SANDBOX",
        SandboxExecutionClientFactory(),
        SandboxExecutionClientConfig(
            venue=_BINANCE,
            starting_balances=[Money(Decimal("10000"), _USDT)],
            base_currency=_USDT,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            default_leverage=policy.default_leverage,
            use_position_ids=False,
            use_random_ids=False,
            use_reduce_only=True,
        ),
    )
    return NodeComposition(builder.build(), True, risk, "LIVE", "SANDBOX")


def reconcile_closed_bars(live_bars: Sequence[ClosedBar], canonical_bars: Sequence[ClosedBar], live_decisions: Sequence[FamilyDecision], canonical_decisions: Sequence[FamilyDecision]) -> dict[str, int | str]:
    if [_canonical_json(asdict(value)) for value in live_bars] != [_canonical_json(asdict(value)) for value in canonical_bars]:
        raise PaperRuntimeError("normalized bar bytes mismatch")
    if [canonical_decision_bytes(value) for value in live_decisions] != [canonical_decision_bytes(value) for value in canonical_decisions]:
        raise PaperRuntimeError("signal identity mismatch")
    return {"bar_count": len(live_bars), "signal_count": len(live_decisions), "status": "PASS"}


def _fixture_instrument() -> CryptoPerpetual:
    return CryptoPerpetual(
        InstrumentId.from_str("BTCUSDT-PERP.BINANCE"), Symbol("BTCUSDT"), Currency.from_str("BTC"), _USDT, _USDT,
        False, 2, 3, Price.from_str("0.01"), Quantity.from_str("0.001"), 0, 0,
        margin_init=Decimal("0.01"), margin_maint=Decimal("0.005"), maker_fee=Decimal("0.001"), taker_fee=Decimal("0.001"),
    )


def forced_long_flat_fixture(policy: RiskExecutionPolicy) -> dict[str, object]:
    instrument = _fixture_instrument()
    closes = ("1000", "1100", "1000", "1000")
    bars = [make_bar(instrument_id="BTCUSDT-PERP.BINANCE", interval="1h", price_type="LAST", price_precision=2, size_precision=3, open_=close, high=close, low=close, close=close, volume="10", close_ms=hour * 3_600_000) for hour, close in enumerate(closes, 1)]
    strategy = FamilyStrategy(
        family_id="lookback-momentum-long-flat",
        family_version="lookback-momentum-long-flat-v1",
        parameters={"entry_threshold": 0.05, "lookback_bars": 2},
        risk_policy=policy,
        mode="PAPER",
        instrument_id=instrument.id,
        bar_type=bars[0].bar_type,
        quantity=Quantity.from_str(str(policy.maximum_quantity)),
    )
    engine = BacktestEngine(BacktestEngineConfig(run_analysis=False, logging=LoggerConfig(stdout_level=LogLevel.OFF)))
    try:
        engine.add_venue(venue=_BINANCE, oms_type=OmsType.NETTING, account_type=AccountType.MARGIN, starting_balances=[Money(Decimal("10000"), _USDT)], base_currency=_USDT)
        engine.add_instrument(instrument)
        engine.add_strategy(strategy)
        engine.add_data(bars)
        engine.run()
        orders = sorted(engine.cache.orders(), key=lambda item: item.last_event.ts_event)
        fills = [{"intent": strategy.nautilus_order_intents[str(order.client_order_id)], "commission": str(order.last_event.commission)} for order in orders]
        account = engine.cache.account_for_venue(_BINANCE)
        if account is None or len(fills) != 2 or engine.cache.positions_open() or engine.cache.orders_open():
            raise PaperRuntimeError("forced LONG to FLAT sandbox fixture did not reconcile")
        return {"account_type": "MARGIN", "fills": fills, "oms_type": "NETTING", "open_order_count": len(engine.cache.orders_open()), "position_count": len(engine.cache.positions_open()), "strategy_class": type(strategy).__name__, "terminal_flat": True}
    finally:
        engine.dispose()


def run_bounded_shadow_smoke(
    risk_policy: RiskExecutionPolicy,
    *,
    completed_bars: int,
    timeout_seconds: int,
) -> dict[str, object]:
    """Collect future Binance USD-M public data without registering execution."""
    if completed_bars < 1 or timeout_seconds < 1:
        raise PaperRuntimeError("bounded smoke limits must be positive")
    instrument_id = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
    strategy = FamilyStrategy(
        family_id="lookback-momentum-long-flat",
        family_version="lookback-momentum-long-flat-v1",
        parameters={"entry_threshold": 0.05, "lookback_bars": 2},
        risk_policy=risk_policy,
        mode="SHADOW",
        instrument_id=instrument_id,
        bar_type="BTCUSDT-PERP.BINANCE-1-MINUTE-LAST-EXTERNAL",
        quantity=Quantity.from_str(str(risk_policy.maximum_quantity)),
    )
    composition = build_shadow_node(risk_policy)
    composition.node.add_strategy(strategy)
    started_ns = time.time_ns()
    deadline = time.monotonic() + timeout_seconds
    try:
        composition.node.start()
        while len(strategy.closed_bars) < completed_bars and time.monotonic() < deadline:
            composition.node.poll()
            time.sleep(0.05)
        if len(strategy.closed_bars) < completed_bars:
            raise PaperRuntimeError("bounded Shadow smoke timed out before future closed bars")
        instrument = composition.node.cache.instrument(instrument_id)
        if instrument is None:
            raise PaperRuntimeError("Binance instrument metadata was not loaded")
        if not strategy.mark_prices:
            raise PaperRuntimeError("Binance Mark Price channel produced no evidence")
        if composition.node.cache.orders():
            raise PaperRuntimeError("Shadow orders cache is not empty")
        selected_bars = strategy.closed_bars[:completed_bars]
        selected_decisions = tuple(
            decision
            for decision in strategy.decisions
            if decision.ts_event_ns <= selected_bars[-1].ts_event_ns
        )
        instrument_document = instrument.to_dict()
        instrument_metadata_id = sha256(_canonical_json(instrument_document)).hexdigest()
        fee_metadata_id = sha256(_canonical_json({
            "maker_fee": str(instrument.maker_fee),
            "taker_fee": str(instrument.taker_fee),
        })).hexdigest()
        mark_price_metadata_id = sha256(_canonical_json({
            "channel": "Binance USD-M Mark Price stream",
            "event_count": len(strategy.mark_prices),
            "latest": strategy.mark_prices[-1],
        })).hexdigest()
        return {
            "bar_count": len(selected_bars),
            "bars": [asdict(value) for value in selected_bars],
            "data_environment": "BINANCE_LIVE_PUBLIC",
            "ended_ns": time.time_ns(),
            "fee_metadata_id": fee_metadata_id,
            "instrument_metadata_id": instrument_metadata_id,
            "mark_price_metadata_id": mark_price_metadata_id,
            "order_count": 0,
            "schema_version": "bounded-shadow-smoke-v1",
            "signals": [asdict(value) for value in selected_decisions],
            "started_ns": started_ns,
            "technical_status": "PASS",
        }
    finally:
        composition.node.stop()
        composition.node.dispose()


def run_bounded_paper_smoke(
    risk_policy: RiskExecutionPolicy,
    *,
    completed_bars: int,
    timeout_seconds: int,
) -> dict[str, object]:
    """Start the real public-data Paper composition with active risk and sandbox execution."""
    if completed_bars < 1 or timeout_seconds < 1:
        raise PaperRuntimeError("bounded smoke limits must be positive")
    instrument_id = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
    strategy = FamilyStrategy(
        family_id="lookback-momentum-long-flat",
        family_version="lookback-momentum-long-flat-v1",
        parameters={"entry_threshold": 0.05, "lookback_bars": 2},
        risk_policy=risk_policy,
        mode="PAPER",
        instrument_id=instrument_id,
        bar_type="BTCUSDT-PERP.BINANCE-1-MINUTE-LAST-EXTERNAL",
        quantity=Quantity.from_str(str(risk_policy.maximum_quantity)),
    )
    composition = build_paper_node(risk_policy)
    composition.node.add_strategy(strategy)
    started_ns = time.time_ns()
    deadline = time.monotonic() + timeout_seconds
    stopped = False
    try:
        composition.node.start()
        while len(strategy.closed_bars) < completed_bars and time.monotonic() < deadline:
            composition.node.poll()
            time.sleep(0.05)
        if len(strategy.closed_bars) < completed_bars:
            raise PaperRuntimeError("bounded Paper smoke timed out before future closed bars")
        instrument = composition.node.cache.instrument(instrument_id)
        if instrument is None or not strategy.mark_prices:
            raise PaperRuntimeError("Paper metadata evidence is incomplete")
        selected_bars = strategy.closed_bars[:completed_bars]
        selected_decisions = tuple(
            decision
            for decision in strategy.decisions
            if decision.ts_event_ns <= selected_bars[-1].ts_event_ns
        )
        composition.node.stop()
        stopped = True
        account = composition.node.cache.account_for_venue(_BINANCE)
        if account is None or composition.node.cache.positions_open() or composition.node.cache.orders_open():
            raise PaperRuntimeError("bounded Paper smoke did not reconcile terminal sandbox state")
        orders = composition.node.cache.orders()
        return {
            "account": account.to_dict(),
            "bar_count": len(selected_bars),
            "bars": [asdict(value) for value in selected_bars],
            "data_environment": "BINANCE_LIVE_PUBLIC",
            "ended_ns": time.time_ns(),
            "execution_environment": "NAUTILUS_SANDBOX",
            "open_order_count": 0,
            "order_count": len(orders),
            "position_count": 0,
            "schema_version": "bounded-paper-live-smoke-v1",
            "signals": [asdict(value) for value in selected_decisions],
            "started_ns": started_ns,
            "technical_status": strategy.technical_status,
            "terminal_flat": True,
        }
    finally:
        if not stopped:
            composition.node.stop()
        composition.node.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run bounded public-data and sandbox Paper smoke")
    parser.add_argument("--risk-policy", type=Path, required=True)
    parser.add_argument("--completed-bars", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument("--output", type=Path)
    output.add_argument("--output-directory", type=Path)
    args = parser.parse_args(argv)
    from .live_strategy import load_risk_execution_policy

    risk_policy = load_risk_execution_policy(args.risk_policy)
    result = {
        "forced_fixture": forced_long_flat_fixture(risk_policy),
        "paper": run_bounded_paper_smoke(
            risk_policy,
            completed_bars=args.completed_bars,
            timeout_seconds=args.timeout_seconds,
        ),
        "schema_version": "bounded-card4-smoke-v1",
        "shadow": run_bounded_shadow_smoke(
            risk_policy,
            completed_bars=args.completed_bars,
            timeout_seconds=args.timeout_seconds,
        ),
    }
    payload = _canonical_json(result)
    destination = (
        args.output
        if args.output is not None
        else args.output_directory / f"{sha256(payload).hexdigest()}.json"
    )
    if destination.resolve(strict=False).is_relative_to((_REPO_ROOT / "data").resolve()):
        raise PaperRuntimeError("Shadow smoke output cannot use canonical data paths")
    _atomic_publish(destination, payload)
    print(payload.decode(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
