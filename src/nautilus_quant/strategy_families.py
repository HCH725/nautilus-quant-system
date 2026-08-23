from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import re
from typing import Any


KERNEL_VERSION = "strategy-family-kernel-v1"
_KERNEL_MANIFEST = {
    "families": {
        "close-vs-sma-mean-reversion-long-flat": "close-vs-sma-mean-reversion-long-flat-v1",
        "lookback-momentum-long-flat": "lookback-momentum-long-flat-v1",
    },
    "kernel_version": KERNEL_VERSION,
    "signal_identity_schema": "strategy-signal-v1",
}
KERNEL_HASH = hashlib.sha256(
    json.dumps(_KERNEL_MANIFEST, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
_SHA256 = re.compile(r"[0-9a-f]{64}")


class FamilyKernelError(ValueError):
    """Raised when the deterministic family-kernel boundary rejects input."""


@dataclass(frozen=True, slots=True)
class ClosedBar:
    ts_event_ns: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class FamilyEvaluation:
    score: float
    target_intent: str
    reason: str


@dataclass(frozen=True, slots=True)
class FamilyDecision:
    signal_id: str
    ts_event_ns: int
    score: str
    target_intent: str
    reason: str
    family_id: str
    family_version: str
    kernel_version: str
    kernel_hash: str


@dataclass(frozen=True, slots=True)
class FamilyDefinition:
    family_id: str
    family_version: str
    warmup_bars: Callable[[Mapping[str, Any]], int]
    validate_parameters: Callable[[Mapping[str, Any]], dict[str, Any]]
    evaluate: Callable[[Sequence[ClosedBar], Mapping[str, Any]], FamilyEvaluation]
    thesis: str = ""
    falsification: str = ""


class FamilyRegistry:
    """Small code-owned registry; external artifacts can only select tracked entries."""

    def __init__(self, definitions: Sequence[FamilyDefinition] = ()) -> None:
        self._definitions: dict[tuple[str, str], FamilyDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: FamilyDefinition) -> None:
        if not definition.family_id or not definition.family_version:
            raise FamilyKernelError("family identity must be non-empty")
        identity = (definition.family_id, definition.family_version)
        if identity in self._definitions:
            raise FamilyKernelError(
                f"family version already registered: {definition.family_id}@{definition.family_version}"
            )
        self._definitions[identity] = definition

    def resolve(self, family_id: str, family_version: str) -> FamilyDefinition:
        try:
            return self._definitions[(family_id, family_version)]
        except KeyError as error:
            if any(identity[0] == family_id for identity in self._definitions):
                raise FamilyKernelError(
                    f"unsupported family_version: {family_version}"
                ) from error
            raise FamilyKernelError(f"unknown family_id: {family_id}") from error


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise FamilyKernelError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> Any:
    raise FamilyKernelError(f"non-finite JSON value: {value}")


def _canonical_json(value: object) -> bytes:
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
        raise FamilyKernelError("kernel value must be finite plain JSON") from error


def _plain_json(value: object, field: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
    elif isinstance(value, list):
        for item in value:
            _plain_json(item, field)
        return
    elif isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise FamilyKernelError(f"{field} keys must be strings")
        for item in value.values():
            _plain_json(item, field)
        return
    raise FamilyKernelError(f"{field} must contain only finite plain JSON")


def canonical_decision_bytes(decision: FamilyDecision) -> bytes:
    return _canonical_json(asdict(decision))


def _canonical_score(value: float) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FamilyKernelError("family score must be numeric")
    if not math.isfinite(float(value)):
        raise FamilyKernelError("family score must be finite")
    rounded = round(float(value), 12)
    if rounded == 0:
        return "0"
    return format(rounded, ".12f").rstrip("0").rstrip(".")


def _validate_bar(value: ClosedBar, previous_ts: int | None) -> None:
    if not isinstance(value, ClosedBar):
        raise FamilyKernelError("bar must be ClosedBar")
    if isinstance(value.ts_event_ns, bool) or not isinstance(value.ts_event_ns, int):
        raise FamilyKernelError("ts_event_ns must be an integer")
    if value.ts_event_ns <= 0:
        raise FamilyKernelError("ts_event_ns must be positive")
    if previous_ts is not None and value.ts_event_ns <= previous_ts:
        raise FamilyKernelError("closed bars must be strictly increasing and unique")
    for field_name in ("open", "high", "low", "close", "volume"):
        field_value = getattr(value, field_name)
        if isinstance(field_value, bool) or not isinstance(field_value, (int, float)):
            raise FamilyKernelError(f"{field_name} must be numeric")
        if not math.isfinite(float(field_value)):
            raise FamilyKernelError(f"{field_name} must be finite")
    if value.close <= 0:
        raise FamilyKernelError("close must be positive")


def _finite_nonnegative_parameter(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FamilyKernelError(f"{name} must be numeric")
    try:
        normalized = float(value)
    except (OverflowError, ValueError) as error:
        raise FamilyKernelError(f"{name} must be finite and non-negative") from error
    if not math.isfinite(normalized) or normalized < 0:
        raise FamilyKernelError(f"{name} must be finite and non-negative")
    return 0.0 if normalized == 0 else normalized


def _validate_momentum_parameters(parameters: Mapping[str, Any]) -> dict[str, Any]:
    if set(parameters) != {"entry_threshold", "lookback_bars"}:
        raise FamilyKernelError(
            "momentum parameters must contain only entry_threshold and lookback_bars"
        )
    lookback_bars = parameters["lookback_bars"]
    entry_threshold = parameters["entry_threshold"]
    if isinstance(lookback_bars, bool) or not isinstance(lookback_bars, int):
        raise FamilyKernelError("lookback_bars must be an integer")
    if not 1 <= lookback_bars <= 8_760:
        raise FamilyKernelError("lookback_bars must be between 1 and 8760")
    return {
        "entry_threshold": _finite_nonnegative_parameter(
            entry_threshold,
            "entry_threshold",
        ),
        "lookback_bars": lookback_bars,
    }


def _momentum_evaluation(
    bars: Sequence[ClosedBar], parameters: Mapping[str, Any]
) -> FamilyEvaluation:
    lookback_bars = int(parameters["lookback_bars"])
    score = round(float(bars[-1].close) / float(bars[-lookback_bars].close) - 1.0, 12)
    is_long = score > float(parameters["entry_threshold"])
    return FamilyEvaluation(
        score=score,
        target_intent="LONG" if is_long else "FLAT",
        reason=(
            "MOMENTUM_ABOVE_ENTRY_THRESHOLD"
            if is_long
            else "MOMENTUM_AT_OR_BELOW_ENTRY_THRESHOLD"
        ),
    )


def _validate_sma_parameters(parameters: Mapping[str, Any]) -> dict[str, Any]:
    if set(parameters) != {"discount_threshold", "window_bars"}:
        raise FamilyKernelError(
            "sma mean-reversion parameters must contain only discount_threshold and window_bars"
        )
    window_bars = parameters["window_bars"]
    discount_threshold = parameters["discount_threshold"]
    if isinstance(window_bars, bool) or not isinstance(window_bars, int):
        raise FamilyKernelError("window_bars must be an integer")
    if not 2 <= window_bars <= 8_760:
        raise FamilyKernelError("window_bars must be between 2 and 8760")
    return {
        "discount_threshold": _finite_nonnegative_parameter(
            discount_threshold,
            "discount_threshold",
        ),
        "window_bars": window_bars,
    }


def _sma_mean_reversion_evaluation(
    bars: Sequence[ClosedBar], parameters: Mapping[str, Any]
) -> FamilyEvaluation:
    mean_close = sum(float(bar.close) for bar in bars) / len(bars)
    score = round(float(bars[-1].close) / mean_close - 1.0, 12)
    discount_threshold = float(parameters["discount_threshold"])
    is_long = score < -discount_threshold
    return FamilyEvaluation(
        score=score,
        target_intent="LONG" if is_long else "FLAT",
        reason=(
            "CLOSE_BELOW_SMA_DISCOUNT_THRESHOLD"
            if is_long
            else "CLOSE_AT_OR_ABOVE_SMA_DISCOUNT_THRESHOLD"
        ),
    )


DEFAULT_REGISTRY = FamilyRegistry(
    (
        FamilyDefinition(
            family_id="close-vs-sma-mean-reversion-long-flat",
            family_version="close-vs-sma-mean-reversion-long-flat-v1",
            warmup_bars=lambda parameters: int(parameters["window_bars"]),
            validate_parameters=_validate_sma_parameters,
            evaluate=_sma_mean_reversion_evaluation,
            thesis=(
                "A completed-bar close materially below its short-window SMA "
                "mean-reverts after the next event."
            ),
            falsification=(
                "No activity, excessive provisional drawdown or turnover, or "
                "failure under later authoritative evaluation."
            ),
        ),
        FamilyDefinition(
            family_id="lookback-momentum-long-flat",
            family_version="lookback-momentum-long-flat-v1",
            warmup_bars=lambda parameters: int(parameters["lookback_bars"]),
            validate_parameters=_validate_momentum_parameters,
            evaluate=_momentum_evaluation,
            thesis="Positive completed-bar momentum persists into the next event.",
            falsification=(
                "No activity, excessive provisional drawdown or turnover, or "
                "failure under later authoritative evaluation."
            ),
        ),
    )
)


def _resolve(
    family_id: str,
    family_version: str,
    parameters: Mapping[str, Any],
    registry: FamilyRegistry,
) -> tuple[FamilyDefinition, dict[str, Any], int]:
    definition = registry.resolve(family_id, family_version)
    if not isinstance(parameters, Mapping):
        raise FamilyKernelError("parameters must be a mapping")
    normalized = definition.validate_parameters(parameters)
    if not isinstance(normalized, dict):
        raise FamilyKernelError("parameter validator must return a dictionary")
    _plain_json(normalized, "parameters")
    warmup = definition.warmup_bars(normalized)
    if isinstance(warmup, bool) or not isinstance(warmup, int) or warmup < 1:
        raise FamilyKernelError("family warmup must be a positive integer")
    return definition, normalized, warmup


def derive_signal_id(
    *,
    family_id: str,
    family_version: str,
    kernel_hash: str,
    kernel_version: str,
    parameters: Mapping[str, Any],
    reason: str,
    score: str,
    target_intent: str,
    ts_event_ns: int,
) -> str:
    identity = {
        "family_id": family_id,
        "family_version": family_version,
        "kernel_hash": kernel_hash,
        "kernel_version": kernel_version,
        "parameters": dict(parameters),
        "reason": reason,
        "score": score,
        "target_intent": target_intent,
        "ts_event_ns": ts_event_ns,
    }
    return hashlib.sha256(_canonical_json(identity)).hexdigest()


def _decision(
    *,
    bar: ClosedBar,
    evaluation: FamilyEvaluation,
    definition: FamilyDefinition,
    parameters: Mapping[str, Any],
) -> FamilyDecision:
    if evaluation.target_intent not in {"LONG", "FLAT"}:
        raise FamilyKernelError("target_intent must be LONG or FLAT")
    if not evaluation.reason:
        raise FamilyKernelError("reason must be non-empty")
    score = _canonical_score(evaluation.score)
    signal_id = derive_signal_id(
        family_id=definition.family_id,
        family_version=definition.family_version,
        kernel_hash=KERNEL_HASH,
        kernel_version=KERNEL_VERSION,
        parameters=parameters,
        reason=evaluation.reason,
        score=score,
        target_intent=evaluation.target_intent,
        ts_event_ns=bar.ts_event_ns,
    )
    return FamilyDecision(
        signal_id=signal_id,
        ts_event_ns=bar.ts_event_ns,
        score=score,
        target_intent=evaluation.target_intent,
        reason=evaluation.reason,
        family_id=definition.family_id,
        family_version=definition.family_version,
        kernel_version=KERNEL_VERSION,
        kernel_hash=KERNEL_HASH,
    )


def evaluate_batch(
    *,
    family_id: str,
    family_version: str,
    parameters: Mapping[str, Any],
    bars: Sequence[ClosedBar],
    registry: FamilyRegistry | None = None,
) -> tuple[FamilyDecision, ...]:
    definition, normalized, warmup = _resolve(
        family_id, family_version, parameters, registry or DEFAULT_REGISTRY
    )
    window: list[ClosedBar] = []
    decisions: list[FamilyDecision] = []
    previous_ts: int | None = None
    for current_bar in bars:
        _validate_bar(current_bar, previous_ts)
        previous_ts = current_bar.ts_event_ns
        window.append(current_bar)
        if len(window) > warmup:
            window.pop(0)
        if len(window) < warmup:
            continue
        decisions.append(
            _decision(
                bar=current_bar,
                evaluation=definition.evaluate(tuple(window), normalized),
                definition=definition,
                parameters=normalized,
            )
        )
    return tuple(decisions)


class IncrementalFamilyEvaluator:
    """Stateful closed-bar adapter with canonical restart state."""

    def __init__(
        self,
        *,
        family_id: str,
        family_version: str,
        parameters: Mapping[str, Any],
        registry: FamilyRegistry | None = None,
    ) -> None:
        self._registry = registry or DEFAULT_REGISTRY
        self._definition, self._parameters, self._warmup = _resolve(
            family_id, family_version, parameters, self._registry
        )
        self._bars: list[ClosedBar] = []
        self._last_ts_event_ns: int | None = None

    @property
    def last_ts_event_ns(self) -> int | None:
        return self._last_ts_event_ns

    def push(self, current_bar: ClosedBar) -> FamilyDecision | None:
        _validate_bar(current_bar, self._last_ts_event_ns)
        self._bars.append(current_bar)
        if len(self._bars) > self._warmup:
            self._bars.pop(0)
        self._last_ts_event_ns = current_bar.ts_event_ns
        if len(self._bars) < self._warmup:
            return None
        return _decision(
            bar=current_bar,
            evaluation=self._definition.evaluate(tuple(self._bars), self._parameters),
            definition=self._definition,
            parameters=self._parameters,
        )

    def snapshot(self) -> bytes:
        return _canonical_json(
            {
                "bars": [asdict(value) for value in self._bars],
                "family_id": self._definition.family_id,
                "family_version": self._definition.family_version,
                "kernel_hash": KERNEL_HASH,
                "kernel_version": KERNEL_VERSION,
                "last_ts_event_ns": self._last_ts_event_ns,
                "parameters": self._parameters,
                "schema_version": "strategy-family-kernel-state-v1",
            }
        )

    @classmethod
    def restore(
        cls, payload: bytes, *, registry: FamilyRegistry | None = None
    ) -> IncrementalFamilyEvaluator:
        return restore_incremental(payload, registry=registry)


def restore_incremental(
    payload: bytes, *, registry: FamilyRegistry | None = None
) -> IncrementalFamilyEvaluator:
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FamilyKernelError("kernel snapshot must be UTF-8 JSON") from error
    if payload != _canonical_json(value):
        raise FamilyKernelError("kernel snapshot must use canonical JSON encoding")
    expected = {
        "bars",
        "family_id",
        "family_version",
        "kernel_hash",
        "kernel_version",
        "last_ts_event_ns",
        "parameters",
        "schema_version",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise FamilyKernelError("invalid kernel snapshot fields")
    if value["schema_version"] != "strategy-family-kernel-state-v1":
        raise FamilyKernelError("unsupported kernel snapshot schema")
    if value["kernel_version"] != KERNEL_VERSION or value["kernel_hash"] != KERNEL_HASH:
        raise FamilyKernelError("kernel identity mismatch")
    family_id = value["family_id"]
    family_version = value["family_version"]
    parameters = value["parameters"]
    if not isinstance(family_id, str) or not isinstance(family_version, str):
        raise FamilyKernelError("invalid family identity in kernel snapshot")
    if not isinstance(parameters, dict):
        raise FamilyKernelError("invalid parameters in kernel snapshot")
    evaluator = IncrementalFamilyEvaluator(
        family_id=family_id,
        family_version=family_version,
        parameters=parameters,
        registry=registry,
    )
    rows = value["bars"]
    if not isinstance(rows, list):
        raise FamilyKernelError("snapshot bars must be an array")
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "close",
            "high",
            "low",
            "open",
            "ts_event_ns",
            "volume",
        }:
            raise FamilyKernelError("invalid snapshot bar fields")
        evaluator.push(ClosedBar(**row))
    if evaluator.last_ts_event_ns != value["last_ts_event_ns"]:
        raise FamilyKernelError("snapshot last timestamp mismatch")
    return evaluator
