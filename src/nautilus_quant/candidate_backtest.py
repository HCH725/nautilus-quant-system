# noqa: E501  # noqa: SIZE_OK — Task C is explicitly scoped to one evaluator module.
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import platform
import re
from typing import Final, Literal

import nautilus_trader
from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.common import LogLevel
from nautilus_trader.config import BacktestEngineConfig, LoggerConfig
from nautilus_trader.core import UUID4
from nautilus_trader.execution import OneTickSlippageFillModel
from nautilus_trader.model import (
    AccountType,
    Bar,
    BarType,
    ContingencyType,
    CryptoPerpetual,
    Currency,
    FundingRateUpdate,
    InstrumentId,
    MarketOrder,
    MarkPriceUpdate,
    Money,
    OmsType,
    OrderSide,
    Price,
    Quantity,
    TimeInForce,
    Venue,
)
from nautilus_trader.persistence import ParquetDataCatalog
from nautilus_trader.trading import Strategy

from .funding_observation import (
    GENERATIONS_DIRECTORY,
    FUNDING_PRICE_SOURCE,
    MANIFEST_NAME,
    MODELED_FUNDING,
    OFFICIAL,
    READY_POINTER,
    FundingObservation,
    read_funding_observations,
)
from .pybroker_candidate import load_pybroker_candidate
from .strategy_families import (
    ClosedBar,
    FamilyDecision,
    FamilyKernelError,
    IncrementalFamilyEvaluator,
    canonical_decision_bytes,
)


type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type Intent = Literal["LONG", "FLAT"]

_POLICY_FIELDS: Final = {
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
}
_DECISION_POLICY_VERSION: Final = "strategy-loop-decision-v1"
_FIXED_QUANTITY_BTC: Final = Decimal("0.001")
_SIGNAL_TIMING: Final = "bar-close; effective no earlier than next event"
_SLIPPAGE_STATUS: Final = "unmodeled"
_USDT: Final = Currency.from_str("USDT")
_VENUE: Final = Venue("BINANCE")
_VERDICT_FIELDS: Final = frozenset(
    {
        "accounting_reconciled",
        "candidate_id",
        "canonical_result_hash",
        "code_commit",
        "decision",
        "ending_balance",
        "ending_position",
        "evaluation_windows",
        "execution",
        "experiment_id",
        "fees",
        "funding",
        "gross_trading_result",
        "hypothesis_id",
        "net_account_delta",
        "open_position_count",
        "performance_claimable",
        "policy_decision_version",
        "realized_balance_drawdown",
        "reason_codes",
        "runtime_versions",
        "schema_version",
        "source",
        "starting_balance",
        "status",
        "strategy_id",
    },
)
_COST_POLICY_FIELDS: Final = frozenset(
    {
        "cost_policy_id",
        "delay_bars",
        "fee_multiplier",
        "fee_source",
        "funding_multiplier",
        "funding_source",
        "schema_version",
        "slippage_model",
    },
)
_PARITY_FIELDS: Final = frozenset(
    {
        "candidate_id",
        "candidate_signal_count",
        "detail",
        "mismatch_index",
        "outcome",
        "reason_code",
        "recomputed_signal_count",
        "recomputed_signals_sha256",
        "required_action",
        "schema_version",
    },
)
_SHA256: Final = re.compile(r"[0-9a-f]{64}")


def _utc_timestamp_ns(value: str) -> int:
    raw = value[:-1]
    whole, separator, fraction = raw.partition(".")
    parsed = datetime.fromisoformat(whole + "+00:00")
    nanos = int((fraction + "000000000")[:9]) if separator else 0
    return int(parsed.timestamp()) * 1_000_000_000 + nanos


@dataclass(frozen=True, slots=True)
class SignalParityResult:
    candidate_id: str
    outcome: Literal["PASS", "ERROR"]
    reason_code: str
    required_action: Literal["FIX_TECHNICAL"] | None
    mismatch_index: int | None
    decisions: tuple[FamilyDecision, ...]
    canonical_bytes: bytes
    artifact_sha256: str


@dataclass(frozen=True, slots=True)
class CandidateBacktestRequest:
    candidate_path: Path
    catalog_path: Path
    funding_path: Path
    policy_path: Path
    hypothesis_id: str
    strategy_id: str
    experiment_id: str
    code_commit: str
    evaluation_start_utc: str | None = None
    evaluation_end_utc: str | None = None
    data_as_of_ns: int | None = None
    evaluation_context_id: str | None = None
    candidate_evaluation_context_id: str | None = None
    strategy_parameters_override: dict[str, JsonValue] | None = None
    fee_multiplier: Decimal = Decimal("1")
    funding_multiplier: Decimal = Decimal("1")
    delay_bars: int = 0
    slippage_model: Literal["none", "one_tick"] = "none"
    cost_policy_id: str | None = None
    robustness_cell_id: str | None = None
    signal_parity: SignalParityResult | None = None

    def __post_init__(self) -> None:
        if (
            self.candidate_evaluation_context_id is not None
            and (
                not isinstance(self.candidate_evaluation_context_id, str)
                or _SHA256.fullmatch(self.candidate_evaluation_context_id) is None
            )
        ):
            raise CandidateBacktestError("candidate_evaluation_context_id must be lowercase SHA-256")
        if self.strategy_parameters_override is not None:
            if not isinstance(self.strategy_parameters_override, dict):
                raise CandidateBacktestError("strategy_parameters_override must be an object")
            try:
                _canonical(self.strategy_parameters_override)
            except (TypeError, ValueError) as error:
                raise CandidateBacktestError(
                    "strategy_parameters_override must contain finite plain JSON",
                ) from error
        card3_values = (
            self.evaluation_start_utc,
            self.evaluation_end_utc,
            self.data_as_of_ns,
            self.evaluation_context_id,
        )
        if all(value is None for value in card3_values):
            # V1 callers remain readable; formal Card 3 callers must opt in as a
            # complete identity tuple below.
            self._validate_cost_fields()
            return
        if any(value is None for value in card3_values):
            raise CandidateBacktestError("CandidateBacktestRequest requires all four Card 3 bounds and identity fields")
        for name, value in (
            ("evaluation_start_utc", self.evaluation_start_utc),
            ("evaluation_end_utc", self.evaluation_end_utc),
        ):
            if not isinstance(value, str) or not value.endswith("Z"):
                raise CandidateBacktestError(f"{name} must be a UTC timestamp ending in Z")
            try:
                parsed = datetime.fromisoformat(value[:-1] + "+00:00")
            except ValueError as error:
                raise CandidateBacktestError(f"{name} must be a valid UTC timestamp") from error
            if parsed.tzinfo != timezone.utc:
                raise CandidateBacktestError(f"{name} must be UTC")
        if (
            isinstance(self.data_as_of_ns, bool)
            or not isinstance(self.data_as_of_ns, int)
            or self.data_as_of_ns < 0
        ):
            raise CandidateBacktestError("data_as_of_ns must be a non-negative integer")
        if (
            not isinstance(self.evaluation_context_id, str)
            or _SHA256.fullmatch(self.evaluation_context_id) is None
        ):
            raise CandidateBacktestError("evaluation_context_id must be lowercase SHA-256")
        start = _utc_timestamp_ns(self.evaluation_start_utc)
        end = _utc_timestamp_ns(self.evaluation_end_utc)
        if start >= end:
            raise CandidateBacktestError("evaluation UTC bounds must be ordered")
        self._validate_cost_fields()

    def _validate_cost_fields(self) -> None:
        for name, value in (
            ("fee_multiplier", self.fee_multiplier),
            ("funding_multiplier", self.funding_multiplier),
        ):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise CandidateBacktestError(f"{name} must be a positive finite Decimal")
        if isinstance(self.delay_bars, bool) or not isinstance(self.delay_bars, int) or self.delay_bars < 0:
            raise CandidateBacktestError("delay_bars must be a non-negative integer")
        if self.slippage_model not in {"none", "one_tick"}:
            raise CandidateBacktestError("slippage_model must be none or one_tick")
        for name, value in (("cost_policy_id", self.cost_policy_id), ("robustness_cell_id", self.robustness_cell_id)):
            if value is not None and (_SHA256.fullmatch(value) is None):
                raise CandidateBacktestError(f"{name} must be lowercase SHA-256")
        if self.cost_policy_id is not None and self.cost_policy_id != _canonical_cost_policy_id(
            self.fee_multiplier,
            self.funding_multiplier,
            self.delay_bars,
            self.slippage_model,
        ):
            raise CandidateBacktestError("cost_policy_id must match the canonical cost policy")


@dataclass(frozen=True, slots=True)
class CandidateBacktestResult:
    verdict: dict[str, JsonValue]
    canonical_bytes: bytes
    verdict_id: str


@dataclass(slots=True)
class CandidateBacktestError(RuntimeError):
    detail: str

    def __str__(self) -> str:
        return self.detail


@dataclass(frozen=True, slots=True)
class _Policy:
    starting_balance: Decimal
    quantity: Quantity
    historical_start: str
    historical_start_ns: int
    official_only_window_start: str
    decision_version: str
    signal_timing: str
    slippage_status: str


@dataclass(frozen=True, slots=True)
class _Signal:
    intent: Intent
    ts_event_ns: int


@dataclass(frozen=True, slots=True)
class _Action:
    intent: Intent
    source_signal_ns: int | None
    action_ns: int


@dataclass(frozen=True, slots=True)
class _ReplayPlan:
    instrument_id: InstrumentId
    bar_type: BarType
    quantity: Quantity
    signals: list[_Signal]
    boundary_ns: int
    delay_bars: int = 0


def candidate_request_bounds_ns(
    request: CandidateBacktestRequest,
) -> tuple[int, int, int] | None:
    """Return explicit Card 3 UTC bounds as nanoseconds, or None for V1 callers."""
    if request.evaluation_start_utc is None:
        return None
    start = _utc_timestamp_ns(request.evaluation_start_utc)
    end = _utc_timestamp_ns(request.evaluation_end_utc)
    return (
        start,
        end,
        request.data_as_of_ns,
    )


def _validate_candidate_request_identity(
    request: CandidateBacktestRequest,
    candidate: dict[str, JsonValue],
) -> tuple[int, int, int] | None:
    bounds = candidate_request_bounds_ns(request)
    if bounds is None:
        return None
    candidate_context = candidate.get("evaluation_context_id")
    expected_context = request.candidate_evaluation_context_id or request.evaluation_context_id
    if candidate_context is not None and candidate_context != expected_context:
        raise CandidateBacktestError("candidate evaluation_context_id does not match request")
    if request.candidate_evaluation_context_id is not None and candidate_context is None:
        raise CandidateBacktestError("candidate evaluation_context_id does not match request")
    source = _mapping(candidate["source"], "candidate source")
    source_last = source.get("last_ts_event_ns")
    if not isinstance(source_last, int) or source_last > bounds[2]:
        raise CandidateBacktestError("candidate source exceeds request data_as_of_ns")
    return bounds


def _canonical(value: JsonValue) -> bytes:
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


def _cost_policy_document(
    fee_multiplier: Decimal,
    funding_multiplier: Decimal,
    delay_bars: int,
    slippage_model: str,
) -> dict[str, JsonValue]:
    return {
        "delay_bars": delay_bars,
        "fee_multiplier": str(fee_multiplier),
        "fee_source": "nautilus_instrument_metadata",
        "funding_multiplier": str(funding_multiplier),
        "funding_source": "canonical_funding_observation_v1",
        "schema_version": "nautilus-cost-policy-v1",
        "slippage_model": slippage_model,
    }


def _canonical_cost_policy_id(
    fee_multiplier: Decimal,
    funding_multiplier: Decimal,
    delay_bars: int,
    slippage_model: str,
) -> str:
    return sha256(
        _canonical(
            _cost_policy_document(
                fee_multiplier,
                funding_multiplier,
                delay_bars,
                slippage_model,
            ),
        ),
    ).hexdigest()


def _mapping(value: JsonValue, name: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise CandidateBacktestError(f"validated {name} is not an object")
    return value


def _decimal(value: JsonValue, name: str) -> Decimal:
    if not isinstance(value, str):
        raise CandidateBacktestError(f"{name} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise CandidateBacktestError(f"{name} must be a decimal string") from error
    if not parsed.is_finite() or parsed <= 0:
        raise CandidateBacktestError(f"{name} must be positive and finite")
    return parsed


def _money(value: Decimal) -> str:
    return f"{value:.8f}"


def _verdict_object(value: JsonValue, fields: frozenset[str], name: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict) or set(value) != fields:
        raise CandidateBacktestError(f"invalid Nautilus verdict {name} fields")
    return value


def _verdict_integer(value: JsonValue, name: str, *, nullable: bool = False) -> int | None:
    if nullable and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CandidateBacktestError(f"Nautilus verdict {name} must be a non-negative integer")
    return value


def _verdict_decimal(value: JsonValue, name: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, str):
        raise CandidateBacktestError(f"Nautilus verdict {name} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise CandidateBacktestError(f"Nautilus verdict {name} must be a decimal string") from error
    if not parsed.is_finite() or (positive and parsed <= 0):
        raise CandidateBacktestError(f"Nautilus verdict {name} must be finite")
    return parsed


def _verdict_content_id(value: JsonValue, name: str, *, length: int = 64) -> str:
    if (
        not isinstance(value, str)
        or len(value) != length
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise CandidateBacktestError(f"Nautilus verdict {name} is invalid")
    return value


def _parity_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    value: dict[str, JsonValue] = {}
    for key, item in pairs:
        if key in value:
            raise CandidateBacktestError(f"signal parity artifact has duplicate key: {key}")
        value[key] = item
    return value


def _parity_constant(value: str) -> JsonValue:
    raise CandidateBacktestError(f"signal parity artifact contains non-finite value: {value}")


def candidate_signal_decisions(
    candidate: dict[str, JsonValue],
) -> tuple[FamilyDecision, ...]:
    """Decode the exact Candidate v2 decision sequence for parity reuse."""
    rows = candidate.get("signals")
    if not isinstance(rows, list):
        raise CandidateBacktestError("Candidate v2 signals are invalid")
    expected_fields = frozenset(FamilyDecision.__dataclass_fields__)
    decisions: list[FamilyDecision] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != expected_fields:
            raise CandidateBacktestError("Candidate v2 decision fields are invalid")
        try:
            decisions.append(FamilyDecision(**row))
        except (TypeError, ValueError) as error:
            raise CandidateBacktestError("Candidate v2 decision is invalid") from error
    return tuple(decisions)


def _recompute_family_decisions(
    candidate: dict[str, JsonValue],
    source_bars: list[Bar],
    parameters: dict[str, JsonValue],
) -> tuple[FamilyDecision, ...]:
    """Recompute signals from canonical closed bars without changing the candidate."""
    strategy = _mapping(candidate["strategy"], "candidate strategy")
    family_id = strategy.get("family_id")
    family_version = strategy.get("family_version")
    if not isinstance(family_id, str) or not isinstance(family_version, str):
        raise CandidateBacktestError("validated Candidate v2 family identity is invalid")
    evaluator = IncrementalFamilyEvaluator(
        family_id=family_id,
        family_version=family_version,
        parameters=parameters,
    )
    decisions: list[FamilyDecision] = []
    for bar in source_bars:
        decision = evaluator.push(
            ClosedBar(
                ts_event_ns=bar.ts_event,
                open=float(str(bar.open)),
                high=float(str(bar.high)),
                low=float(str(bar.low)),
                close=float(str(bar.close)),
                volume=float(str(bar.volume)),
            ),
        )
        if decision is not None:
            decisions.append(decision)
    return tuple(decisions)


def load_signal_parity_result(
    payload: bytes,
    *,
    candidate_id: str,
    candidate_signal_count: int,
    recomputed_decisions: tuple[FamilyDecision, ...],
    artifact_sha256: str | None = None,
) -> SignalParityResult:
    """Load one exact parity artifact against independently recomputed decisions."""
    try:
        value: JsonValue = json.loads(
            payload,
            object_pairs_hook=_parity_object,
            parse_constant=_parity_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CandidateBacktestError("signal parity artifact must be UTF-8 JSON") from error
    if not isinstance(value, dict) or set(value) != _PARITY_FIELDS:
        raise CandidateBacktestError("invalid signal parity artifact fields")
    if payload != _canonical(value):
        raise CandidateBacktestError("signal parity artifact must use canonical JSON encoding")
    _verdict_content_id(value["candidate_id"], "signal_parity.candidate_id")
    if value["candidate_id"] != candidate_id:
        raise CandidateBacktestError("signal parity candidate_id mismatch")
    if (
        isinstance(candidate_signal_count, bool)
        or not isinstance(candidate_signal_count, int)
        or candidate_signal_count < 0
    ):
        raise CandidateBacktestError("signal parity candidate signal count is invalid")
    artifact_candidate_count = _verdict_integer(
        value["candidate_signal_count"],
        "signal_parity.candidate_signal_count",
    )
    recomputed_count = _verdict_integer(
        value["recomputed_signal_count"],
        "signal_parity.recomputed_signal_count",
    )
    if artifact_candidate_count != candidate_signal_count:
        raise CandidateBacktestError("signal parity candidate signal count mismatch")
    if recomputed_count != len(recomputed_decisions):
        raise CandidateBacktestError("signal parity recomputed signal count mismatch")
    decision_payload = b"".join(
        canonical_decision_bytes(item) for item in recomputed_decisions
    )
    if value["recomputed_signals_sha256"] != sha256(decision_payload).hexdigest():
        raise CandidateBacktestError("signal parity recomputed decisions hash mismatch")
    if value["schema_version"] != "signal-parity-result-v1":
        raise CandidateBacktestError("unsupported signal parity schema")
    outcome = value["outcome"]
    reason_code = value["reason_code"]
    required_action = value["required_action"]
    mismatch_index = value["mismatch_index"]
    detail = value["detail"]
    if outcome == "PASS":
        if (
            reason_code != "SIGNAL_PARITY_MATCH"
            or required_action is not None
            or mismatch_index is not None
            or detail is not None
            or candidate_signal_count != len(recomputed_decisions)
        ):
            raise CandidateBacktestError("signal parity PASS fields are inconsistent")
    elif outcome == "ERROR":
        if required_action != "FIX_TECHNICAL" or reason_code not in {
            "SIGNAL_PARITY_MISMATCH",
            "SIGNAL_PARITY_RECOMPUTE_FAILED",
        }:
            raise CandidateBacktestError("signal parity ERROR fields are inconsistent")
        if not isinstance(detail, str) or not detail:
            raise CandidateBacktestError("signal parity ERROR detail is invalid")
        if reason_code == "SIGNAL_PARITY_RECOMPUTE_FAILED":
            if mismatch_index is not None:
                raise CandidateBacktestError("signal parity recompute mismatch index is invalid")
        elif (
            isinstance(mismatch_index, bool)
            or not isinstance(mismatch_index, int)
            or not 0 <= mismatch_index <= max(candidate_signal_count, len(recomputed_decisions))
        ):
            raise CandidateBacktestError("signal parity mismatch index is invalid")
    else:
        raise CandidateBacktestError("signal parity outcome is invalid")
    if artifact_sha256 is not None and sha256(payload).hexdigest() != artifact_sha256:
        raise CandidateBacktestError("signal parity artifact hash mismatch")
    return SignalParityResult(
        candidate_id=candidate_id,
        outcome=outcome,
        reason_code=reason_code,
        required_action=required_action,
        mismatch_index=mismatch_index,
        decisions=recomputed_decisions,
        canonical_bytes=payload,
        artifact_sha256=sha256(payload).hexdigest(),
    )


def load_candidate_backtest_verdict(payload: bytes) -> dict[str, JsonValue]:
    """Load one canonical, structurally complete Nautilus historical verdict."""
    try:
        value: JsonValue = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CandidateBacktestError("Nautilus verdict must be UTF-8 JSON") from error
    if not isinstance(value, dict) or set(value) not in {
        _VERDICT_FIELDS,
        _VERDICT_FIELDS | {"signal_parity"},
        _VERDICT_FIELDS | {"cost_policy"},
        _VERDICT_FIELDS | {"signal_parity", "cost_policy"},
    }:
        raise CandidateBacktestError("invalid Nautilus verdict fields")
    try:
        canonical = _canonical(value)
    except (TypeError, ValueError) as error:
        raise CandidateBacktestError("Nautilus verdict contains invalid JSON values") from error
    if payload != canonical:
        raise CandidateBacktestError("Nautilus verdict must use canonical JSON encoding")

    document = value
    cost_policy = document.get("cost_policy")
    if cost_policy is not None:
        if not isinstance(cost_policy, dict) or set(cost_policy) != _COST_POLICY_FIELDS:
            raise CandidateBacktestError("Nautilus verdict cost policy is invalid")
        _verdict_content_id(cost_policy["cost_policy_id"], "cost_policy.cost_policy_id")
        if cost_policy["schema_version"] != "nautilus-cost-policy-v1":
            raise CandidateBacktestError("Nautilus verdict cost policy schema is invalid")
        if cost_policy["fee_source"] != "nautilus_instrument_metadata" or cost_policy["funding_source"] != "canonical_funding_observation_v1":
            raise CandidateBacktestError("Nautilus verdict cost policy source is invalid")
        fee_multiplier = _verdict_decimal(
            cost_policy["fee_multiplier"],
            "cost_policy.fee_multiplier",
            positive=True,
        )
        funding_multiplier = _verdict_decimal(
            cost_policy["funding_multiplier"],
            "cost_policy.funding_multiplier",
            positive=True,
        )
        delay_bars = _verdict_integer(cost_policy["delay_bars"], "cost_policy.delay_bars")
        if cost_policy["slippage_model"] not in {"none", "one_tick"}:
            raise CandidateBacktestError("Nautilus verdict cost policy slippage is invalid")
        if (
            delay_bars is None
            or delay_bars < 0
            or cost_policy["cost_policy_id"]
            != _canonical_cost_policy_id(
                fee_multiplier,
                funding_multiplier,
                delay_bars,
                str(cost_policy["slippage_model"]),
            )
        ):
            raise CandidateBacktestError("Nautilus verdict cost policy identity is invalid")
    claimed_result_hash = _verdict_content_id(
        document["canonical_result_hash"],
        "canonical_result_hash",
    )
    preimage = dict(document)
    del preimage["canonical_result_hash"]
    if sha256(_canonical(preimage)).hexdigest() != claimed_result_hash:
        raise CandidateBacktestError("Nautilus verdict canonical_result_hash mismatch")
    for field in ("candidate_id", "experiment_id", "hypothesis_id", "strategy_id"):
        _verdict_content_id(document[field], field)
    _verdict_content_id(document["code_commit"], "code_commit", length=40)
    if (
        document["schema_version"] != "nautilus-verdict-v1"
        or document["status"] != "EVALUATED"
        or document["accounting_reconciled"] is not True
        or document["ending_position"] != "FLAT"
        or document["open_position_count"] != 0
        or not isinstance(document["performance_claimable"], bool)
        or document["policy_decision_version"] != _DECISION_POLICY_VERSION
    ):
        raise CandidateBacktestError("Nautilus verdict terminal fields are invalid")
    decision = document["decision"]
    reason_codes = document["reason_codes"]
    if decision not in {"REVISE", "RETAIN_FOR_RESEARCH"} or (
        not isinstance(reason_codes, list)
        or not reason_codes
        or not all(isinstance(reason, str) and reason for reason in reason_codes)
    ):
        raise CandidateBacktestError("Nautilus verdict decision is invalid")
    starting_balance = _verdict_decimal(
        document["starting_balance"],
        "starting_balance",
        positive=True,
    )
    ending_balance = _verdict_decimal(document["ending_balance"], "ending_balance")
    gross_result = _verdict_decimal(document["gross_trading_result"], "gross_trading_result")
    net_account_delta = _verdict_decimal(document["net_account_delta"], "net_account_delta")
    realized_drawdown = _verdict_decimal(
        document["realized_balance_drawdown"],
        "realized_balance_drawdown",
    )
    if ending_balance <= 0:
        raise CandidateBacktestError("Nautilus verdict ending_balance must be positive")
    if realized_drawdown < 0:
        raise CandidateBacktestError("Nautilus verdict drawdown must be non-negative")
    if ending_balance - starting_balance != net_account_delta:
        raise CandidateBacktestError("Nautilus verdict balance delta does not reconcile")

    windows = _verdict_object(
        document["evaluation_windows"],
        frozenset(
            {
                "actual_first_ts_event_ns",
                "actual_last_ts_event_ns",
                "configured_historical_start",
                "first_official_funding_ns",
            },
        ),
        "evaluation_windows",
    )
    first_event = _verdict_integer(windows["actual_first_ts_event_ns"], "actual_first_ts_event_ns")
    last_event = _verdict_integer(windows["actual_last_ts_event_ns"], "actual_last_ts_event_ns")
    if (
        first_event is None
        or last_event is None
        or first_event > last_event
        or not isinstance(windows["configured_historical_start"], str)
        or not windows["configured_historical_start"]
    ):
        raise CandidateBacktestError("Nautilus verdict evaluation window is invalid")
    _verdict_integer(windows["first_official_funding_ns"], "first_official_funding_ns", nullable=True)

    execution = _verdict_object(
        document["execution"],
        frozenset(
            {
                "boundary_flattened",
                "deduped_signal_count",
                "fill_count",
                "fills",
                "fixed_quantity_btc",
                "order_count",
                "signal_timing",
                "slippage_status",
                "trade_count",
            },
        ),
        "execution",
    )
    if not isinstance(execution["boundary_flattened"], bool):
        raise CandidateBacktestError("Nautilus verdict execution boundary is invalid")
    slippage_status = execution["slippage_status"]
    allowed_slippage_statuses = {_SLIPPAGE_STATUS}
    if isinstance(cost_policy, dict):
        if cost_policy["slippage_model"] == "one_tick":
            allowed_slippage_statuses.add("modeled_one_tick")
        else:
            allowed_slippage_statuses.add("modeled")
    if slippage_status not in allowed_slippage_statuses:
        raise CandidateBacktestError("Nautilus verdict slippage policy is invalid")
    if slippage_status in {"modeled", "modeled_one_tick"} and (
        not isinstance(cost_policy, dict)
        or (slippage_status == "modeled_one_tick") != (cost_policy["slippage_model"] == "one_tick")
    ):
        raise CandidateBacktestError("Nautilus verdict modeled slippage is unbound")
    fixed_quantity = _verdict_decimal(
        execution["fixed_quantity_btc"],
        "fixed_quantity_btc",
        positive=True,
    )
    if execution["signal_timing"] != _SIGNAL_TIMING or fixed_quantity != _FIXED_QUANTITY_BTC:
        raise CandidateBacktestError("Nautilus verdict execution policy is invalid")
    counts = {
        field: _verdict_integer(execution[field], field)
        for field in ("deduped_signal_count", "fill_count", "order_count", "trade_count")
    }
    assert all(value is not None for value in counts.values())
    fills = execution["fills"]
    if not isinstance(fills, list) or execution["fill_count"] != len(fills):
        raise CandidateBacktestError("Nautilus verdict fill count is invalid")
    fill_commissions: list[Decimal] = []
    for fill in fills:
        item = _verdict_object(
            fill,
            frozenset(
                {
                    "action_ts_event_ns",
                    "commission",
                    "fill_ts_event_ns",
                    "intent",
                    "quantity",
                    "source_signal_ts_event_ns",
                },
            ),
            "fill",
        )
        action_ts = _verdict_integer(item["action_ts_event_ns"], "action_ts_event_ns")
        _verdict_integer(item["fill_ts_event_ns"], "fill_ts_event_ns")
        source_signal_ts = _verdict_integer(
            item["source_signal_ts_event_ns"],
            "source_signal_ts_event_ns",
            nullable=True,
        )
        fill_commissions.append(_verdict_decimal(item["commission"], "commission"))
        if _verdict_decimal(item["quantity"], "quantity", positive=True) != fixed_quantity:
            raise CandidateBacktestError("Nautilus verdict fill quantity is inconsistent")
        if source_signal_ts is not None and action_ts <= source_signal_ts:
            raise CandidateBacktestError("Nautilus verdict fill timing is inconsistent")
        if item["intent"] not in {"LONG", "FLAT"}:
            raise CandidateBacktestError("Nautilus verdict fill intent is invalid")
    if counts["order_count"] != counts["fill_count"] or counts["trade_count"] != sum(
        item["intent"] == "FLAT" for item in fills
    ):
        raise CandidateBacktestError("Nautilus verdict execution counts are inconsistent")

    fees = _verdict_object(
        document["fees"],
        frozenset({"maker_rate", "source", "taker_rate", "total"}),
        "fees",
    )
    if fees["source"] != "nautilus_instrument_metadata":
        raise CandidateBacktestError("Nautilus verdict fee source is invalid")
    for field in ("maker_rate", "taker_rate", "total"):
        _verdict_decimal(fees[field], f"fees.{field}")
    fee_total = _verdict_decimal(fees["total"], "fees.total")
    if sum(fill_commissions, Decimal()) != fee_total:
        raise CandidateBacktestError("Nautilus verdict fees do not reconcile with fills")

    funding = _verdict_object(
        document["funding"],
        frozenset(
            {"events", "same_timestamp_order", "source", "total", "truth_counts", "truth_status"},
        ),
        "funding",
    )
    if (
        funding["same_timestamp_order"] != "mark_then_funding"
        or funding["source"] != "canonical_funding_observation_v1"
        or funding["truth_status"] not in {"official", "modeled_funding", "mixed", "missing"}
    ):
        raise CandidateBacktestError("Nautilus verdict funding policy is invalid")
    funding_total = _verdict_decimal(funding["total"], "funding.total")
    truth_counts = _verdict_object(
        funding["truth_counts"],
        frozenset({"missing_mark", "modeled_funding", "official"}),
        "funding.truth_counts",
    )
    truth_count_values: dict[str, int] = {}
    for field in truth_counts:
        truth_count = _verdict_integer(truth_counts[field], f"funding.truth_counts.{field}")
        assert truth_count is not None
        truth_count_values[field] = truth_count
    events = funding["events"]
    if not isinstance(events, list):
        raise CandidateBacktestError("Nautilus verdict funding events are invalid")
    event_total = Decimal()
    event_counts = {"official": 0, "modeled_funding": 0}
    for event in events:
        item = _verdict_object(
            event,
            frozenset({"amount", "mark_price", "price_source", "rate", "truth_status", "ts_event_ns"}),
            "funding event",
        )
        event_total += _verdict_decimal(item["amount"], "funding.amount")
        _verdict_decimal(item["mark_price"], "funding.mark_price", positive=True)
        _verdict_decimal(item["rate"], "funding.rate")
        _verdict_integer(item["ts_event_ns"], "funding.ts_event_ns")
        if (
            not isinstance(item["price_source"], str)
            or not item["price_source"]
            or item["truth_status"] not in {"official", "modeled_funding"}
        ):
            raise CandidateBacktestError("Nautilus verdict funding event is invalid")
        event_counts[str(item["truth_status"])] += 1
    if event_total != funding_total:
        raise CandidateBacktestError("Nautilus verdict funding total does not reconcile")
    if any(event_counts[field] > truth_count_values[field] for field in event_counts):
        raise CandidateBacktestError("Nautilus verdict funding counts are inconsistent")
    if truth_count_values["missing_mark"] > truth_count_values["modeled_funding"]:
        raise CandidateBacktestError("Nautilus verdict funding truth counts are inconsistent")

    source = _verdict_object(
        document["source"],
        frozenset({"first_ts_event_ns", "last_ts_event_ns", "row_count", "sha256"}),
        "source",
    )
    source_first = _verdict_integer(source["first_ts_event_ns"], "source.first_ts_event_ns")
    source_last = _verdict_integer(source["last_ts_event_ns"], "source.last_ts_event_ns")
    source_rows = _verdict_integer(source["row_count"], "source.row_count")
    _verdict_content_id(source["sha256"], "source.sha256")
    if source_first is None or source_last is None or source_rows is None or source_first > source_last or source_rows == 0:
        raise CandidateBacktestError("Nautilus verdict source range is invalid")

    versions = _verdict_object(
        document["runtime_versions"],
        frozenset({"nautilus_trader", "nautilus_python", "pybroker", "research_python"}),
        "runtime_versions",
    )
    if (
        not all(isinstance(item, str) and item for item in versions.values())
        or versions["nautilus_trader"] != nautilus_trader.__version__
        or versions["nautilus_python"] != platform.python_version()
    ):
        raise CandidateBacktestError("Nautilus verdict runtime versions are invalid")
    expected_performance_claimable = (
        funding["truth_status"] == "official"
        and slippage_status != "unmodeled"
    )
    if document["performance_claimable"] is not expected_performance_claimable:
        raise CandidateBacktestError("Nautilus verdict performance claim is inconsistent")
    expected_truth_status = (
        "official"
        if truth_count_values["official"] and not truth_count_values["modeled_funding"]
        else "modeled_funding"
        if truth_count_values["modeled_funding"] and not truth_count_values["official"]
        else "mixed"
        if truth_count_values["modeled_funding"] and truth_count_values["official"]
        else "missing"
    )
    if funding["truth_status"] != expected_truth_status:
        raise CandidateBacktestError("Nautilus verdict funding truth status is inconsistent")
    if gross_result + fee_total + funding_total != net_account_delta:
        raise CandidateBacktestError("Nautilus verdict gross, fees, and funding do not reconcile")
    expected_reason_codes = [
        "POSITIVE_NET_RESEARCH_ONLY" if net_account_delta > 0 else "NON_POSITIVE_NET_RESULT",
    ]
    if slippage_status == "unmodeled":
        expected_reason_codes.append("UNMODELED_SLIPPAGE")
    if funding["truth_status"] != "official":
        expected_reason_codes.append(f"FUNDING_TRUTH_{str(funding['truth_status']).upper()}")
    if document["reason_codes"] != expected_reason_codes:
        raise CandidateBacktestError("Nautilus verdict decision reasons are inconsistent")
    expected_decision = "RETAIN_FOR_RESEARCH" if net_account_delta > 0 else "REVISE"
    if document["decision"] != expected_decision:
        raise CandidateBacktestError("Nautilus verdict decision is inconsistent with net result")

    parity = document.get("signal_parity")
    if parity is not None:
        parity_document = _verdict_object(
            parity,
            frozenset({"artifact_sha256", "outcome", "reason_code"}),
            "signal_parity",
        )
        _verdict_content_id(parity_document["artifact_sha256"], "signal_parity.artifact_sha256")
        if (
            parity_document["outcome"] != "PASS"
            or parity_document["reason_code"] != "SIGNAL_PARITY_MATCH"
        ):
            raise CandidateBacktestError("Nautilus verdict signal parity is invalid")
    return document


def _load_policy(path: Path) -> _Policy:
    payload = Path(path).read_bytes()
    try:
        root = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CandidateBacktestError("strategy loop policy must be UTF-8 JSON") from error
    if not isinstance(root, dict) or set(root) != _POLICY_FIELDS:
        raise CandidateBacktestError("invalid strategy loop policy fields")
    if payload != _canonical(root):
        raise CandidateBacktestError("strategy loop policy must use canonical JSON encoding")
    if (
        root["schema_version"] != "strategy-loop-policy-v1"
        or root["fee_source"] != "nautilus_instrument_metadata"
        or root["leverage_enabled"] is not False
        or root["official_only_window_start"] != "first_official_funding_observation"
        or root["decision_policy_version"] != _DECISION_POLICY_VERSION
        or root["fixed_quantity_btc"] != str(_FIXED_QUANTITY_BTC)
        or root["signal_timing"] != _SIGNAL_TIMING
        or root["slippage_status"] != _SLIPPAGE_STATUS
        or not isinstance(root["historical_start"], str)
        or not isinstance(root["decision_policy_version"], str)
    ):
        raise CandidateBacktestError("unsupported strategy loop policy")
    try:
        historical_start = datetime.strptime(
            root["historical_start"],
            "%Y-%m-%dT%H:%M:%SZ",
        ).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError) as error:
        raise CandidateBacktestError("historical_start must be a UTC second boundary") from error
    return _Policy(
        starting_balance=_decimal(root["starting_balance_usdt"], "starting_balance_usdt"),
        quantity=Quantity.from_str(str(_decimal(root["fixed_quantity_btc"], "fixed_quantity_btc"))),
        historical_start=root["historical_start"],
        historical_start_ns=int(historical_start.timestamp()) * 1_000_000_000,
        official_only_window_start=root["official_only_window_start"],
        decision_version=root["decision_policy_version"],
        signal_timing=root["signal_timing"],
        slippage_status=root["slippage_status"],
    )


def _catalog_digest(catalog_path: Path, bar_type: str) -> str:
    paths = sorted((catalog_path / "data" / "bars" / bar_type).glob("*.parquet"))
    if not paths:
        raise CandidateBacktestError(f"no catalog bytes for {bar_type}")
    digest = sha256()
    for path in paths:
        digest.update(path.relative_to(catalog_path).as_posix().encode())
        digest.update(b"\0")
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def _source_bars(
    candidate: dict[str, JsonValue],
    catalog: ParquetDataCatalog,
    catalog_path: Path,
) -> list[Bar]:
    bar_type = candidate["bar_type"]
    if not isinstance(bar_type, str):
        raise CandidateBacktestError("validated bar_type is not a string")
    bars = catalog.query_bars([bar_type])
    if not bars or not all(isinstance(bar, Bar) for bar in bars):
        raise CandidateBacktestError("catalog query did not return historical Bar objects")
    source = _mapping(candidate["source"], "candidate source")
    actual = {
        "sha256": _catalog_digest(catalog_path, bar_type),
        "first_ts_event_ns": bars[0].ts_event,
        "last_ts_event_ns": bars[-1].ts_event,
        "row_count": len(bars),
    }
    for field, value in actual.items():
        if source[field] != value:
            raise CandidateBacktestError(
                f"source {field} mismatch: candidate={source[field]}, catalog={value}",
            )
    times = [bar.ts_event for bar in bars]
    if times != sorted(set(times)):
        raise CandidateBacktestError("catalog bars must be unique and strictly ordered")
    return bars


def validated_candidate_source_bars(
    candidate: dict[str, JsonValue],
    catalog_path: Path,
) -> list[Bar]:
    """Validate candidate source identity against the current canonical catalog."""
    catalog = ParquetDataCatalog(str(catalog_path))
    return _source_bars(candidate, catalog, Path(catalog_path))


def _parity_result(
    *,
    candidate_id: str,
    outcome: Literal["PASS", "ERROR"],
    reason_code: str,
    mismatch_index: int | None,
    decisions: tuple[FamilyDecision, ...],
    candidate_signal_count: int,
    detail: str | None,
) -> SignalParityResult:
    decision_payload = b"".join(canonical_decision_bytes(item) for item in decisions)
    document: dict[str, JsonValue] = {
        "candidate_id": candidate_id,
        "candidate_signal_count": candidate_signal_count,
        "detail": detail,
        "mismatch_index": mismatch_index,
        "outcome": outcome,
        "reason_code": reason_code,
        "recomputed_signal_count": len(decisions),
        "recomputed_signals_sha256": sha256(decision_payload).hexdigest(),
        "required_action": None if outcome == "PASS" else "FIX_TECHNICAL",
        "schema_version": "signal-parity-result-v1",
    }
    payload = _canonical(document)
    return SignalParityResult(
        candidate_id=candidate_id,
        outcome=outcome,
        reason_code=reason_code,
        required_action=None if outcome == "PASS" else "FIX_TECHNICAL",
        mismatch_index=mismatch_index,
        decisions=decisions,
        canonical_bytes=payload,
        artifact_sha256=sha256(payload).hexdigest(),
    )


def _verified_parity_decisions(
    parity: SignalParityResult,
    candidate_id: str,
) -> tuple[FamilyDecision, ...]:
    if parity.outcome != "PASS" or parity.required_action is not None:
        raise CandidateBacktestError("signal parity gate did not pass")
    if parity.candidate_id != candidate_id:
        raise CandidateBacktestError("signal parity candidate_id mismatch")
    if not isinstance(parity.decisions, tuple) or not all(
        isinstance(item, FamilyDecision) for item in parity.decisions
    ):
        raise CandidateBacktestError("signal parity decisions are invalid")
    try:
        return load_signal_parity_result(
            parity.canonical_bytes,
            candidate_id=candidate_id,
            candidate_signal_count=len(parity.decisions),
            recomputed_decisions=parity.decisions,
            artifact_sha256=parity.artifact_sha256,
        ).decisions
    except CandidateBacktestError as error:
        raise CandidateBacktestError("signal parity artifact content mismatch") from error


def run_signal_parity_gate(
    candidate_path: Path,
    catalog_path: Path,
) -> SignalParityResult:
    """Independently recompute Candidate v2 signals from canonical closed bars."""
    candidate, candidate_id = load_pybroker_candidate(candidate_path)
    if candidate.get("schema_version") != "pybroker-candidate-v2":
        raise CandidateBacktestError("signal parity gate requires Candidate v2")
    rows = candidate.get("signals")
    if not isinstance(rows, list):
        raise CandidateBacktestError("validated Candidate v2 signals are invalid")
    try:
        source_bars = validated_candidate_source_bars(candidate, Path(catalog_path))
        strategy = _mapping(candidate["strategy"], "candidate strategy")
        family_id = strategy.get("family_id")
        family_version = strategy.get("family_version")
        parameters = strategy.get("parameters")
        if (
            not isinstance(family_id, str)
            or not isinstance(family_version, str)
            or not isinstance(parameters, dict)
        ):
            raise CandidateBacktestError("validated Candidate v2 strategy is invalid")
        recomputed = _recompute_family_decisions(candidate, source_bars, parameters)
    except (ArithmeticError, FamilyKernelError, LookupError, OSError, RuntimeError, ValueError) as error:
        return _parity_result(
            candidate_id=candidate_id,
            outcome="ERROR",
            reason_code="SIGNAL_PARITY_RECOMPUTE_FAILED",
            mismatch_index=None,
            decisions=(),
            candidate_signal_count=len(rows),
            detail=f"{type(error).__name__}: {error}",
        )

    decisions = tuple(recomputed)
    expected_rows = [asdict(item) for item in decisions]
    mismatch_index: int | None = None
    detail: str | None = None
    if len(rows) != len(expected_rows):
        mismatch_index = min(len(rows), len(expected_rows))
        detail = f"sequence length differs: candidate={len(rows)}, recomputed={len(expected_rows)}"
    else:
        for index, (candidate_row, expected_row) in enumerate(
            zip(rows, expected_rows, strict=True)
        ):
            if candidate_row != expected_row:
                mismatch_index = index
                if isinstance(candidate_row, dict):
                    changed = sorted(
                        key
                        for key in set(candidate_row) | set(expected_row)
                        if candidate_row.get(key) != expected_row.get(key)
                    )
                    detail = "fields differ: " + ",".join(changed)
                else:
                    detail = "candidate signal is not an object"
                break
    if mismatch_index is not None:
        return _parity_result(
            candidate_id=candidate_id,
            outcome="ERROR",
            reason_code="SIGNAL_PARITY_MISMATCH",
            mismatch_index=mismatch_index,
            decisions=decisions,
            candidate_signal_count=len(rows),
            detail=detail,
        )
    return _parity_result(
        candidate_id=candidate_id,
        outcome="PASS",
        reason_code="SIGNAL_PARITY_MATCH",
        mismatch_index=None,
        decisions=decisions,
        candidate_signal_count=len(rows),
        detail=None,
    )


def _signals(candidate: dict[str, JsonValue]) -> list[_Signal]:
    rows = candidate["signals"]
    if not isinstance(rows, list):
        raise CandidateBacktestError("validated signals is not an array")
    signals: list[_Signal] = []
    for value in rows:
        row = _mapping(value, "candidate signal")
        match row["intent"]:  # noqa: E501  # noqa: MATCH_OK — validated external JSON.
            case "LONG":
                intent: Intent = "LONG"
            case "FLAT":
                intent = "FLAT"
            case unexpected:
                raise CandidateBacktestError(f"unsupported intent: {unexpected}")
        timestamp = row["ts_event_ns"]
        if isinstance(timestamp, bool) or not isinstance(timestamp, int):
            raise CandidateBacktestError("validated signal timestamp is not an integer")
        signals.append(_Signal(intent, timestamp))
    return signals


class _CandidateReplayStrategy(Strategy):
    """Mutable Nautilus strategy state for one LONG/FLAT event replay."""

    def __init__(self) -> None:
        super().__init__()
        self._plan: _ReplayPlan | None = None
        self._cursor = 0
        self._desired: Intent = "FLAT"
        self.deduped_signal_count = 0
        self.boundary_flattened = False
        self.actions: dict[str, _Action] = {}
        self._pending: list[tuple[int, _Signal]] = []

    def configure(self, plan: _ReplayPlan) -> None:
        self._plan = plan

    def on_start(self) -> None:
        if self._plan is None:
            raise CandidateBacktestError("candidate strategy is not configured")
        self.subscribe_bars(self._plan.bar_type)

    def _submit(self, action: _Action) -> None:
        if self.trader_id is None or self._plan is None:
            raise CandidateBacktestError("candidate strategy is not configured")
        client_order_id = self.order_factory.generate_client_order_id()
        order = MarketOrder(
            trader_id=self.trader_id,
            strategy_id=self.strategy_id,
            instrument_id=self._plan.instrument_id,
            client_order_id=client_order_id,
            order_side=OrderSide.BUY if action.intent == "LONG" else OrderSide.SELL,
            quantity=self._plan.quantity,
            init_id=UUID4(),
            ts_init=self.clock.timestamp_ns(),
            time_in_force=TimeInForce.GTC,
            reduce_only=action.intent == "FLAT",
            quote_quantity=False,
            contingency_type=ContingencyType.NO_CONTINGENCY,
        )
        self.actions[str(client_order_id)] = action
        self.submit_order(order)

    def on_bar(self, bar: Bar) -> None:
        if self._plan is None:
            raise CandidateBacktestError("candidate strategy is not configured")
        due: list[_Signal] = []
        pending: list[tuple[int, _Signal]] = []
        for remaining, signal in self._pending:
            if remaining == 0:
                due.append(signal)
            else:
                pending.append((remaining - 1, signal))
        self._pending = pending
        changed: _Signal | None = None
        while (
            self._cursor < len(self._plan.signals)
            and self._plan.signals[self._cursor].ts_event_ns < bar.ts_event
        ):
            signal = self._plan.signals[self._cursor]
            self._cursor += 1
            if signal.intent == self._desired:
                self.deduped_signal_count += 1
            else:
                self._desired = signal.intent
                changed = signal
        is_long = self.portfolio.is_net_long(self._plan.instrument_id)
        if bar.ts_event == self._plan.boundary_ns:
            self._pending.clear()
            if is_long:
                source_ns = (
                    due[-1].ts_event_ns
                    if due and due[-1].intent == "FLAT"
                    else changed.ts_event_ns
                    if changed is not None and changed.intent == "FLAT"
                    else None
                )
                self.boundary_flattened = source_ns is None
                self._submit(_Action("FLAT", source_ns, bar.ts_event))
            return
        if changed is not None:
            if self._plan.delay_bars > 0:
                self._pending.append((self._plan.delay_bars - 1, changed))
            else:
                due.append(changed)
        for signal in due:
            if signal.intent == "LONG" and not is_long:
                self._submit(_Action("LONG", signal.ts_event_ns, bar.ts_event))
                is_long = True
            elif signal.intent == "FLAT" and is_long:
                self._submit(_Action("FLAT", signal.ts_event_ns, bar.ts_event))
                is_long = False


def _funding_data(
    observations: list[FundingObservation],
    bars: list[Bar],
    instrument_id: InstrumentId,
    funding_multiplier: Decimal = Decimal("1"),
) -> tuple[list[MarkPriceUpdate | FundingRateUpdate], dict[int, tuple[FundingObservation, Price, str]]]:
    events: list[MarkPriceUpdate | FundingRateUpdate] = []
    evidence: dict[int, tuple[FundingObservation, Price, str]] = {}
    for observation in observations:
        effective_observation = replace(
            observation,
            rate=observation.rate * funding_multiplier,
        )
        if observation.mark_price is not None:
            mark_price = Price.from_str(str(observation.mark_price))
            price_source = FUNDING_PRICE_SOURCE
        else:
            prior = [bar for bar in bars if bar.ts_event < observation.funding_time_ns]
            if not prior:
                raise CandidateBacktestError(
                    f"modeled Funding has no bar fallback at {observation.funding_time_ns}",
                )
            # ponytail: This bar-close mark is a modeled-Funding ceiling, not market
            # truth. Upgrade to official historical marks before claimable evaluation.
            mark_price = prior[-1].close
            price_source = "bar_close_fallback"
        timestamp = observation.funding_time_ns
        # ponytail: Nautilus has no explicit tie-break priority for a Funding event
        # and a bar at T, so settle at T-1ns against strictly pre-T evidence while
        # retaining the official T in the verdict. Remove when event priority exists.
        settlement_timestamp = timestamp - 1
        events.append(
            MarkPriceUpdate(
                instrument_id,
                mark_price,
                settlement_timestamp,
                settlement_timestamp,
            ),
        )
        events.append(
            FundingRateUpdate(
                instrument_id,
                effective_observation.rate,
                settlement_timestamp,
                settlement_timestamp,
                interval=480,
                next_funding_ns=settlement_timestamp,
            ),
        )
        evidence[settlement_timestamp] = (effective_observation, mark_price, price_source)
    return events, evidence


def _funding_symbols(funding_path: Path) -> tuple[str, ...]:
    """Discover manifest coverage, then let the canonical reader verify every byte."""
    try:
        pointer = json.loads((funding_path / READY_POINTER).read_bytes())
        generation = pointer["generation"]
        if (
            not isinstance(generation, str)
            or len(generation) != 64
            or any(character not in "0123456789abcdef" for character in generation)
        ):
            raise CandidateBacktestError("invalid FundingObservation generation")
        manifest = json.loads(
            (funding_path / GENERATIONS_DIRECTORY / generation / MANIFEST_NAME).read_bytes(),
        )
        entries = manifest["observations"]
        if not isinstance(entries, list):
            raise CandidateBacktestError("invalid FundingObservation manifest coverage")
        symbols = tuple(
            sorted(
                entry["instrument_id"].removesuffix("-PERP.BINANCE")
                for entry in entries
                if isinstance(entry, dict)
                and isinstance(entry.get("instrument_id"), str)
                and entry["instrument_id"].endswith("-PERP.BINANCE")
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError) as error:
        raise CandidateBacktestError("FundingObservation manifest is unavailable or invalid") from error
    if not symbols or len(symbols) != len(entries) or len(symbols) != len(set(symbols)):
        raise CandidateBacktestError("invalid FundingObservation manifest coverage")
    return symbols


def _account_total(event) -> Decimal:
    balances = getattr(event, "balances")
    return next(
        balance.total.as_decimal()
        for balance in balances
        if balance.currency == _USDT
    )


def _historical_instrument(
    instrument: CryptoPerpetual,
    bars: list[Bar],
    fee_multiplier: Decimal = Decimal("1"),
) -> CryptoPerpetual:
    increment = instrument.price_increment.as_decimal()
    prices = (
        price.as_decimal()
        for bar in bars
        for price in (bar.open, bar.high, bar.low, bar.close)
    )
    if all(price % increment == 0 for price in prices) and fee_multiplier == Decimal("1"):
        return instrument
    historical_increment = Decimal(1).scaleb(-instrument.price_precision)
    # ponytail: use the stored bar precision until Catalog carries versioned tick sizes.
    definition = instrument.to_dict()
    definition["price_increment"] = str(historical_increment)
    if fee_multiplier != Decimal("1"):
        definition["maker_fee"] = str(instrument.maker_fee * fee_multiplier)
        definition["taker_fee"] = str(instrument.taker_fee * fee_multiplier)
    return CryptoPerpetual.from_dict(definition)


def run_candidate_backtest(request: CandidateBacktestRequest) -> CandidateBacktestResult:
    """Evaluate one validated PyBroker candidate with the real Nautilus engine."""
    candidate, candidate_id = load_pybroker_candidate(request.candidate_path)
    request_bounds = _validate_candidate_request_identity(request, candidate)
    policy = _load_policy(request.policy_path)
    catalog_path = Path(request.catalog_path)
    catalog = ParquetDataCatalog(str(catalog_path))
    source_bars = _source_bars(candidate, catalog, catalog_path)
    bars = [bar for bar in source_bars if bar.ts_event >= policy.historical_start_ns]
    if candidate.get("schema_version") == "pybroker-candidate-v2":
        parity = request.signal_parity
        if parity is None:
            raise CandidateBacktestError("Candidate v2 requires a passed signal parity gate")
        base_decisions = _verified_parity_decisions(parity, candidate_id)
        if request.strategy_parameters_override is None:
            replay_decisions = base_decisions
        else:
            replay_decisions = _recompute_family_decisions(
                candidate,
                source_bars,
                request.strategy_parameters_override,
            )
        replay_signals = [
            _Signal(item.target_intent, item.ts_event_ns)
            for item in replay_decisions
        ]
    else:
        if request.strategy_parameters_override is not None:
            raise CandidateBacktestError(
                "strategy_parameters_override requires Candidate v2",
            )
        replay_signals = _signals(candidate)
    if request_bounds is not None:
        evaluation_start_ns, evaluation_end_ns, data_as_of_ns = request_bounds
        bars = [
            bar for bar in bars
            if evaluation_start_ns <= bar.ts_event <= evaluation_end_ns
            and bar.ts_event <= data_as_of_ns
        ]
        if not bars:
            raise CandidateBacktestError("request UTC bounds contain no catalog bars")
    if not bars:
        raise CandidateBacktestError("catalog has no bars at or after historical_start")
    instrument_id_text = candidate["instrument_id"]
    bar_type_text = candidate["bar_type"]
    if not isinstance(instrument_id_text, str) or not isinstance(bar_type_text, str):
        raise CandidateBacktestError("validated candidate identity is invalid")
    instruments = catalog.instruments([instrument_id_text])
    match instruments:  # noqa: E501  # noqa: MATCH_OK — rc2 open instrument hierarchy.
        case [CryptoPerpetual() as instrument]:
            pass
        case _:
            raise CandidateBacktestError("catalog must contain one CryptoPerpetual instrument")
    instrument = _historical_instrument(instrument, bars, request.fee_multiplier)

    symbol = instrument_id_text.partition("-PERP.")[0]
    funding_symbols = _funding_symbols(request.funding_path)
    if symbol not in funding_symbols:
        raise CandidateBacktestError(f"canonical Funding does not cover {symbol}")
    observations = read_funding_observations(
        request.funding_path,
        symbols=funding_symbols,
    )[symbol]
    evaluated_observations = [
        item
        for item in observations
        if bars[0].ts_event < item.funding_time_ns <= bars[-1].ts_event
    ]
    funding_data, funding_evidence = _funding_data(
        evaluated_observations,
        bars,
        instrument.id,
        request.funding_multiplier,
    )
    strategy = _CandidateReplayStrategy()
    strategy.configure(
        _ReplayPlan(
            instrument.id,
            BarType.from_str(bar_type_text),
            policy.quantity,
            replay_signals,
            bars[-1].ts_event,
            request.delay_bars,
        ),
    )
    engine = BacktestEngine(
        BacktestEngineConfig(
            run_analysis=False,
            logging=LoggerConfig(stdout_level=LogLevel.OFF),
        ),
    )
    try:
        fill_model = (
            OneTickSlippageFillModel(prob_slippage=1.0, random_seed=42)
            if request.slippage_model == "one_tick"
            else None
        )
        engine.add_venue(
            venue=_VENUE,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            starting_balances=[Money(policy.starting_balance, _USDT)],
            base_currency=_USDT,
            fill_model=fill_model,
        )
        engine.add_instrument(instrument)
        engine.add_strategy(strategy)
        engine.add_data([*funding_data, *bars])
        engine.run()

        account = engine.cache.account_for_venue(_VENUE)
        if account is None:
            raise CandidateBacktestError("Nautilus account was not created")
        orders = sorted(engine.cache.orders(), key=lambda item: item.last_event.ts_event)
        fills: list[dict[str, JsonValue]] = []
        fee_total = Decimal()
        gross_result = Decimal()
        for order in orders:
            fill = order.last_event
            if not hasattr(fill, "commission"):
                raise CandidateBacktestError(f"Nautilus order was not filled: {order.status}")
            action = strategy.actions[str(order.client_order_id)]
            commission = fill.commission.as_decimal()
            fee_total -= commission
            notional = fill.last_px.as_decimal() * fill.last_qty.as_decimal()
            gross_result += notional if str(fill.order_side) == "SELL" else -notional
            fills.append(
                {
                    "action_ts_event_ns": action.action_ns,
                    "commission": _money(-commission),
                    "fill_ts_event_ns": fill.ts_event,
                    "intent": action.intent,
                    "quantity": str(fill.last_qty),
                    "source_signal_ts_event_ns": action.source_signal_ns,
                },
            )
        if engine.cache.positions_open():
            raise CandidateBacktestError("evaluation boundary did not flatten the position")

        account_events = list(account.events)
        funding_events: list[dict[str, JsonValue]] = []
        funding_total = Decimal()
        for timestamp, (observation, mark_price, price_source) in funding_evidence.items():
            net_quantity = sum(
                (
                    fill.last_qty.as_decimal()
                    if str(fill.order_side) == "BUY"
                    else -fill.last_qty.as_decimal()
                    for fill in (order.last_event for order in orders)
                    if fill.ts_event < timestamp
                ),
                Decimal(),
            )
            settlements = [
                event
                for event in account_events
                if event.ts_event == timestamp and event.is_reported
            ]
            if net_quantity == 0:
                if settlements:
                    raise CandidateBacktestError(f"flat Funding changed account at {timestamp}")
                continue
            if len(settlements) != 1:
                raise CandidateBacktestError(f"Funding did not settle exactly once at {timestamp}")
            index = account_events.index(settlements[0])
            if index == 0:
                raise CandidateBacktestError(f"Funding settlement lacks prior account event at {timestamp}")
            amount = _account_total(settlements[0]) - _account_total(account_events[index - 1])
            funding_total += amount
            funding_events.append(
                {
                    "amount": _money(amount),
                    "mark_price": str(mark_price),
                    "price_source": price_source,
                    "rate": str(observation.rate),
                    "truth_status": observation.truth_status,
                    "ts_event_ns": observation.funding_time_ns,
                },
            )

        ending_balance = account.balance_total(_USDT).as_decimal()
        account_delta = ending_balance - policy.starting_balance
        if gross_result + fee_total + funding_total != account_delta:
            raise CandidateBacktestError("fees, Funding, and gross result do not reconcile")
        balances = [policy.starting_balance, *(_account_total(event) for event in account_events)]
        peak = balances[0]
        realized_drawdown = Decimal()
        for balance in balances:
            peak = max(peak, balance)
            realized_drawdown = max(realized_drawdown, peak - balance)

        modeled_count = sum(item.truth_status == MODELED_FUNDING for item in evaluated_observations)
        official_count = sum(item.truth_status == OFFICIAL for item in evaluated_observations)
        truth_status = (
            "official"
            if official_count and not modeled_count
            else "modeled_funding"
            if modeled_count and not official_count
            else "mixed"
            if modeled_count and official_count
            else "missing"
        )
        formal_cost_policy = (
            request.fee_multiplier != Decimal("1")
            or request.funding_multiplier != Decimal("1")
            or request.delay_bars != 0
            or request.slippage_model != "none"
            or request.cost_policy_id is not None
        )
        slippage_status = (
            "modeled_one_tick"
            if request.slippage_model == "one_tick"
            else "modeled"
            if formal_cost_policy
            else policy.slippage_status
        )
        reason_codes = [
            "POSITIVE_NET_RESEARCH_ONLY" if account_delta > 0 else "NON_POSITIVE_NET_RESULT",
        ]
        if slippage_status == "unmodeled":
            reason_codes.append("UNMODELED_SLIPPAGE")
        if truth_status != "official":
            reason_codes.append(f"FUNDING_TRUTH_{truth_status.upper()}")
        source = _mapping(candidate["source"], "candidate source")
        runtime = _mapping(candidate["runtime"], "candidate runtime")
        verdict: dict[str, JsonValue] = {
            "candidate_id": candidate_id,
            "code_commit": request.code_commit,
            "decision": "RETAIN_FOR_RESEARCH" if account_delta > 0 else "REVISE",
            "ending_balance": _money(ending_balance),
            "ending_position": "FLAT",
            "evaluation_windows": {
                "actual_first_ts_event_ns": bars[0].ts_event,
                "actual_last_ts_event_ns": bars[-1].ts_event,
                "configured_historical_start": policy.historical_start,
                "first_official_funding_ns": next(
                    (item.funding_time_ns for item in observations if item.truth_status == OFFICIAL),
                    None,
                ),
            },
            "execution": {
                "boundary_flattened": strategy.boundary_flattened,
                "deduped_signal_count": strategy.deduped_signal_count,
                "fill_count": len(fills),
                "fills": fills,
                "fixed_quantity_btc": str(policy.quantity),
                "order_count": len(orders),
                "signal_timing": policy.signal_timing,
                "slippage_status": slippage_status,
                "trade_count": sum(fill["intent"] == "FLAT" for fill in fills),
            },
            "experiment_id": request.experiment_id,
            "fees": {
                "maker_rate": str(instrument.maker_fee),
                "source": "nautilus_instrument_metadata",
                "taker_rate": str(instrument.taker_fee),
                "total": _money(fee_total),
            },
            "funding": {
                "events": funding_events,
                "same_timestamp_order": "mark_then_funding",
                "source": "canonical_funding_observation_v1",
                "total": _money(funding_total),
                "truth_counts": {
                    "missing_mark": sum(item.mark_price is None for item in evaluated_observations),
                    "modeled_funding": modeled_count,
                    "official": official_count,
                },
                "truth_status": truth_status,
            },
            "gross_trading_result": _money(gross_result),
            "hypothesis_id": request.hypothesis_id,
            "net_account_delta": _money(account_delta),
            "open_position_count": 0,
            "performance_claimable": (
                truth_status == "official" and slippage_status != "unmodeled"
            ),
            "policy_decision_version": policy.decision_version,
            "realized_balance_drawdown": _money(realized_drawdown),
            "reason_codes": reason_codes,
            "runtime_versions": {
                "nautilus_trader": nautilus_trader.__version__,
                "nautilus_python": platform.python_version(),
                "pybroker": str(runtime["pybroker_version"]),
                "research_python": str(runtime["python_version"]),
            },
            "schema_version": "nautilus-verdict-v1",
            "source": {
                "first_ts_event_ns": source["first_ts_event_ns"],
                "last_ts_event_ns": source["last_ts_event_ns"],
                "row_count": source["row_count"],
                "sha256": source["sha256"],
            },
            "starting_balance": _money(policy.starting_balance),
            "status": "EVALUATED",
            "strategy_id": request.strategy_id,
            "accounting_reconciled": True,
        }
        if candidate.get("schema_version") == "pybroker-candidate-v2":
            parity = request.signal_parity
            if parity is None:  # Defensive: the gate check above already rejects this.
                raise CandidateBacktestError("Candidate v2 parity result disappeared")
            verdict["signal_parity"] = {
                "artifact_sha256": parity.artifact_sha256,
                "outcome": parity.outcome,
                "reason_code": parity.reason_code,
            }
        if formal_cost_policy:
            cost_policy_document = _cost_policy_document(
                request.fee_multiplier,
                request.funding_multiplier,
                request.delay_bars,
                request.slippage_model,
            )
            verdict["cost_policy"] = {
                **cost_policy_document,
                "cost_policy_id": _canonical_cost_policy_id(
                    request.fee_multiplier,
                    request.funding_multiplier,
                    request.delay_bars,
                    request.slippage_model,
                ),
            }
        verdict["canonical_result_hash"] = sha256(_canonical(verdict)).hexdigest()
        payload = _canonical(verdict)
        return CandidateBacktestResult(verdict, payload, sha256(payload).hexdigest())
    finally:
        engine.dispose()
