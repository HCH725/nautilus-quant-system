# noqa: E501  # noqa: SIZE_OK — Card 2 keeps the canonical campaign boundary in one module.
from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum, unique
from hashlib import sha256
from itertools import product
import json
import math
from pathlib import Path
import re
from typing import Final, Literal, Protocol

from .strategy_families import DEFAULT_REGISTRY, FamilyKernelError


type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]

_SHA256 = re.compile(r"[0-9a-f]{64}")
_POLICY_FIELDS: Final = frozenset(
    {
        "max_provisional_drawdown",
        "max_turnover",
        "minimum_signal_count",
        "minimum_trade_count",
        "policy_version",
        "reject_no_signal",
        "schema_version",
    },
)
_RESULT_FIELDS: Final = frozenset(
    {"candidate_id", "provisional_metrics", "schema_version", "truth_status"},
)
_SCREEN_RESULT_FIELDS: Final = frozenset(
    {
        "candidate_id",
        "provisional_metrics",
        "schema_version",
        "screen_outcome",
        "screen_policy",
        "screen_policy_id",
        "screen_reason_codes",
        "truth_status",
    },
)
_METRIC_FIELDS: Final = frozenset(
    {"max_drawdown", "signal_count", "total_return", "trade_count", "turnover"},
)
_CAMPAIGN_FIELDS: Final = frozenset(
    {
        "approved_bar_types",
        "approved_instruments",
        "data_as_of_ns",
        "family_id",
        "family_version",
        "generation_budget",
        "maximum_candidates",
        "parameter_search_policy_id",
        "schema_version",
        "screen_policy_id",
        "search_space",
        "seed",
    },
)
_INSTRUMENT_ID: Final = "BTCUSDT-PERP.BINANCE"
_BAR_TYPE: Final = "BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL"
_SQLITE_INTEGER_MAX: Final = (1 << 63) - 1


class StrategyCampaignError(ValueError):
    """Raised when a Card 2 research or policy boundary is invalid."""


@unique
class TerminalStatus(StrEnum):
    """The only terminal states a generated campaign trial may have."""

    DUPLICATE_SUPPRESSED = "DUPLICATE_SUPPRESSED"
    TECHNICAL_INVALID = "TECHNICAL_INVALID"
    SCREEN_REJECTED = "SCREEN_REJECTED"
    SURVIVED = "SURVIVED"


@dataclass(frozen=True, slots=True)
class CampaignBudgetExceeded(StrategyCampaignError):
    """Raised before a campaign can read data or create ledger state."""

    generated_count: int
    maximum_candidates: int

    def __str__(self) -> str:
        return (
            f"campaign generates {self.generated_count} candidates above budget "
            f"{self.maximum_candidates}"
        )


@dataclass(frozen=True, slots=True)
class CampaignPreflight:
    """Frozen identities observed immediately before campaign execution."""

    screen_policy_id: str
    data_as_of_ns: int
    data_source_id: str
    technical_reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CampaignSpec:
    """Canonical bounded campaign input owned by the deterministic runner."""

    family_id: str
    family_version: str
    search_space: Mapping[str, tuple[JsonValue, ...]]
    approved_instruments: tuple[str, ...]
    approved_bar_types: tuple[str, ...]
    parameter_search_policy_id: str
    seed: int
    data_as_of_ns: int
    generation_budget: int
    maximum_candidates: int
    screen_policy_id: str

    @property
    def campaign_id(self) -> str:
        return sha256(canonical_json(self.document)).hexdigest()

    @property
    def document(self) -> dict[str, JsonValue]:
        return {
            "approved_bar_types": list(self.approved_bar_types),
            "approved_instruments": list(self.approved_instruments),
            "data_as_of_ns": self.data_as_of_ns,
            "family_id": self.family_id,
            "family_version": self.family_version,
            "generation_budget": self.generation_budget,
            "maximum_candidates": self.maximum_candidates,
            "parameter_search_policy_id": self.parameter_search_policy_id,
            "schema_version": "strategy-campaign-v1",
            "screen_policy_id": self.screen_policy_id,
            "search_space": {
                key: list(self.search_space[key]) for key in sorted(self.search_space)
            },
            "seed": self.seed,
        }


@dataclass(frozen=True, slots=True)
class CampaignAttempt:
    """One deterministic attempt identified by the canonical strategy ID."""

    campaign_id: str
    ordinal: int
    strategy_id: str | None
    parameters: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class TrialEvidence:
    """Executor result separated from campaign membership and identity."""

    terminal_status: TerminalStatus
    execution_started: bool
    reason_codes: tuple[str, ...]
    experiment_id: str | None = None
    candidate_id: str | None = None


@dataclass(frozen=True, slots=True)
class CampaignTrial:
    """Immutable readback of one generated campaign membership row."""

    campaign_id: str
    ordinal: int
    strategy_id: str | None
    candidate_id: str | None
    evidence: TrialEvidence


@dataclass(frozen=True, slots=True)
class CampaignTechnicalError(StrategyCampaignError):
    """Typed technical failure that cannot become a screen rejection."""

    reason_code: str
    execution_started: bool
    experiment_id: str | None = None
    candidate_id: str | None = None

    def __str__(self) -> str:
        return self.reason_code


class StrategyLedgerCampaignPort(Protocol):
    """The part of StrategyLedger used by the campaign runner."""

    def record_campaign(self, spec: CampaignSpec, preflight: CampaignPreflight) -> None: ...

    def record_campaign_trial(
        self,
        attempt: CampaignAttempt,
        evidence: TrialEvidence,
    ) -> None: ...

    def record_campaign_trials(
        self,
        trials: tuple[tuple[CampaignAttempt, TrialEvidence], ...],
    ) -> None: ...

    def campaign_trials(self, campaign_id: str) -> tuple[CampaignTrial, ...]: ...


class StrategyCampaignExecutor(Protocol):
    """Callable boundary for one executable strategy-loop attempt."""

    def __call__(self, attempt: CampaignAttempt) -> TrialEvidence: ...


@dataclass(frozen=True, slots=True)
class ScreenPolicy:
    """Frozen provisional thresholds selected before observing campaign results."""

    policy_id: str
    policy_version: str
    minimum_trade_count: int
    minimum_signal_count: int
    max_provisional_drawdown: float
    max_turnover: float
    reject_no_signal: bool


@dataclass(frozen=True, slots=True)
class ProvisionalMetrics:
    """Finite research-only metrics; none are Nautilus accounting truth."""

    trade_count: int
    signal_count: int
    total_return: float
    max_drawdown: float
    turnover: float


@dataclass(frozen=True, slots=True)
class ResearchResultV2:
    """Validated provisional result emitted by the Nautilus-native research step."""

    candidate_id: str
    metrics: ProvisionalMetrics


@dataclass(frozen=True, slots=True)
class ScreenDecision:
    """Deterministic screen outcome and ordered reason codes."""

    outcome: Literal["PASSED", "SCREEN_REJECTED"]
    reason_codes: tuple[str, ...]


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    value: dict[str, JsonValue] = {}
    for key, item in pairs:
        if key in value:
            raise StrategyCampaignError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> JsonValue:
    raise StrategyCampaignError(f"JSON value must be finite: {value}")


def canonical_json(value: JsonValue) -> bytes:
    try:
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
    except (TypeError, ValueError) as error:
        raise StrategyCampaignError("campaign value must be finite plain JSON") from error


def _plain_json(value: JsonValue, field: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
    if isinstance(value, list):
        for item in value:
            _plain_json(item, field)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise StrategyCampaignError(f"{field} keys must be strings")
            _plain_json(item, field)
        return
    raise StrategyCampaignError(f"{field} must contain only finite plain JSON")


def _mapping(value: JsonValue, fields: frozenset[str], name: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict) or set(value) != fields:
        raise StrategyCampaignError(f"invalid {name} fields")
    return value


def _nonnegative_integer(value: JsonValue, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= _SQLITE_INTEGER_MAX
    ):
        raise StrategyCampaignError(
            f"{name} must be a non-negative signed 64-bit integer",
        )
    return value


def _finite_nonnegative(value: JsonValue, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StrategyCampaignError(f"{name} must be numeric")
    try:
        normalized = float(value)
    except (OverflowError, ValueError) as error:
        raise StrategyCampaignError(f"{name} must be finite and non-negative") from error
    if not math.isfinite(normalized) or normalized < 0:
        raise StrategyCampaignError(f"{name} must be finite and non-negative")
    return normalized


def _finite_number(value: JsonValue, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StrategyCampaignError(f"{name} must be numeric")
    try:
        normalized = float(value)
    except (OverflowError, ValueError) as error:
        raise StrategyCampaignError(f"{name} must be finite") from error
    if not math.isfinite(normalized):
        raise StrategyCampaignError(f"{name} must be finite")
    return normalized


def _content_id(value: JsonValue, name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise StrategyCampaignError(f"{name} must be lowercase SHA-256")
    return value


def _string_tuple(value: JsonValue, name: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
        or len(value) != len(set(value))
    ):
        raise StrategyCampaignError(f"{name} must be a non-empty unique string list")
    return tuple(value)


def _screen_policy_document(policy: ScreenPolicy) -> dict[str, JsonValue]:
    return {
        "max_provisional_drawdown": policy.max_provisional_drawdown,
        "max_turnover": policy.max_turnover,
        "minimum_signal_count": policy.minimum_signal_count,
        "minimum_trade_count": policy.minimum_trade_count,
        "policy_version": policy.policy_version,
        "reject_no_signal": policy.reject_no_signal,
        "schema_version": "strategy-research-policy-v1",
    }


def _parse_screen_policy(value: JsonValue) -> ScreenPolicy:
    root = _mapping(value, _POLICY_FIELDS, "screen policy")
    if root["schema_version"] != "strategy-research-policy-v1":
        raise StrategyCampaignError("unsupported screen policy schema")
    policy_version = root["policy_version"]
    if not isinstance(policy_version, str) or not policy_version:
        raise StrategyCampaignError("screen policy version must be non-empty")
    reject_no_signal = root["reject_no_signal"]
    if not isinstance(reject_no_signal, bool):
        raise StrategyCampaignError("reject_no_signal must be boolean")
    payload = canonical_json(root)
    return ScreenPolicy(
        policy_id=sha256(payload).hexdigest(),
        policy_version=policy_version,
        minimum_trade_count=_nonnegative_integer(
            root["minimum_trade_count"], "minimum_trade_count"
        ),
        minimum_signal_count=_nonnegative_integer(
            root["minimum_signal_count"], "minimum_signal_count"
        ),
        max_provisional_drawdown=_finite_nonnegative(
            root["max_provisional_drawdown"], "max_provisional_drawdown"
        ),
        max_turnover=_finite_nonnegative(root["max_turnover"], "max_turnover"),
        reject_no_signal=reject_no_signal,
    )


def load_screen_policy(path: Path) -> ScreenPolicy:
    """Load the canonical screen policy and derive its content identity."""
    payload = Path(path).read_bytes()
    try:
        value: JsonValue = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StrategyCampaignError("screen policy must be UTF-8 JSON") from error
    if payload != canonical_json(value):
        raise StrategyCampaignError("screen policy must use canonical JSON encoding")
    return _parse_screen_policy(value)


def load_research_result_v2(payload: bytes) -> ResearchResultV2:
    """Parse one finite, canonical V2 result from isolated research stdout."""
    try:
        value: JsonValue = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StrategyCampaignError("research result must be UTF-8 JSON") from error
    root = _mapping(value, _RESULT_FIELDS, "research result v2")
    if root["schema_version"] != "research-result-v2":
        raise StrategyCampaignError("unsupported research result schema")
    if root["truth_status"] != "provisional":
        raise StrategyCampaignError("research result truth_status must be provisional")
    candidate_id = _content_id(root["candidate_id"], "candidate_id")
    metrics = _mapping(root["provisional_metrics"], _METRIC_FIELDS, "provisional_metrics")
    parsed = ProvisionalMetrics(
        trade_count=_nonnegative_integer(metrics["trade_count"], "trade_count"),
        signal_count=_nonnegative_integer(metrics["signal_count"], "signal_count"),
        total_return=_finite_number(metrics["total_return"], "total_return"),
        max_drawdown=_finite_nonnegative(metrics["max_drawdown"], "max_drawdown"),
        turnover=_finite_nonnegative(metrics["turnover"], "turnover"),
    )
    if payload != canonical_json(root):
        raise StrategyCampaignError("research result must use canonical JSON encoding")
    return ResearchResultV2(candidate_id, parsed)


def screen_research_result(result: ResearchResultV2, policy: ScreenPolicy) -> ScreenDecision:
    """Apply the frozen provisional screen without invoking historical accounting."""
    reasons: list[str] = []
    metrics = result.metrics
    if policy.reject_no_signal and metrics.signal_count == 0:
        reasons.append("NO_SIGNALS")
    if metrics.trade_count < policy.minimum_trade_count:
        reasons.append("MINIMUM_TRADE_COUNT")
    if metrics.signal_count < policy.minimum_signal_count:
        reasons.append("MINIMUM_SIGNAL_COUNT")
    if metrics.max_drawdown > policy.max_provisional_drawdown:
        reasons.append("MAX_PROVISIONAL_DRAWDOWN_EXCEEDED")
    if metrics.turnover > policy.max_turnover:
        reasons.append("TURNOVER_CEILING_EXCEEDED")
    return ScreenDecision(
        outcome="SCREEN_REJECTED" if reasons else "PASSED",
        reason_codes=tuple(reasons),
    )


def screened_result_document(
    result: ResearchResultV2,
    decision: ScreenDecision,
    policy: ScreenPolicy,
) -> dict[str, JsonValue]:
    """Return the immutable persisted screen evidence derived from raw V2."""
    policy_document = _screen_policy_document(policy)
    if sha256(canonical_json(policy_document)).hexdigest() != policy.policy_id:
        raise StrategyCampaignError("screen policy identity is inconsistent")
    return {
        "candidate_id": result.candidate_id,
        "provisional_metrics": {
            "max_drawdown": result.metrics.max_drawdown,
            "signal_count": result.metrics.signal_count,
            "total_return": result.metrics.total_return,
            "trade_count": result.metrics.trade_count,
            "turnover": result.metrics.turnover,
        },
        "schema_version": "research-screen-result-v1",
        "screen_outcome": decision.outcome,
        "screen_policy": policy_document,
        "screen_policy_id": policy.policy_id,
        "screen_reason_codes": list(decision.reason_codes),
        "truth_status": "provisional",
    }


def load_screen_result_v1(payload: bytes) -> dict[str, JsonValue]:
    """Validate one canonical persisted screen artifact without changing raw V2."""
    try:
        value: JsonValue = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StrategyCampaignError("screen result must be UTF-8 JSON") from error
    root = _mapping(value, _SCREEN_RESULT_FIELDS, "research screen result v1")
    if root["schema_version"] != "research-screen-result-v1":
        raise StrategyCampaignError("unsupported screen result schema")
    raw_result: dict[str, JsonValue] = {
        "candidate_id": root["candidate_id"],
        "provisional_metrics": root["provisional_metrics"],
        "schema_version": "research-result-v2",
        "truth_status": root["truth_status"],
    }
    result = load_research_result_v2(canonical_json(raw_result))
    policy = _parse_screen_policy(root["screen_policy"])
    if _content_id(root["screen_policy_id"], "screen_policy_id") != policy.policy_id:
        raise StrategyCampaignError("screen policy identity is inconsistent")
    outcome = root["screen_outcome"]
    reasons = root["screen_reason_codes"]
    if outcome not in {"PASSED", "SCREEN_REJECTED"}:
        raise StrategyCampaignError("screen result outcome is invalid")
    if not isinstance(reasons, list) or not all(
        isinstance(reason, str) and reason for reason in reasons
    ):
        raise StrategyCampaignError("screen result reason codes are invalid")
    if len(reasons) != len(set(reasons)):
        raise StrategyCampaignError("screen result reason codes must be unique")
    if (outcome == "PASSED") == bool(reasons):
        raise StrategyCampaignError("screen result outcome and reasons conflict")
    decision = screen_research_result(result, policy)
    if outcome != decision.outcome or reasons != list(decision.reason_codes):
        raise StrategyCampaignError("screen result does not match frozen policy")
    if payload != canonical_json(root):
        raise StrategyCampaignError("screen result must use canonical JSON encoding")
    return root


def load_campaign_spec(path: Path) -> CampaignSpec:
    """Load one canonical campaign spec without executing any family code."""
    payload = Path(path).read_bytes()
    try:
        value: JsonValue = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StrategyCampaignError("campaign spec must be UTF-8 JSON") from error
    root = _mapping(value, _CAMPAIGN_FIELDS, "strategy-campaign-v1")
    if root["schema_version"] != "strategy-campaign-v1":
        raise StrategyCampaignError("unsupported campaign schema")
    family_id = root["family_id"]
    family_version = root["family_version"]
    if (
        not isinstance(family_id, str)
        or not family_id
        or not isinstance(family_version, str)
        or not family_version
    ):
        raise StrategyCampaignError("campaign family identity must be non-empty")
    raw_space = root["search_space"]
    if not isinstance(raw_space, dict) or not raw_space:
        raise StrategyCampaignError("campaign search_space must be a non-empty object")
    for key, items in raw_space.items():
        if not isinstance(key, str) or not key or not isinstance(items, list) or not items:
            raise StrategyCampaignError("campaign search_space values must be non-empty lists")
        for item in items:
            _plain_json(item, f"search_space.{key}")
    if payload != canonical_json(value):
        raise StrategyCampaignError("campaign spec must use canonical JSON encoding")
    return CampaignSpec(
        family_id=family_id,
        family_version=family_version,
        search_space={key: tuple(items) for key, items in raw_space.items()},
        approved_instruments=_string_tuple(root["approved_instruments"], "approved_instruments"),
        approved_bar_types=_string_tuple(root["approved_bar_types"], "approved_bar_types"),
        parameter_search_policy_id=_content_id(
            root["parameter_search_policy_id"], "parameter_search_policy_id"
        ),
        seed=_nonnegative_integer(root["seed"], "seed"),
        data_as_of_ns=_nonnegative_integer(root["data_as_of_ns"], "data_as_of_ns"),
        generation_budget=_nonnegative_integer(
            root["generation_budget"], "generation_budget"
        ),
        maximum_candidates=_nonnegative_integer(
            root["maximum_candidates"], "maximum_candidates"
        ),
        screen_policy_id=_content_id(root["screen_policy_id"], "screen_policy_id"),
    )


def _validate_campaign(spec: CampaignSpec) -> tuple[str, ...]:
    if not spec.family_id or not spec.family_version:
        raise StrategyCampaignError("campaign family identity must be non-empty")
    if len(spec.approved_instruments) != 1 or len(spec.approved_bar_types) != 1:
        raise StrategyCampaignError("campaign v1 requires one approved instrument and bar type")
    if spec.approved_instruments[0] != _INSTRUMENT_ID:
        raise StrategyCampaignError("campaign instrument is not approved")
    if spec.approved_bar_types[0] != _BAR_TYPE:
        raise StrategyCampaignError("campaign bar type is not approved")
    if not spec.approved_bar_types[0].startswith(f"{spec.approved_instruments[0]}-"):
        raise StrategyCampaignError("approved bar type must identify the instrument")
    _content_id(spec.parameter_search_policy_id, "parameter_search_policy_id")
    _content_id(spec.screen_policy_id, "screen_policy_id")
    _nonnegative_integer(spec.seed, "seed")
    _nonnegative_integer(spec.data_as_of_ns, "data_as_of_ns")
    _nonnegative_integer(spec.generation_budget, "generation_budget")
    _nonnegative_integer(spec.maximum_candidates, "maximum_candidates")
    keys = tuple(sorted(spec.search_space))
    if not keys:
        raise StrategyCampaignError("campaign search_space must not be empty")
    for key in keys:
        values = spec.search_space[key]
        if not key or not values:
            raise StrategyCampaignError("campaign search_space keys and values must be non-empty")
        for value in values:
            _plain_json(value, f"search_space.{key}")
    return keys


def _normalize_attempt_parameters(
    spec: CampaignSpec,
    parameters: dict[str, JsonValue],
) -> tuple[dict[str, JsonValue], str] | None:
    try:
        definition = DEFAULT_REGISTRY.resolve(spec.family_id, spec.family_version)
        normalized = definition.validate_parameters(parameters)
        _plain_json(normalized, "parameters")
    except (FamilyKernelError, TypeError, ValueError):
        return None
    strategy_document: dict[str, JsonValue] = {
        "bar_type": spec.approved_bar_types[0],
        "family_version": spec.family_version,
        "identity_schema": "strategy-id-v2",
        "instrument_id": spec.approved_instruments[0],
        "parameters": normalized,
        "strategy_family": spec.family_id,
    }
    return normalized, sha256(canonical_json(strategy_document)).hexdigest()


def expand_campaign(spec: CampaignSpec) -> tuple[CampaignAttempt, ...]:
    """Expand a bounded campaign in canonical order using strategy IDs."""
    keys = _validate_campaign(spec)
    values = tuple(spec.search_space[key] for key in keys)
    generated_count = math.prod(len(items) for items in values)
    maximum = min(spec.generation_budget, spec.maximum_candidates)
    if generated_count > maximum:
        raise CampaignBudgetExceeded(generated_count, maximum)
    attempts: list[CampaignAttempt] = []
    for ordinal, combination in enumerate(product(*values)):
        raw_parameters = dict(zip(keys, combination, strict=True))
        normalized = _normalize_attempt_parameters(spec, raw_parameters)
        strategy_id: str | None = None
        parameters = raw_parameters
        if normalized is not None:
            parameters, strategy_id = normalized
        attempts.append(
            CampaignAttempt(spec.campaign_id, ordinal, strategy_id, parameters),
        )
    return tuple(attempts)


def _validate_evidence(evidence: TrialEvidence) -> None:
    if not evidence.reason_codes or not all(evidence.reason_codes):
        raise StrategyCampaignError("campaign trial requires reason codes")
    if evidence.terminal_status is TerminalStatus.DUPLICATE_SUPPRESSED:
        raise StrategyCampaignError("executor cannot return duplicate suppression")
    if evidence.terminal_status is TerminalStatus.SCREEN_REJECTED and not evidence.execution_started:
        raise StrategyCampaignError("screen rejection requires started execution")
    if evidence.terminal_status is TerminalStatus.SURVIVED and not evidence.execution_started:
        raise StrategyCampaignError("survival requires started execution")


def _summary(
    spec: CampaignSpec,
    preflight: CampaignPreflight,
    attempts: tuple[CampaignAttempt, ...],
    trials: tuple[CampaignTrial, ...],
) -> dict[str, JsonValue]:
    statuses = Counter(trial.evidence.terminal_status.value for trial in trials)
    reasons = Counter(
        reason
        for trial in trials
        for reason in trial.evidence.reason_codes
    )
    family_counts = {
        spec.family_id: {
            "deduped": statuses.get(TerminalStatus.DUPLICATE_SUPPRESSED.value, 0),
            "executed": sum(trial.evidence.execution_started for trial in trials),
            "generated": len(attempts),
            "rejected": statuses.get(TerminalStatus.SCREEN_REJECTED.value, 0),
            "surviving": statuses.get(TerminalStatus.SURVIVED.value, 0),
            "technical_invalid": statuses.get(TerminalStatus.TECHNICAL_INVALID.value, 0),
        },
    }
    counts = {
        status.value: statuses.get(status.value, 0) for status in TerminalStatus
    }
    if sum(counts.values()) != len(attempts):
        raise StrategyCampaignError("campaign terminal census does not reconcile")
    return {
        "approved_bar_type": spec.approved_bar_types[0],
        "approved_instrument": spec.approved_instruments[0],
        "campaign_id": spec.campaign_id,
        "candidate_count": len({attempt.strategy_id for attempt in attempts if attempt.strategy_id}),
        "data_as_of_ns": spec.data_as_of_ns,
        "data_source_id": preflight.data_source_id,
        "deduped_count": counts[TerminalStatus.DUPLICATE_SUPPRESSED.value],
        "executed_count": sum(trial.evidence.execution_started for trial in trials),
        "family_count": 1,
        "family_version": spec.family_version,
        "family_counts": family_counts,
        "generated_count": len(attempts),
        "generation_budget": min(spec.generation_budget, spec.maximum_candidates),
        "rejected_count": counts[TerminalStatus.SCREEN_REJECTED.value],
        "schema_version": "strategy-cohort-summary-v1",
        "screen_policy_id": spec.screen_policy_id,
        "parameter_search_policy_id": spec.parameter_search_policy_id,
        "surviving_count": counts[TerminalStatus.SURVIVED.value],
        "technical_invalid_count": counts[TerminalStatus.TECHNICAL_INVALID.value],
        "top_reason_codes": [
            {"count": count, "reason_code": reason}
            for reason, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0]))[:5]
        ],
        "trial_count": len(trials),
        "terminal_status_counts": counts,
    }


def _stored_trials_match(
    spec: CampaignSpec,
    attempts: tuple[CampaignAttempt, ...],
    trials: tuple[CampaignTrial, ...],
) -> None:
    if len(trials) > len(attempts):
        raise StrategyCampaignError("campaign trial census exceeds generated attempts")
    for attempt, trial in zip(attempts, trials):
        if (
            trial.campaign_id != spec.campaign_id
            or trial.ordinal != attempt.ordinal
            or trial.strategy_id != attempt.strategy_id
        ):
            raise StrategyCampaignError("campaign trial readback conflict")


def _cohort_technical_evidence(
    attempts: tuple[CampaignAttempt, ...],
    evidence_by_ordinal: dict[int, TrialEvidence],
    reason_codes: tuple[str, ...],
) -> tuple[TrialEvidence, ...]:
    """Fail the whole in-memory census without erasing execution linkage."""
    if not reason_codes or not all(reason_codes):
        raise StrategyCampaignError("cohort technical failure requires reason codes")
    converted: list[TrialEvidence] = []
    for attempt in attempts:
        prior = evidence_by_ordinal.get(attempt.ordinal)
        if prior is None:
            converted.append(
                TrialEvidence(TerminalStatus.TECHNICAL_INVALID, False, reason_codes),
            )
            continue
        markers = tuple(
            reason
            for reason in prior.reason_codes
            if reason in {"DUPLICATE_CONTENT_ID", "REUSED_EXECUTION"}
        )
        converted.append(
            TrialEvidence(
                TerminalStatus.TECHNICAL_INVALID,
                prior.execution_started,
                (*reason_codes, *markers),
                prior.experiment_id,
                prior.candidate_id,
            ),
        )
    return tuple(converted)


def run_campaign(
    spec: CampaignSpec,
    *,
    ledger: StrategyLedgerCampaignPort,
    preflight: CampaignPreflight,
    execute: StrategyCampaignExecutor,
    reuse: Callable[[CampaignAttempt], TrialEvidence | None] | None = None,
    validate_stored: Callable[[CampaignTrial], None] | None = None,
    reconcile: Callable[[], None] | None = None,
) -> dict[str, JsonValue]:
    """Persist and execute a bounded campaign through an existing StrategyLedger."""
    attempts = expand_campaign(spec)
    ledger.record_campaign(spec, preflight)
    stored = ledger.campaign_trials(spec.campaign_id)
    if stored:
        _stored_trials_match(spec, attempts, stored)
        if validate_stored is not None:
            for trial in stored:
                validate_stored(trial)
        if len(stored) == len(attempts):
            if reconcile is not None:
                reconcile()
            return _summary(spec, preflight, attempts, stored)
        raise StrategyCampaignError("campaign trial census is incomplete")

    preflight_reasons = list(preflight.technical_reason_codes)
    if preflight.screen_policy_id != spec.screen_policy_id:
        preflight_reasons.append("SCREEN_POLICY_MISMATCH")
    if preflight.data_as_of_ns != spec.data_as_of_ns:
        preflight_reasons.append("DATA_AS_OF_MISMATCH")
    seen: dict[str, TrialEvidence] = {}
    evidence_by_ordinal: dict[int, TrialEvidence] = {}
    if preflight_reasons:
        cohort_evidence = _cohort_technical_evidence(
            attempts,
            evidence_by_ordinal,
            tuple(dict.fromkeys(preflight_reasons)),
        )
    else:
        cohort_reason_codes: tuple[str, ...] | None = None
        for attempt in attempts:
            prior = seen.get(attempt.strategy_id) if attempt.strategy_id is not None else None
            if prior is not None:
                evidence = TrialEvidence(
                    TerminalStatus.DUPLICATE_SUPPRESSED,
                    False,
                    ("DUPLICATE_CONTENT_ID",),
                    prior.experiment_id,
                    prior.candidate_id,
                )
            elif attempt.strategy_id is None:
                evidence = TrialEvidence(
                    TerminalStatus.TECHNICAL_INVALID,
                    False,
                    ("INVALID_FAMILY_OR_PARAMETERS",),
                )
            else:
                technical: TrialEvidence | None = None
                try:
                    prior = reuse(attempt) if reuse is not None else None
                except CampaignTechnicalError as error:
                    technical = TrialEvidence(
                        TerminalStatus.TECHNICAL_INVALID,
                        error.execution_started,
                        (error.reason_code,),
                        error.experiment_id,
                        error.candidate_id,
                    )
                    prior = None
                except Exception:
                    technical = TrialEvidence(
                        TerminalStatus.TECHNICAL_INVALID,
                        False,
                        ("CAMPAIGN_REUSE_FAILED",),
                    )
                    prior = None
                if technical is not None:
                    evidence = technical
                elif prior is not None:
                    evidence = TrialEvidence(
                        prior.terminal_status,
                        False,
                        (*prior.reason_codes, "REUSED_EXECUTION"),
                        prior.experiment_id,
                        prior.candidate_id,
                    )
                else:
                    try:
                        evidence = execute(attempt)
                        _validate_evidence(evidence)
                    except CampaignTechnicalError as error:
                        evidence = TrialEvidence(
                            TerminalStatus.TECHNICAL_INVALID,
                            error.execution_started,
                            (error.reason_code,),
                            error.experiment_id,
                            error.candidate_id,
                        )
                    except Exception:
                        evidence = TrialEvidence(
                            TerminalStatus.TECHNICAL_INVALID,
                            False,
                            ("CAMPAIGN_EXECUTOR_FAILED",),
                        )
            if attempt.strategy_id is not None:
                seen[attempt.strategy_id] = evidence
            evidence_by_ordinal[attempt.ordinal] = evidence
            if (
                evidence.terminal_status is TerminalStatus.TECHNICAL_INVALID
                and "CAMPAIGN_PREFLIGHT_DRIFT" in evidence.reason_codes
            ):
                cohort_reason_codes = ("CAMPAIGN_PREFLIGHT_DRIFT",)
                break
        if cohort_reason_codes is None and reconcile is not None:
            try:
                reconcile()
            except CampaignTechnicalError as error:
                cohort_reason_codes = (error.reason_code,)
            except Exception:
                cohort_reason_codes = ("CAMPAIGN_RECONCILIATION_FAILED",)
        cohort_evidence = (
            tuple(evidence_by_ordinal[index] for index in range(len(attempts)))
            if cohort_reason_codes is None
            else _cohort_technical_evidence(
                attempts,
                evidence_by_ordinal,
                cohort_reason_codes,
            )
        )
    ledger.record_campaign_trials(tuple(zip(attempts, cohort_evidence, strict=True)))
    stored = ledger.campaign_trials(spec.campaign_id)
    _stored_trials_match(spec, attempts, stored)
    if validate_stored is not None:
        for trial in stored:
            validate_stored(trial)
    if len(stored) != len(attempts):
        raise StrategyCampaignError("campaign trial census is incomplete")
    return _summary(spec, preflight, attempts, stored)


__all__ = (
    "CampaignAttempt",
    "CampaignBudgetExceeded",
    "CampaignPreflight",
    "CampaignSpec",
    "CampaignTechnicalError",
    "CampaignTrial",
    "ProvisionalMetrics",
    "ResearchResultV2",
    "ScreenDecision",
    "ScreenPolicy",
    "StrategyCampaignError",
    "TerminalStatus",
    "TrialEvidence",
    "canonical_json",
    "expand_campaign",
    "load_campaign_spec",
    "load_research_result_v2",
    "load_screen_result_v1",
    "load_screen_policy",
    "run_campaign",
    "screen_research_result",
    "screened_result_document",
)
