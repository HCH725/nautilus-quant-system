from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import argparse
import json
from pathlib import Path
from typing import Any, Final, Literal, cast


WindowScheme = Literal["expanding", "rolling"]
RegimeLabel = Literal["TREND", "RANGE", "HIGH_VOLATILITY"]
StressScenario = Literal[
    "baseline",
    "fee_2x",
    "funding_2x",
    "delay_1_bar",
    "parameter_low",
    "parameter_high",
    "slippage_one_tick",
]

_EVIDENCE_OR_MODELING_GAP_REASONS: Final = frozenset(
    {
        "FUNDING_TRUTH_NOT_OFFICIAL",
        "ONE_TICK_SLIPPAGE_NOT_MODELED_BY_NAUTILUS",
        "UNMODELED_SLIPPAGE",
    },
)

_POLICY_FIELDS: Final = frozenset(
    {
        "action_policy_version",
        "delay_bars",
        "fee_multipliers",
        "funding_multipliers",
        "maximum_realized_drawdown",
        "maximum_windows_per_scheme",
        "minimum_net_account_delta",
        "parameter_relative_offsets",
        "policy_version",
        "regime_lookback_bars",
        "regime_return_threshold",
        "regime_volatility_threshold",
        "require_complete_matrix",
        "require_modeled_stresses",
        "require_official_funding",
        "schema_version",
        "slippage_models",
        "stress_scenarios",
        "unmodeled_metric_status",
        "unmodeled_metrics",
        "window_schemes",
        "windowing",
    },
)
_WINDOW_FIELDS: Final = frozenset(
    {
        "expanding_minimum_train_bars",
        "rolling_train_bars",
        "step_bars",
        "test_bars",
    },
)
_STRESS_SCENARIOS: Final[tuple[StressScenario, ...]] = (
    "baseline",
    "fee_2x",
    "funding_2x",
    "delay_1_bar",
    "parameter_low",
    "parameter_high",
    "slippage_one_tick",
)
_REPOSITORY_POLICY_PATH: Final = (
    Path(__file__).resolve().parents[2] / "config/strategy_robustness_policy.json"
)


class StrategyRobustnessError(ValueError):
    """Raised when Card 3 evidence crosses an invalid trust boundary."""


@dataclass(frozen=True, slots=True)
class RobustnessPolicy:
    policy_id: str
    policy_version: str
    action_policy_version: str
    window_schemes: tuple[WindowScheme, ...]
    maximum_windows_per_scheme: int
    expanding_minimum_train_bars: int
    rolling_train_bars: int
    test_bars: int
    step_bars: int
    regime_lookback_bars: int
    regime_return_threshold: Decimal
    regime_volatility_threshold: Decimal
    fee_multipliers: tuple[Decimal, ...]
    funding_multipliers: tuple[Decimal, ...]
    delay_bars: tuple[int, ...]
    parameter_relative_offsets: tuple[Decimal, ...]
    slippage_models: tuple[str, ...]
    stress_scenarios: tuple[StressScenario, ...]
    minimum_net_account_delta: Decimal
    maximum_realized_drawdown: Decimal
    require_complete_matrix: bool
    require_modeled_stresses: bool
    require_official_funding: bool
    unmodeled_metrics: tuple[str, ...]
    unmodeled_metric_status: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_id", sha256(canonical_json(self.document())).hexdigest())

    def document(self) -> dict[str, object]:
        return {
            "action_policy_version": self.action_policy_version,
            "delay_bars": list(self.delay_bars),
            "fee_multipliers": [format(value, "f") for value in self.fee_multipliers],
            "funding_multipliers": [format(value, "f") for value in self.funding_multipliers],
            "maximum_realized_drawdown": format(self.maximum_realized_drawdown, "f"),
            "maximum_windows_per_scheme": self.maximum_windows_per_scheme,
            "minimum_net_account_delta": format(self.minimum_net_account_delta, "f"),
            "parameter_relative_offsets": [format(value, "f") for value in self.parameter_relative_offsets],
            "policy_version": self.policy_version,
            "regime_lookback_bars": self.regime_lookback_bars,
            "regime_return_threshold": format(self.regime_return_threshold, "f"),
            "regime_volatility_threshold": format(self.regime_volatility_threshold, "f"),
            "require_complete_matrix": self.require_complete_matrix,
            "require_modeled_stresses": self.require_modeled_stresses,
            "require_official_funding": self.require_official_funding,
            "schema_version": "strategy-robustness-policy-v1",
            "slippage_models": list(self.slippage_models),
            "stress_scenarios": list(self.stress_scenarios),
            "unmodeled_metric_status": self.unmodeled_metric_status,
            "unmodeled_metrics": list(self.unmodeled_metrics),
            "window_schemes": list(self.window_schemes),
            "windowing": {
                "expanding_minimum_train_bars": self.expanding_minimum_train_bars,
                "rolling_train_bars": self.rolling_train_bars,
                "step_bars": self.step_bars,
                "test_bars": self.test_bars,
            },
        }


@dataclass(frozen=True, slots=True)
class RobustnessWindow:
    """One bounded, immutable train/test evaluation window."""

    scheme: WindowScheme
    ordinal: int
    train_start_ns: int
    train_end_ns: int
    test_start_ns: int
    test_end_ns: int
    data_as_of_ns: int
    evaluation_context_id: str
    regime_label: RegimeLabel | None = None


@dataclass(frozen=True, slots=True)
class NautilusCostPolicy:
    """Tracked cost assumptions passed to the formal Nautilus evaluator."""

    fee_multiplier: Decimal = Decimal("1")
    funding_multiplier: Decimal = Decimal("1")
    delay_bars: int = 0
    slippage_model: Literal["none", "one_tick"] = "none"
    fee_source: str = "nautilus_instrument_metadata"
    funding_source: str = "canonical_funding_observation_v1"
    cost_policy_id: str = ""

    def __post_init__(self) -> None:
        for name, value in (
            ("fee_multiplier", self.fee_multiplier),
            ("funding_multiplier", self.funding_multiplier),
        ):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise StrategyRobustnessError(f"{name} must be a positive finite Decimal")
        if isinstance(self.delay_bars, bool) or self.delay_bars < 0:
            raise StrategyRobustnessError("delay_bars must be a non-negative integer")
        if self.slippage_model not in {"none", "one_tick"}:
            raise StrategyRobustnessError("unsupported slippage_model")
        if self.fee_source != "nautilus_instrument_metadata":
            raise StrategyRobustnessError("fee source must be Nautilus instrument metadata")
        if self.funding_source != "canonical_funding_observation_v1":
            raise StrategyRobustnessError("funding source must be canonical observations")
        canonical_id = sha256(canonical_json(self.document())).hexdigest()
        if not self.cost_policy_id:
            object.__setattr__(self, "cost_policy_id", canonical_id)
        elif self.cost_policy_id != canonical_id:
            raise StrategyRobustnessError("cost_policy_id must match the canonical cost policy")

    def document(self) -> dict[str, object]:
        return {
            "fee_multiplier": str(self.fee_multiplier),
            "fee_source": self.fee_source,
            "funding_multiplier": str(self.funding_multiplier),
            "funding_source": self.funding_source,
            "delay_bars": self.delay_bars,
            "schema_version": "nautilus-cost-policy-v1",
            "slippage_model": self.slippage_model,
        }


@dataclass(frozen=True, slots=True)
class RobustnessCell:
    """One immutable window × stress evaluation identity."""

    cell_id: str
    evaluation_context_id: str
    window: RobustnessWindow
    stress_scenario: StressScenario
    cost_policy: NautilusCostPolicy
    parameters: dict[str, object]
    parameter_relative_offset: Decimal


@dataclass(frozen=True, slots=True)
class RobustnessCellResult:
    cell_id: str
    evaluation_context_id: str
    technical_status: Literal["PASS", "ERROR"]
    economic_status: Literal["PASS", "FAIL", "NOT_MODELED"]
    verdict_id: str | None
    net_account_delta: Decimal | None
    realized_balance_drawdown: Decimal | None
    funding_truth_status: str | None
    reason_codes: tuple[str, ...]
    artifact_sha256: str | None = None


def _cell_identity(
    *,
    window: RobustnessWindow,
    stress_scenario: StressScenario,
    cost_policy: NautilusCostPolicy,
    parameters: Mapping[str, object],
    parameter_relative_offset: Decimal,
) -> tuple[str, str]:
    preimage = {
        "cost_policy_id": cost_policy.cost_policy_id,
        "data_as_of_ns": window.data_as_of_ns,
        "parameter_relative_offset": str(parameter_relative_offset),
        "parameters": dict(parameters),
        "schema_version": "robustness-cell-v1",
        "stress_scenario": stress_scenario,
        "regime_label": window.regime_label,
        "test_end_ns": window.test_end_ns,
        "test_start_ns": window.test_start_ns,
        "train_end_ns": window.train_end_ns,
        "train_start_ns": window.train_start_ns,
        "window_context_id": window.evaluation_context_id,
        "window_ordinal": window.ordinal,
        "window_scheme": window.scheme,
    }
    cell_id = sha256(canonical_json(preimage)).hexdigest()
    context_id = sha256(
        canonical_json(
            {
                "base_window_context_id": window.evaluation_context_id,
                "cell_id": cell_id,
                "schema_version": "robustness-cell-context-v1",
            },
        ),
    ).hexdigest()
    return cell_id, context_id


def _scenario_cost_policy(policy: RobustnessPolicy, scenario: StressScenario) -> tuple[NautilusCostPolicy, Decimal]:
    if scenario == "baseline":
        return NautilusCostPolicy(), Decimal("0")
    if scenario == "fee_2x":
        return NautilusCostPolicy(fee_multiplier=policy.fee_multipliers[1]), Decimal("0")
    if scenario == "funding_2x":
        return NautilusCostPolicy(funding_multiplier=policy.funding_multipliers[1]), Decimal("0")
    if scenario == "delay_1_bar":
        return NautilusCostPolicy(delay_bars=policy.delay_bars[1]), Decimal("0")
    if scenario == "slippage_one_tick":
        return NautilusCostPolicy(slippage_model="one_tick"), Decimal("0")
    if scenario == "parameter_low":
        return NautilusCostPolicy(), policy.parameter_relative_offsets[0]
    if scenario == "parameter_high":
        return NautilusCostPolicy(), policy.parameter_relative_offsets[1]
    raise StrategyRobustnessError(f"unsupported stress scenario: {scenario}")


def generate_robustness_matrix(
    parameters: dict[str, object],
    windows: Sequence[RobustnessWindow],
    policy: RobustnessPolicy,
) -> tuple[RobustnessCell, ...]:
    """Build the frozen window × stress matrix without changing candidate parameters."""
    cells: list[RobustnessCell] = []
    for window in windows:
        for scenario in policy.stress_scenarios:
            cost_policy, offset = _scenario_cost_policy(policy, scenario)
            cell_parameters = (
                parameter_neighborhood(parameters, offset)
                if offset
                else dict(parameters)
            )
            cell_id, context_id = _cell_identity(
                window=window,
                stress_scenario=scenario,
                cost_policy=cost_policy,
                parameters=cell_parameters,
                parameter_relative_offset=offset,
            )
            cells.append(
                RobustnessCell(
                    cell_id=cell_id,
                    evaluation_context_id=context_id,
                    window=window,
                    stress_scenario=scenario,
                    cost_policy=cost_policy,
                    parameters=cell_parameters,
                    parameter_relative_offset=offset,
                ),
            )
    return tuple(cells)


def robustness_evaluation_context_id(
    policy: RobustnessPolicy,
    cells: Sequence[RobustnessCell],
) -> str:
    """Bind reuse to the frozen policy and exact ordered robustness matrix."""
    cell_ids = [cell.cell_id for cell in cells]
    if not cell_ids or len(cell_ids) != len(set(cell_ids)):
        raise StrategyRobustnessError("robustness context requires unique cells")
    return sha256(
        canonical_json(
            {
                "cell_ids": cell_ids,
                "policy_id": policy.policy_id,
                "schema_version": "robustness-evaluation-context-v1",
            },
        ),
    ).hexdigest()


def _window_context_id(
    *,
    base_context_id: str,
    scheme: WindowScheme,
    ordinal: int,
    train_start_ns: int,
    train_end_ns: int,
    test_start_ns: int,
    test_end_ns: int,
    data_as_of_ns: int,
) -> str:
    return sha256(
        canonical_json(
            {
                "base_evaluation_context_id": base_context_id,
                "data_as_of_ns": data_as_of_ns,
                "ordinal": ordinal,
                "scheme": scheme,
                "test_end_ns": test_end_ns,
                "test_start_ns": test_start_ns,
                "train_end_ns": train_end_ns,
                "train_start_ns": train_start_ns,
                "schema_version": "robustness-window-context-v1",
            },
        ),
    ).hexdigest()


def _bounded_timestamps(
    timestamps: tuple[int, ...] | list[int] | object,
    *,
    evaluation_start_ns: int,
    evaluation_end_ns: int,
    data_as_of_ns: int,
) -> tuple[int, ...]:
    if (
        isinstance(evaluation_start_ns, bool)
        or isinstance(evaluation_end_ns, bool)
        or isinstance(data_as_of_ns, bool)
        or not all(isinstance(value, int) for value in (evaluation_start_ns, evaluation_end_ns, data_as_of_ns))
        or evaluation_start_ns <= 0
        or evaluation_end_ns <= evaluation_start_ns
        or data_as_of_ns < evaluation_end_ns
    ):
        raise StrategyRobustnessError("robustness bounds must be positive, ordered UTC nanoseconds")
    try:
        values = tuple(timestamps)  # type: ignore[arg-type]
    except TypeError as error:
        raise StrategyRobustnessError("robustness timestamps must be a sequence") from error
    if (
        not values
        or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values)
        or values != tuple(sorted(set(values)))
    ):
        raise StrategyRobustnessError("robustness timestamps must be unique, positive, and increasing")
    bounded = tuple(
        value for value in values
        if evaluation_start_ns <= value <= evaluation_end_ns and value <= data_as_of_ns
    )
    if not bounded:
        raise StrategyRobustnessError("robustness bounds contain no timestamps")
    return bounded


def generate_robustness_windows(
    timestamps: tuple[int, ...] | list[int],
    policy: RobustnessPolicy,
    *,
    evaluation_start_ns: int,
    evaluation_end_ns: int,
    data_as_of_ns: int,
    evaluation_context_id: str,
    closed_bars: Sequence[object] | None = None,
) -> tuple[RobustnessWindow, ...]:
    """Generate deterministic, bounded expanding and rolling windows."""
    if not isinstance(evaluation_context_id, str) or len(evaluation_context_id) != 64:
        raise StrategyRobustnessError("evaluation_context_id must be a SHA-256 string")
    bounded = _bounded_timestamps(
        timestamps,
        evaluation_start_ns=evaluation_start_ns,
        evaluation_end_ns=evaluation_end_ns,
        data_as_of_ns=data_as_of_ns,
    )
    canonical_bars: tuple[object, ...] | None = None
    if closed_bars is not None:
        try:
            canonical_bars = tuple(closed_bars)
        except TypeError as error:
            raise StrategyRobustnessError("closed bars must be a sequence") from error
        if not canonical_bars:
            raise StrategyRobustnessError("closed bars must not be empty")
        timestamps_for_bars = tuple(_closed_bar_timestamp(bar) for bar in canonical_bars)
        if timestamps_for_bars != tuple(sorted(set(timestamps_for_bars))):
            raise StrategyRobustnessError("closed bars must be strictly ordered")
    windows: list[RobustnessWindow] = []
    for scheme in policy.window_schemes:
        train_size = (
            policy.expanding_minimum_train_bars
            if scheme == "expanding"
            else policy.rolling_train_bars
        )
        ordinal = 0
        test_start_index = train_size
        while (
            ordinal < policy.maximum_windows_per_scheme
            and test_start_index + policy.test_bars <= len(bounded)
        ):
            test_end_index = test_start_index + policy.test_bars - 1
            if scheme == "expanding":
                train_start_index = 0
            else:
                train_start_index = test_start_index - train_size
            train_end_index = test_start_index - 1
            train_start_ns = bounded[train_start_index]
            train_end_ns = bounded[train_end_index]
            test_start_ns = bounded[test_start_index]
            test_end_ns = bounded[test_end_index]
            regime_label: RegimeLabel | None = None
            if canonical_bars is not None:
                train_bars = tuple(
                    bar
                    for bar in canonical_bars
                    if train_start_ns <= _closed_bar_timestamp(bar) <= train_end_ns
                )
                regime_label = deterministic_regime_label(train_bars, policy)
            windows.append(
                RobustnessWindow(
                    scheme=scheme,
                    ordinal=ordinal,
                    train_start_ns=train_start_ns,
                    train_end_ns=train_end_ns,
                    test_start_ns=test_start_ns,
                    test_end_ns=test_end_ns,
                    data_as_of_ns=data_as_of_ns,
                    evaluation_context_id=_window_context_id(
                        base_context_id=evaluation_context_id,
                        scheme=scheme,
                        ordinal=ordinal,
                        train_start_ns=train_start_ns,
                        train_end_ns=train_end_ns,
                        test_start_ns=test_start_ns,
                        test_end_ns=test_end_ns,
                        data_as_of_ns=data_as_of_ns,
                    ),
                    regime_label=regime_label,
                ),
            )
            ordinal += 1
            test_start_index += policy.step_bars
    return tuple(windows)


def _closed_bar_timestamp(bar: object) -> int:
    timestamp = getattr(bar, "ts_event_ns", getattr(bar, "ts_event", None))
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp <= 0:
        raise StrategyRobustnessError("closed bars must expose positive integer timestamps")
    return timestamp


def deterministic_regime_label(bars: object, policy: RobustnessPolicy) -> RegimeLabel:
    """Classify a closed-price sequence without future or random state."""
    try:
        closes = tuple(Decimal(str(getattr(bar, "close"))) for bar in bars)  # type: ignore[union-attr]
    except (TypeError, ValueError, InvalidOperation) as error:
        raise StrategyRobustnessError("regime bars must expose finite close prices") from error
    if len(closes) < 2 or any(not value.is_finite() or value <= 0 for value in closes):
        raise StrategyRobustnessError("regime bars must contain at least two positive closes")
    lookback = min(policy.regime_lookback_bars, len(closes))
    closes = closes[-lookback:]
    returns = tuple(
        (current / previous) - Decimal("1")
        for previous, current in zip(closes, closes[1:])
    )
    mean = sum(returns, Decimal()) / Decimal(len(returns))
    variance = sum((value - mean) ** 2 for value in returns) / Decimal(len(returns))
    volatility = variance.sqrt()
    if volatility >= policy.regime_volatility_threshold:
        return "HIGH_VOLATILITY"
    deltas = tuple(current - previous for previous, current in zip(closes, closes[1:]))
    if all(delta >= 0 for delta in deltas) and any(delta > 0 for delta in deltas):
        return "TREND"
    if all(delta <= 0 for delta in deltas) and any(delta < 0 for delta in deltas):
        return "TREND"
    total_return = abs(closes[-1] / closes[0] - Decimal("1"))
    return "TREND" if total_return >= policy.regime_return_threshold else "RANGE"


def parameter_neighborhood(
    parameters: dict[str, object],
    relative_offset: Decimal,
) -> dict[str, object]:
    """Return a scaled copy of parameters; never mutate persisted input."""
    if not isinstance(relative_offset, Decimal) or not relative_offset.is_finite():
        raise StrategyRobustnessError("relative_offset must be a finite Decimal")
    if relative_offset <= Decimal("-1"):
        raise StrategyRobustnessError("relative_offset must keep parameters positive")
    result: dict[str, object] = {}
    for name, value in parameters.items():
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            raise StrategyRobustnessError(f"parameter {name} must be numeric")
        try:
            scaled = Decimal(str(value)) * (Decimal("1") + relative_offset)
        except (InvalidOperation, ValueError) as error:
            raise StrategyRobustnessError(f"parameter {name} must be finite") from error
        if not scaled.is_finite() or scaled < 0:
            raise StrategyRobustnessError(f"parameter {name} must remain finite and non-negative")
        if isinstance(value, int):
            result[name] = int(scaled.to_integral_value())
        elif isinstance(value, Decimal):
            result[name] = scaled
        else:
            result[name] = float(scaled)
    return result


def canonical_json(value: object) -> bytes:
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
        raise StrategyRobustnessError("robustness value must be finite plain JSON") from error


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise StrategyRobustnessError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _decimal(value: object, name: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, str):
        raise StrategyRobustnessError(f"{name} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise StrategyRobustnessError(f"{name} must be a finite decimal") from error
    if not parsed.is_finite() or parsed < 0 or (positive and parsed <= 0):
        raise StrategyRobustnessError(f"{name} must be finite and {'positive' if positive else 'non-negative'}")
    return parsed


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise StrategyRobustnessError(f"{name} must be a positive integer")
    return value


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
        or len(value) != len(set(value))
    ):
        raise StrategyRobustnessError(f"{name} must be a non-empty unique string list")
    return tuple(value)


def load_robustness_policy(path: Path | bytes) -> RobustnessPolicy:
    """Load the result-independent, canonical Card 3 policy."""
    payload = path if isinstance(path, bytes) else Path(path).read_bytes()
    try:
        root = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                StrategyRobustnessError(f"non-finite JSON value: {value}")
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StrategyRobustnessError("robustness policy must be UTF-8 JSON") from error
    if not isinstance(root, dict) or set(root) != _POLICY_FIELDS:
        raise StrategyRobustnessError("invalid robustness policy fields")
    if payload != canonical_json(root):
        raise StrategyRobustnessError("robustness policy must use canonical JSON encoding")
    if root["schema_version"] != "strategy-robustness-policy-v1":
        raise StrategyRobustnessError("unsupported robustness policy schema")
    windows = root["windowing"]
    if not isinstance(windows, dict) or set(windows) != _WINDOW_FIELDS:
        raise StrategyRobustnessError("invalid robustness window fields")
    schemes = _string_tuple(root["window_schemes"], "window_schemes")
    scenarios = _string_tuple(root["stress_scenarios"], "stress_scenarios")
    if schemes != ("expanding", "rolling") or scenarios != _STRESS_SCENARIOS:
        raise StrategyRobustnessError("robustness matrix shape is not frozen")
    metrics = _string_tuple(root["unmodeled_metrics"], "unmodeled_metrics")
    if metrics != ("DSR", "PBO") or root["unmodeled_metric_status"] != "NOT_MODELED":
        raise StrategyRobustnessError("DSR and PBO must remain explicitly NOT_MODELED")
    booleans = (
        root["require_complete_matrix"],
        root["require_modeled_stresses"],
        root["require_official_funding"],
    )
    if not all(value is True for value in booleans):
        raise StrategyRobustnessError("robustness completion gates must be enabled")
    fee_multipliers = tuple(_decimal(item, "fee_multipliers", positive=True) for item in root["fee_multipliers"])
    funding_multipliers = tuple(_decimal(item, "funding_multipliers", positive=True) for item in root["funding_multipliers"])
    delay_bars = root["delay_bars"]
    if delay_bars != [0, 1]:
        raise StrategyRobustnessError("delay_bars must be [0,1]")
    offsets = tuple(_decimal(item.lstrip("-"), "parameter_relative_offsets", positive=True).copy_negate() if item.startswith("-") else _decimal(item, "parameter_relative_offsets", positive=True) for item in root["parameter_relative_offsets"])
    slippage_models = _string_tuple(root["slippage_models"], "slippage_models")
    if fee_multipliers != (Decimal("1"), Decimal("2")) or funding_multipliers != (Decimal("1"), Decimal("2")) or offsets != (Decimal("-0.1"), Decimal("0.1")) or slippage_models != ("none", "one_tick"):
        raise StrategyRobustnessError("robustness stress values are not frozen")
    policy_version = root["policy_version"]
    action_version = root["action_policy_version"]
    if policy_version != "strategy-robustness-decision-v1" or action_version != "strategy-action-v1":
        raise StrategyRobustnessError("robustness policy versions are invalid")
    return RobustnessPolicy(
        policy_id=sha256(payload).hexdigest(),
        policy_version=policy_version,
        action_policy_version=action_version,
        window_schemes=("expanding", "rolling"),
        maximum_windows_per_scheme=_positive_integer(root["maximum_windows_per_scheme"], "maximum_windows_per_scheme"),
        expanding_minimum_train_bars=_positive_integer(windows["expanding_minimum_train_bars"], "expanding_minimum_train_bars"),
        rolling_train_bars=_positive_integer(windows["rolling_train_bars"], "rolling_train_bars"),
        test_bars=_positive_integer(windows["test_bars"], "test_bars"),
        step_bars=_positive_integer(windows["step_bars"], "step_bars"),
        regime_lookback_bars=_positive_integer(root["regime_lookback_bars"], "regime_lookback_bars"),
        regime_return_threshold=_decimal(root["regime_return_threshold"], "regime_return_threshold", positive=True),
        regime_volatility_threshold=_decimal(root["regime_volatility_threshold"], "regime_volatility_threshold", positive=True),
        fee_multipliers=fee_multipliers,
        funding_multipliers=funding_multipliers,
        delay_bars=(0, 1),
        parameter_relative_offsets=offsets,
        slippage_models=slippage_models,
        stress_scenarios=_STRESS_SCENARIOS,
        minimum_net_account_delta=_decimal(root["minimum_net_account_delta"], "minimum_net_account_delta", positive=True),
        maximum_realized_drawdown=_decimal(root["maximum_realized_drawdown"], "maximum_realized_drawdown", positive=True),
        require_complete_matrix=True,
        require_modeled_stresses=True,
        require_official_funding=True,
        unmodeled_metrics=metrics,
        unmodeled_metric_status="NOT_MODELED",
    )


def _utc_z_from_ns(timestamp_ns: int) -> str:
    seconds, nanos = divmod(timestamp_ns, 1_000_000_000)
    whole = datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{whole}.{nanos:09d}Z" if nanos else f"{whole}Z"


def _economic_reason_codes(
    policy: RobustnessPolicy,
    cost_policy: NautilusCostPolicy,
    net: Decimal,
    drawdown: Decimal,
    funding_truth: str,
    slippage_status: object,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if net < policy.minimum_net_account_delta:
        reasons.append("MINIMUM_NET_ACCOUNT_DELTA_NOT_MET")
    if drawdown > policy.maximum_realized_drawdown:
        reasons.append("MAXIMUM_REALIZED_DRAWDOWN_EXCEEDED")
    if policy.require_official_funding and funding_truth != "official":
        reasons.append("FUNDING_TRUTH_NOT_OFFICIAL")
    if cost_policy.slippage_model == "one_tick" and slippage_status != "modeled_one_tick":
        reasons.append("ONE_TICK_SLIPPAGE_NOT_MODELED_BY_NAUTILUS")
    elif policy.require_modeled_stresses and slippage_status == "unmodeled":
        reasons.append("UNMODELED_SLIPPAGE")
    return tuple(reasons) if reasons else ("CELL_ECONOMIC_PASS",)


def _cell_result_from_verdict(cell: RobustnessCell, returned: object, policy: RobustnessPolicy) -> RobustnessCellResult:
    verdict = getattr(returned, "verdict", returned)
    verdict_id = getattr(returned, "verdict_id", None)
    artifact_sha256 = verdict_id if isinstance(verdict_id, str) else None
    if not isinstance(verdict, dict):
        return RobustnessCellResult(
            cell.cell_id,
            cell.evaluation_context_id,
            "ERROR",
            "NOT_MODELED",
            None,
            None,
            None,
            None,
            ("NAUTILUS_VERDICT_NOT_OBJECT",),
            artifact_sha256,
        )
    if verdict.get("status") != "EVALUATED":
        return RobustnessCellResult(
            cell.cell_id,
            cell.evaluation_context_id,
            "ERROR",
            "NOT_MODELED",
            verdict_id if isinstance(verdict_id, str) else None,
            None,
            None,
            None,
            ("NAUTILUS_VERDICT_NOT_EVALUATED",),
            artifact_sha256,
        )
    try:
        net = Decimal(str(verdict["net_account_delta"]))
        drawdown = Decimal(str(verdict["realized_balance_drawdown"]))
        funding = verdict["funding"]
        funding_truth = funding["truth_status"]
        if not net.is_finite() or not drawdown.is_finite() or not isinstance(funding_truth, str):
            raise ValueError("non-finite or invalid economic fields")
    except (KeyError, TypeError, InvalidOperation, ValueError) as error:
        return RobustnessCellResult(
            cell.cell_id,
            cell.evaluation_context_id,
            "ERROR",
            "NOT_MODELED",
            verdict_id if isinstance(verdict_id, str) else None,
            None,
            None,
            None,
            ("NAUTILUS_VERDICT_ECONOMIC_FIELDS_INVALID", str(error)),
            artifact_sha256,
        )
    execution = verdict.get("execution")
    slippage_status = execution.get("slippage_status") if isinstance(execution, dict) else None
    reasons = _economic_reason_codes(
        policy,
        cell.cost_policy,
        net,
        drawdown,
        funding_truth,
        slippage_status,
    )
    return RobustnessCellResult(
        cell.cell_id,
        cell.evaluation_context_id,
        "PASS",
        "PASS" if reasons == ("CELL_ECONOMIC_PASS",) else "FAIL",
        verdict_id if isinstance(verdict_id, str) else None,
        net,
        drawdown,
        funding_truth,
        reasons,
        artifact_sha256,
    )


def evaluate_robustness_matrix(
    request: object,
    windows: Sequence[RobustnessWindow],
    policy: RobustnessPolicy,
    *,
    evaluator: Callable[[object, RobustnessCell], object] | None = None,
) -> tuple[RobustnessCell, tuple[RobustnessCellResult, ...]]:
    """Run one formal evaluator call per frozen cell and split technical/economic outcomes."""
    parameters = getattr(request, "parameters", None)
    if not isinstance(parameters, dict):
        parameters = getattr(request, "candidate_parameters", None)
    if not isinstance(parameters, dict):
        try:
            from .pybroker_candidate import load_pybroker_candidate

            candidate, _candidate_id = load_pybroker_candidate(getattr(request, "candidate_path"))
            strategy = candidate.get("strategy")
            parameters = strategy.get("parameters") if isinstance(strategy, dict) else None
        except (AttributeError, KeyError, OSError, TypeError, ValueError) as error:
            raise StrategyRobustnessError("formal robustness request must expose candidate parameters") from error
    if not isinstance(parameters, dict):
        raise StrategyRobustnessError("formal robustness request must expose candidate parameters")
    cells = generate_robustness_matrix(parameters, windows, policy)
    if evaluator is None:
        from dataclasses import replace

        from .candidate_backtest import run_candidate_backtest

        def evaluator(request_value: object, cell: RobustnessCell) -> object:
            request_value = replace(
                request_value,
                evaluation_start_utc=_utc_z_from_ns(cell.window.test_start_ns),
                evaluation_end_utc=_utc_z_from_ns(cell.window.test_end_ns),
                data_as_of_ns=cell.window.data_as_of_ns,
                evaluation_context_id=cell.evaluation_context_id,
                candidate_evaluation_context_id=(
                    getattr(request_value, "candidate_evaluation_context_id", None)
                    or getattr(request_value, "evaluation_context_id")
                ),
                strategy_parameters_override=dict(cell.parameters),
                fee_multiplier=cell.cost_policy.fee_multiplier,
                funding_multiplier=cell.cost_policy.funding_multiplier,
                delay_bars=cell.cost_policy.delay_bars,
                slippage_model=cell.cost_policy.slippage_model,
                cost_policy_id=cell.cost_policy.cost_policy_id,
                robustness_cell_id=cell.cell_id,
            )
            return run_candidate_backtest(request_value)

    results: list[RobustnessCellResult] = []
    for cell in cells:
        try:
            returned = evaluator(request, cell)
        except Exception as error:  # formal boundary: technical evidence, never economic rejection
            results.append(
                RobustnessCellResult(
                    cell.cell_id,
                    cell.evaluation_context_id,
                    "ERROR",
                    "NOT_MODELED",
                    None,
                    None,
                    None,
                    None,
                    ("NAUTILUS_EVALUATION_ERROR", type(error).__name__, str(error)),
                ),
            )
            continue
        results.append(_cell_result_from_verdict(cell, returned, policy))
    return cells, tuple(results)


@dataclass(frozen=True, slots=True)
class FormalNautilusEvaluator:
    """Adapter that turns one robustness cell into one real candidate replay."""

    runner: Callable[[object], object] | None = None

    def __call__(self, request: object, cell: RobustnessCell) -> object:
        from dataclasses import replace

        from .candidate_backtest import run_candidate_backtest

        runner = self.runner or run_candidate_backtest
        return runner(
            replace(
                request,
                evaluation_start_utc=_utc_z_from_ns(cell.window.test_start_ns),
                evaluation_end_utc=_utc_z_from_ns(cell.window.test_end_ns),
                data_as_of_ns=cell.window.data_as_of_ns,
                evaluation_context_id=cell.evaluation_context_id,
                candidate_evaluation_context_id=(
                    getattr(request, "candidate_evaluation_context_id", None)
                    or getattr(request, "evaluation_context_id")
                ),
                strategy_parameters_override=dict(cell.parameters),
                fee_multiplier=cell.cost_policy.fee_multiplier,
                funding_multiplier=cell.cost_policy.funding_multiplier,
                delay_bars=cell.cost_policy.delay_bars,
                slippage_model=cell.cost_policy.slippage_model,
                cost_policy_id=cell.cost_policy.cost_policy_id,
                robustness_cell_id=cell.cell_id,
            ),
        )


def run_formal_robustness(
    request: object,
    timestamps: Sequence[int],
    policy: RobustnessPolicy,
    *,
    evaluation_start_ns: int,
    evaluation_end_ns: int,
    data_as_of_ns: int,
    evaluation_context_id: str,
    closed_bars: Sequence[object] | None = None,
    evaluator: Callable[[object, RobustnessCell], object] | None = None,
) -> tuple[tuple[RobustnessCell, ...], tuple[RobustnessCellResult, ...]]:
    windows = generate_robustness_windows(
        timestamps,
        policy,
        evaluation_start_ns=evaluation_start_ns,
        evaluation_end_ns=evaluation_end_ns,
        data_as_of_ns=data_as_of_ns,
        evaluation_context_id=evaluation_context_id,
        closed_bars=closed_bars,
    )
    if any(window.regime_label is None for window in windows):
        raise StrategyRobustnessError(
            "formal robustness requires canonical closed bars for regime labels",
        )
    return evaluate_robustness_matrix(request, windows, policy, evaluator=evaluator)


generate_robustness_cells = generate_robustness_matrix


_ROBUSTNESS_IDENTITY_FIELDS: Final = frozenset(
    {
        "candidate_id",
        "code_commit",
        "data_as_of_ns",
        "data_snapshot_id",
        "data_source_id",
        "engine_id",
        "evaluation_context_id",
        "experiment_id",
        "hypothesis_id",
        "policy_id",
        "runtime_id",
        "strategy_id",
    },
)
_TRIAL_CONTEXT_REQUIRED_FIELDS: Final = frozenset(
    {
        "campaign_id",
        "candidate_count",
        "cohort_id",
        "data_as_of_ns",
        "deduped_count",
        "executed_count",
        "family_count",
        "family_id",
        "family_version",
        "generated_count",
        "generation_budget",
        "maximum_candidates",
        "parameter_search_policy_id",
        "rejected_count",
        "search_space",
        "surviving_count",
        "technical_invalid_count",
        "terminal_census_complete",
        "trial_census_id",
    },
)


def _validated_identity(identity: Mapping[str, object]) -> dict[str, object]:
    if set(identity) != _ROBUSTNESS_IDENTITY_FIELDS:
        raise StrategyRobustnessError("robustness identity must contain all bound identity fields")
    normalized = dict(identity)
    for field in _ROBUSTNESS_IDENTITY_FIELDS - {"data_as_of_ns", "code_commit"}:
        value = normalized[field]
        if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise StrategyRobustnessError(f"{field} must be lowercase SHA-256")
    if not isinstance(normalized["code_commit"], str) or not normalized["code_commit"]:
        raise StrategyRobustnessError("code_commit must be non-empty")
    data_as_of_ns = normalized["data_as_of_ns"]
    if isinstance(data_as_of_ns, bool) or not isinstance(data_as_of_ns, int) or data_as_of_ns < 0:
        raise StrategyRobustnessError("data_as_of_ns must be a non-negative integer")
    return normalized


def _validated_trial_context(trial_context: Mapping[str, object] | None) -> dict[str, object]:
    if not isinstance(trial_context, Mapping):
        raise StrategyRobustnessError("robustness verdict requires a complete trial census")
    if set(trial_context) != _TRIAL_CONTEXT_REQUIRED_FIELDS:
        raise StrategyRobustnessError("trial census fields are incomplete or unknown")
    normalized = dict(trial_context)
    canonical_json(normalized)
    for field in (
        "campaign_id",
        "cohort_id",
        "parameter_search_policy_id",
        "trial_census_id",
    ):
        value = normalized[field]
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise StrategyRobustnessError(f"trial census {field} must be lowercase SHA-256")
    for field in ("family_id", "family_version"):
        if not isinstance(normalized[field], str) or not normalized[field]:
            raise StrategyRobustnessError(f"trial census {field} must be non-empty")
    search_space = normalized["search_space"]
    if (
        not isinstance(search_space, dict)
        or not search_space
        or any(
            not isinstance(name, str)
            or not name
            or not isinstance(values, list)
            or not values
            for name, values in search_space.items()
        )
    ):
        raise StrategyRobustnessError("trial census search_space must be a non-empty grid")
    counts: dict[str, int] = {}
    for field in (
        "candidate_count",
        "data_as_of_ns",
        "deduped_count",
        "generated_count",
        "generation_budget",
        "maximum_candidates",
        "executed_count",
        "family_count",
        "rejected_count",
        "surviving_count",
        "technical_invalid_count",
    ):
        value = normalized[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise StrategyRobustnessError(f"trial census {field} must be a non-negative integer")
        counts[field] = value
    if normalized["terminal_census_complete"] is not True:
        raise StrategyRobustnessError("trial census is not terminal and complete")
    if counts["family_count"] != 1:
        raise StrategyRobustnessError("trial census family_count must be one")
    if counts["generation_budget"] <= 0 or counts["maximum_candidates"] <= 0:
        raise StrategyRobustnessError("trial census budgets must be positive")
    if counts["generated_count"] > min(
        counts["generation_budget"],
        counts["maximum_candidates"],
    ):
        raise StrategyRobustnessError("trial census generated count exceeds budget")
    if counts["generated_count"] != (
        counts["deduped_count"]
        + counts["rejected_count"]
        + counts["surviving_count"]
        + counts["technical_invalid_count"]
    ):
        raise StrategyRobustnessError("trial census terminal counts do not reconcile")
    if (
        counts["candidate_count"] > counts["generated_count"]
        or counts["executed_count"] > counts["generated_count"]
        or counts["executed_count"] < counts["rejected_count"] + counts["surviving_count"]
    ):
        raise StrategyRobustnessError("trial census executed count does not reconcile")
    return normalized


def _result_document(cell: RobustnessCell, result: RobustnessCellResult) -> dict[str, object]:
    return {
        "cell_id": cell.cell_id,
        "economic_status": result.economic_status,
        "artifact_sha256": result.artifact_sha256,
        "evaluation_context_id": result.evaluation_context_id,
        "funding_truth_status": result.funding_truth_status,
        "net_account_delta": None if result.net_account_delta is None else str(result.net_account_delta),
        "realized_balance_drawdown": None if result.realized_balance_drawdown is None else str(result.realized_balance_drawdown),
        "reason_codes": list(result.reason_codes),
        "schema_version": "robustness-cell-result-v1",
        "stress_scenario": cell.stress_scenario,
        "technical_status": result.technical_status,
        "verdict_id": result.verdict_id,
        "regime_label": cell.window.regime_label,
        "window": {
            "data_as_of_ns": cell.window.data_as_of_ns,
            "evaluation_context_id": cell.window.evaluation_context_id,
            "ordinal": cell.window.ordinal,
            "scheme": cell.window.scheme,
            "test_end_ns": cell.window.test_end_ns,
            "test_start_ns": cell.window.test_start_ns,
            "train_end_ns": cell.window.train_end_ns,
            "train_start_ns": cell.window.train_start_ns,
        },
        "cost_policy_id": cell.cost_policy.cost_policy_id,
        "cost_policy": {
            **cell.cost_policy.document(),
            "cost_policy_id": cell.cost_policy.cost_policy_id,
        },
        "parameter_relative_offset": str(cell.parameter_relative_offset),
        "parameters": dict(cell.parameters),
    }


def _validated_cell_document(
    cell: object,
    verdict: Mapping[str, object],
    policy: RobustnessPolicy,
) -> tuple[RobustnessCell, str, str, tuple[str, ...], str | None, bool]:
    invalid = "robustness verdict cell-derived closure is invalid"
    cell_fields = {
        "artifact_sha256",
        "cell_id",
        "cost_policy",
        "cost_policy_id",
        "economic_status",
        "evaluation_context_id",
        "funding_truth_status",
        "net_account_delta",
        "parameter_relative_offset",
        "parameters",
        "reason_codes",
        "realized_balance_drawdown",
        "regime_label",
        "schema_version",
        "stress_scenario",
        "technical_status",
        "verdict_id",
        "window",
    }
    window_fields = {
        "data_as_of_ns",
        "evaluation_context_id",
        "ordinal",
        "scheme",
        "test_end_ns",
        "test_start_ns",
        "train_end_ns",
        "train_start_ns",
    }
    cost_fields = {
        "cost_policy_id",
        "delay_bars",
        "fee_multiplier",
        "fee_source",
        "funding_multiplier",
        "funding_source",
        "schema_version",
        "slippage_model",
    }
    try:
        if not isinstance(cell, dict) or set(cell) != cell_fields:
            raise StrategyRobustnessError(invalid)
        window_document = cell["window"]
        cost_document = cell["cost_policy"]
        trial_context = verdict.get("trial_context")
        if (
            cell["schema_version"] != "robustness-cell-result-v1"
            or not isinstance(window_document, dict)
            or set(window_document) != window_fields
            or not isinstance(cost_document, dict)
            or set(cost_document) != cost_fields
            or not isinstance(trial_context, dict)
        ):
            raise StrategyRobustnessError(invalid)

        scheme = window_document["scheme"]
        ordinal = window_document["ordinal"]
        timestamps = tuple(
            window_document[field]
            for field in (
                "train_start_ns",
                "train_end_ns",
                "test_start_ns",
                "test_end_ns",
                "data_as_of_ns",
            )
        )
        window_context_id = window_document["evaluation_context_id"]
        regime_label = cell["regime_label"]
        if (
            scheme not in policy.window_schemes
            or isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or not 0 <= ordinal < policy.maximum_windows_per_scheme
            or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in timestamps)
            or not timestamps[0] <= timestamps[1] < timestamps[2] <= timestamps[3] <= timestamps[4]
            or timestamps[4] != verdict.get("data_as_of_ns")
            or timestamps[4] != trial_context.get("data_as_of_ns")
            or not isinstance(window_context_id, str)
            or len(window_context_id) != 64
            or any(character not in "0123456789abcdef" for character in window_context_id)
            or regime_label not in {None, "TREND", "RANGE", "HIGH_VOLATILITY"}
        ):
            raise StrategyRobustnessError(invalid)
        window = RobustnessWindow(
            cast(WindowScheme, scheme),
            ordinal,
            cast(int, timestamps[0]),
            cast(int, timestamps[1]),
            cast(int, timestamps[2]),
            cast(int, timestamps[3]),
            cast(int, timestamps[4]),
            window_context_id,
            cast(RegimeLabel | None, regime_label),
        )

        if cost_document["schema_version"] != "nautilus-cost-policy-v1":
            raise StrategyRobustnessError(invalid)
        delay_bars = cost_document["delay_bars"]
        if isinstance(delay_bars, bool) or not isinstance(delay_bars, int):
            raise StrategyRobustnessError(invalid)
        cost_policy = NautilusCostPolicy(
            fee_multiplier=_decimal(cost_document["fee_multiplier"], "fee_multiplier", positive=True),
            funding_multiplier=_decimal(cost_document["funding_multiplier"], "funding_multiplier", positive=True),
            delay_bars=delay_bars,
            slippage_model=cast(Literal["none", "one_tick"], cost_document["slippage_model"]),
            fee_source=cast(str, cost_document["fee_source"]),
            funding_source=cast(str, cost_document["funding_source"]),
        )
        scenario = cell["stress_scenario"]
        if scenario not in policy.stress_scenarios:
            raise StrategyRobustnessError(invalid)
        expected_cost_policy, expected_offset = _scenario_cost_policy(
            policy,
            cast(StressScenario, scenario),
        )
        offset_value = cell["parameter_relative_offset"]
        if not isinstance(offset_value, str):
            raise StrategyRobustnessError(invalid)
        offset = Decimal(offset_value)
        if (
            not offset.is_finite()
            or offset_value != str(expected_offset)
            or cost_policy != expected_cost_policy
            or cost_document != {**cost_policy.document(), "cost_policy_id": cost_policy.cost_policy_id}
            or cell["cost_policy_id"] != cost_policy.cost_policy_id
        ):
            raise StrategyRobustnessError(invalid)

        parameters = cell["parameters"]
        if not isinstance(parameters, dict) or not parameters:
            raise StrategyRobustnessError(invalid)
        canonical_json(parameters)
        cell_id, evaluation_context_id = _cell_identity(
            window=window,
            stress_scenario=cast(StressScenario, scenario),
            cost_policy=cost_policy,
            parameters=parameters,
            parameter_relative_offset=offset,
        )
        if cell["cell_id"] != cell_id or cell["evaluation_context_id"] != evaluation_context_id:
            raise StrategyRobustnessError(invalid)
        validated_cell = RobustnessCell(
            cell_id,
            evaluation_context_id,
            window,
            cast(StressScenario, scenario),
            cost_policy,
            dict(parameters),
            offset,
        )

        artifact_sha256 = cell["artifact_sha256"]
        verdict_id = cell["verdict_id"]
        references_missing = artifact_sha256 is None and verdict_id is None
        if not references_missing and (
            any(
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
                for value in (artifact_sha256, verdict_id)
            )
            or artifact_sha256 != verdict_id
        ):
            raise StrategyRobustnessError(invalid)

        technical_status = cell["technical_status"]
        economic_status = cell["economic_status"]
        reason_codes_value = cell["reason_codes"]
        if (
            technical_status not in {"PASS", "ERROR"}
            or economic_status not in {"PASS", "FAIL", "NOT_MODELED"}
            or not isinstance(reason_codes_value, list)
            or not reason_codes_value
            or not all(isinstance(reason, str) and reason for reason in reason_codes_value)
        ):
            raise StrategyRobustnessError(invalid)
        reason_codes = tuple(reason_codes_value)
        funding_truth = cell["funding_truth_status"]
        if technical_status == "ERROR":
            if (
                economic_status != "NOT_MODELED"
                or cell["net_account_delta"] is not None
                or cell["realized_balance_drawdown"] is not None
                or funding_truth is not None
            ):
                raise StrategyRobustnessError(invalid)
        else:
            net_value = cell["net_account_delta"]
            drawdown_value = cell["realized_balance_drawdown"]
            if not isinstance(net_value, str) or not isinstance(drawdown_value, str):
                raise StrategyRobustnessError(invalid)
            net = Decimal(net_value)
            drawdown = Decimal(drawdown_value)
            if (
                not net.is_finite()
                or not drawdown.is_finite()
                or drawdown < 0
                or funding_truth not in {"official", "modeled_funding", "mixed", "missing"}
            ):
                raise StrategyRobustnessError(invalid)
            slippage_status = (
                "unmodeled"
                if any(
                    reason in {
                        "ONE_TICK_SLIPPAGE_NOT_MODELED_BY_NAUTILUS",
                        "UNMODELED_SLIPPAGE",
                    }
                    for reason in reason_codes
                )
                else "modeled_one_tick"
                if cost_policy.slippage_model == "one_tick"
                else "modeled"
            )
            expected_reasons = _economic_reason_codes(
                policy,
                cost_policy,
                net,
                drawdown,
                cast(str, funding_truth),
                slippage_status,
            )
            if (
                reason_codes != expected_reasons
                or economic_status != ("PASS" if expected_reasons == ("CELL_ECONOMIC_PASS",) else "FAIL")
            ):
                raise StrategyRobustnessError(invalid)
        return (
            validated_cell,
            cast(str, technical_status),
            cast(str, economic_status),
            reason_codes,
            cast(str | None, funding_truth),
            references_missing,
        )
    except (InvalidOperation, KeyError, TypeError, ValueError, StrategyRobustnessError) as error:
        raise StrategyRobustnessError(invalid) from error


def _cell_derived_verdict_closure(verdict: Mapping[str, object]) -> dict[str, object]:
    shape = verdict.get("matrix_shape")
    cells = verdict.get("cells")
    if (
        not isinstance(shape, dict)
        or set(shape) != {
            "maximum_windows_per_scheme",
            "stress_scenarios",
            "window_schemes",
        }
        or not isinstance(cells, list)
        or not cells
    ):
        raise StrategyRobustnessError("robustness verdict cell-derived closure is invalid")
    policy_document = verdict.get("policy")
    try:
        frozen_policy = (
            load_robustness_policy(_REPOSITORY_POLICY_PATH)
            if policy_document is None
            else load_robustness_policy(canonical_json(policy_document))
        )
    except StrategyRobustnessError as error:
        raise StrategyRobustnessError("robustness verdict policy-bound matrix is invalid") from error
    policy_document = frozen_policy.document()
    if frozen_policy.policy_id != verdict.get("policy_id"):
        raise StrategyRobustnessError("robustness verdict policy-bound matrix is invalid")
    expected_shape = {
        "maximum_windows_per_scheme": policy_document.get("maximum_windows_per_scheme"),
        "stress_scenarios": policy_document.get("stress_scenarios"),
        "window_schemes": policy_document.get("window_schemes"),
    }
    if shape != expected_shape or any(
        verdict.get(field) != policy_document.get(field)
        for field in (
            "action_policy_version",
            "policy_version",
            "unmodeled_metrics",
            "unmodeled_metric_status",
        )
    ):
        raise StrategyRobustnessError("robustness verdict policy-bound matrix is invalid")
    maximum_windows = shape["maximum_windows_per_scheme"]
    if isinstance(maximum_windows, bool) or not isinstance(maximum_windows, int) or maximum_windows <= 0:
        raise StrategyRobustnessError("robustness verdict cell-derived closure is invalid")
    try:
        schemes = _string_tuple(shape["window_schemes"], "window_schemes")
        scenarios = _string_tuple(shape["stress_scenarios"], "stress_scenarios")
    except StrategyRobustnessError as error:
        raise StrategyRobustnessError("robustness verdict cell-derived closure is invalid") from error
    if schemes != ("expanding", "rolling") or scenarios != _STRESS_SCENARIOS:
        raise StrategyRobustnessError("robustness verdict cell-derived closure is invalid")

    actual_matrix: list[tuple[str, int, str]] = []
    technical_invalid = False
    economic_fail = False
    evidence_modeling_gap = False
    funding_truth_counts = {
        truth: 0
        for truth in ("official", "modeled_funding", "mixed", "missing")
    }
    performance_unclaimable = False
    cell_ids: list[str] = []
    validated_cells: list[RobustnessCell] = []
    for cell in cells:
        validated_cell, technical_status, economic_status, reason_codes, funding_truth, references_missing = (
            _validated_cell_document(cell, verdict, frozen_policy)
        )
        validated_cells.append(validated_cell)
        cell_ids.append(validated_cell.cell_id)
        actual_matrix.append(
            (
                validated_cell.window.scheme,
                validated_cell.window.ordinal,
                validated_cell.stress_scenario,
            ),
        )
        technical_invalid |= technical_status != "PASS"
        economic_fail |= economic_status == "FAIL"
        if technical_status == "PASS":
            evidence_modeling_gap |= cast(str, funding_truth) != "official"
            evidence_modeling_gap |= any(
                reason in _EVIDENCE_OR_MODELING_GAP_REASONS
                for reason in reason_codes
            )
            evidence_modeling_gap |= references_missing
        if funding_truth in funding_truth_counts:
            funding_truth_counts[cast(str, funding_truth)] += 1
        else:
            performance_unclaimable = True
        performance_unclaimable |= references_missing
        performance_unclaimable |= any(
            reason in {
                "ONE_TICK_SLIPPAGE_NOT_MODELED_BY_NAUTILUS",
                "UNMODELED_SLIPPAGE",
            }
            for reason in reason_codes
        )

    if len(cell_ids) != len(set(cell_ids)):
        raise StrategyRobustnessError("robustness verdict cell-derived closure is invalid")
    baseline_parameters = next(
        (cell.parameters for cell in validated_cells if cell.stress_scenario == "baseline"),
        None,
    )
    if baseline_parameters is None or any(
        cell.parameters
        != (
            parameter_neighborhood(baseline_parameters, cell.parameter_relative_offset)
            if cell.parameter_relative_offset
            else baseline_parameters
        )
        for cell in validated_cells
    ):
        raise StrategyRobustnessError("robustness verdict cell-derived closure is invalid")
    if verdict.get("evaluation_context_id") != robustness_evaluation_context_id(
        frozen_policy,
        validated_cells,
    ):
        raise StrategyRobustnessError("robustness verdict cell-derived closure is invalid")

    expected_matrix = {
        (scheme, ordinal, scenario)
        for scheme in schemes
        for ordinal in range(maximum_windows)
        for scenario in scenarios
    }
    complete = len(actual_matrix) == len(expected_matrix) and set(actual_matrix) == expected_matrix
    not_modeled = (
        verdict.get("unmodeled_metrics") == ["DSR", "PBO"]
        and verdict.get("unmodeled_metric_status") == "NOT_MODELED"
    )
    if not not_modeled:
        raise StrategyRobustnessError("robustness verdict cell-derived closure is invalid")
    performance_claimable = (
        complete
        and not technical_invalid
        and not economic_fail
        and not performance_unclaimable
        and funding_truth_counts["official"] == len(cells)
    )
    return {
        "action": (
            "FIX_TECHNICAL"
            if not complete or technical_invalid or evidence_modeling_gap
            else "MUTATE"
            if economic_fail
            else "ADVANCE"
        ),
        "cell_count": len(cells),
        "complete_matrix": complete,
        "economic_status": "FAIL" if economic_fail else "PASS",
        "funding_truth_counts": funding_truth_counts,
        "performance_claimable": performance_claimable,
        "reason_codes": [
            *(["TECHNICAL_INVALID"] if technical_invalid or not complete or evidence_modeling_gap else []),
            *(["EVIDENCE_OR_MODELING_GAP"] if evidence_modeling_gap else []),
            *(["ECONOMIC_ROBUSTNESS_FAILED"] if economic_fail and not evidence_modeling_gap else []),
            "DSR_PBO_NOT_MODELED",
        ],
        "status": (
            "TECHNICAL_INVALID"
            if technical_invalid or not complete or evidence_modeling_gap
            else "COMPLETE"
        ),
        "technical_status": (
            "ERROR"
            if technical_invalid or not complete or evidence_modeling_gap
            else "PASS"
        ),
    }


def build_robustness_verdict_v2(
    identity: Mapping[str, object],
    policy: RobustnessPolicy,
    cells: Sequence[RobustnessCell],
    results: Sequence[RobustnessCellResult],
    *,
    trial_context: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Create the canonical identity-bound robustness verdict and fail-closed action."""
    bound_identity = _validated_identity(identity)
    if bound_identity["policy_id"] != policy.policy_id:
        raise StrategyRobustnessError("robustness aggregate policy_id does not match frozen policy")
    if len(cells) != len(results) or not cells:
        raise StrategyRobustnessError("robustness verdict requires one result for every cell")
    if bound_identity["evaluation_context_id"] != robustness_evaluation_context_id(policy, cells):
        raise StrategyRobustnessError("robustness aggregate evaluation context mismatch")
    bound_trial_context = _validated_trial_context(trial_context)
    if (
        bound_trial_context["data_as_of_ns"] != bound_identity["data_as_of_ns"]
        or any(cell.window.data_as_of_ns != bound_identity["data_as_of_ns"] for cell in cells)
    ):
        raise StrategyRobustnessError("robustness data-as-of identity mismatch")
    if len({cell.cell_id for cell in cells}) != len(cells):
        raise StrategyRobustnessError("robustness cell IDs must be unique")
    if any(
        result.cell_id != cell.cell_id
        or result.evaluation_context_id != cell.evaluation_context_id
        for cell, result in zip(cells, results, strict=True)
    ):
        raise StrategyRobustnessError("robustness cell result identity mismatch")
    cell_documents = [
        _result_document(cell, result)
        for cell, result in zip(cells, results, strict=True)
    ]
    closure = _cell_derived_verdict_closure(
        {
            "data_as_of_ns": bound_identity["data_as_of_ns"],
            "evaluation_context_id": bound_identity["evaluation_context_id"],
            "cells": cell_documents,
            "matrix_shape": {
                "maximum_windows_per_scheme": policy.maximum_windows_per_scheme,
                "stress_scenarios": list(policy.stress_scenarios),
                "window_schemes": list(policy.window_schemes),
            },
            "unmodeled_metrics": list(policy.unmodeled_metrics),
            "unmodeled_metric_status": policy.unmodeled_metric_status,
            "action_policy_version": policy.action_policy_version,
            "policy": policy.document(),
            "policy_id": policy.policy_id,
            "policy_version": policy.policy_version,
            "trial_context": bound_trial_context,
        },
    )
    economic_results = [
        result for result in results
        if result.net_account_delta is not None and result.realized_balance_drawdown is not None
    ]
    worst_result = min(
        economic_results,
        key=lambda result: (-(result.realized_balance_drawdown or Decimal("0")), result.net_account_delta or Decimal("0")),
        default=None,
    )
    worst_cell = next((cell for cell in cells if worst_result is not None and cell.cell_id == worst_result.cell_id), None)
    verdict: dict[str, object] = {
        **bound_identity,
        **closure,
        "action_policy_version": policy.action_policy_version,
        "cells": cell_documents,
        "matrix_shape": {
            "maximum_windows_per_scheme": policy.maximum_windows_per_scheme,
            "stress_scenarios": list(policy.stress_scenarios),
            "window_schemes": list(policy.window_schemes),
        },
        "policy": policy.document(),
        "policy_version": policy.policy_version,
        "schema_version": "nautilus-verdict-v2",
        "tier": "ROBUSTNESS",
        "trial_context": bound_trial_context,
        "unmodeled_metrics": list(policy.unmodeled_metrics),
        "unmodeled_metric_status": policy.unmodeled_metric_status,
        "worst_window": None
        if worst_cell is None or worst_result is None
        else {
            "cell_id": worst_cell.cell_id,
            "net_account_delta": str(worst_result.net_account_delta),
            "realized_balance_drawdown": str(worst_result.realized_balance_drawdown),
            "scheme": worst_cell.window.scheme,
            "window_ordinal": worst_cell.window.ordinal,
        },
    }
    verdict["robustness_verdict_id"] = sha256(canonical_json(verdict)).hexdigest()
    return verdict


def load_robustness_verdict_v2(payload: bytes, *, expected_identity: Mapping[str, object] | None = None) -> dict[str, object]:
    try:
        value = json.loads(payload, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StrategyRobustnessError("robustness verdict must be UTF-8 JSON") from error
    if not isinstance(value, dict) or value.get("schema_version") != "nautilus-verdict-v2":
        raise StrategyRobustnessError("unsupported robustness verdict schema")
    if payload != canonical_json(value):
        raise StrategyRobustnessError("robustness verdict must use canonical JSON encoding")
    verdict_id = value.get("robustness_verdict_id")
    if not isinstance(verdict_id, str):
        raise StrategyRobustnessError("robustness_verdict_id is missing")
    preimage = dict(value)
    del preimage["robustness_verdict_id"]
    if sha256(canonical_json(preimage)).hexdigest() != verdict_id:
        raise StrategyRobustnessError("robustness verdict hash mismatch")
    _validated_identity({field: value.get(field) for field in _ROBUSTNESS_IDENTITY_FIELDS})
    _validated_trial_context(value.get("trial_context"))
    if expected_identity is not None and dict(expected_identity) != {field: value[field] for field in _ROBUSTNESS_IDENTITY_FIELDS}:
        raise StrategyRobustnessError("robustness verdict identity mismatch")
    closure = _cell_derived_verdict_closure(value)
    if any(value.get(field) != expected for field, expected in closure.items()):
        raise StrategyRobustnessError("robustness verdict cell-derived closure mismatch")
    return value


def build_feedback_v2(verdict: Mapping[str, object], *, parent_strategy_id: str | None = None) -> dict[str, object]:
    loaded = load_robustness_verdict_v2(canonical_json(dict(verdict)))
    feedback: dict[str, object] = {
        "action": loaded["action"],
        "action_policy_version": loaded["action_policy_version"],
        "experiment_id": loaded["experiment_id"],
        "hypothesis_id": loaded["hypothesis_id"],
        "parent_strategy_id": parent_strategy_id,
        "reason_codes": loaded["reason_codes"],
        "robustness_verdict_id": loaded["robustness_verdict_id"],
        "schema_version": "strategy-feedback-v2",
        "status": loaded["status"],
        "strategy_id": loaded["strategy_id"],
    }
    for field in _ROBUSTNESS_IDENTITY_FIELDS:
        feedback[field] = loaded[field]
    feedback["feedback_id"] = sha256(canonical_json(feedback)).hexdigest()
    return feedback


def build_action_v1(
    verdict: Mapping[str, object],
    *,
    changed_dimension: str | None = None,
    campaign_id: str | None = None,
    generation: int | None = None,
    child_hypothesis_id: str | None = None,
    child_strategy_id: str | None = None,
) -> dict[str, object]:
    loaded = load_robustness_verdict_v2(canonical_json(dict(verdict)))
    action = {
        "action": loaded["action"],
        "action_policy_version": loaded["action_policy_version"],
        "campaign_id": campaign_id,
        "changed_dimension": changed_dimension,
        "child_hypothesis_id": child_hypothesis_id,
        "child_strategy_id": child_strategy_id,
        "consumed_reason_codes": loaded["reason_codes"],
        "generation": generation,
        "reason_codes": loaded["reason_codes"],
        "robustness_verdict_id": loaded["robustness_verdict_id"],
        "schema_version": "strategy-action-v1",
        "source_tier": "ROBUSTNESS",
        "source_verdict_id": loaded["robustness_verdict_id"],
        "status": loaded["status"],
        "strategy_id": loaded["strategy_id"],
    }
    for field in _ROBUSTNESS_IDENTITY_FIELDS:
        action[field] = loaded[field]
    _validate_action_closure(action)
    action["action_id"] = sha256(canonical_json(action)).hexdigest()
    return action


def load_feedback_v2(payload: bytes) -> dict[str, object]:
    try:
        value = json.loads(payload, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StrategyRobustnessError("strategy feedback must be UTF-8 JSON") from error
    if not isinstance(value, dict) or value.get("schema_version") != "strategy-feedback-v2":
        raise StrategyRobustnessError("unsupported strategy feedback schema")
    if payload != canonical_json(value):
        raise StrategyRobustnessError("strategy feedback must use canonical JSON encoding")
    feedback_id = value.get("feedback_id")
    if not isinstance(feedback_id, str):
        raise StrategyRobustnessError("feedback_id is missing")
    preimage = dict(value)
    del preimage["feedback_id"]
    if sha256(canonical_json(preimage)).hexdigest() != feedback_id:
        raise StrategyRobustnessError("strategy feedback hash mismatch")
    _validated_identity({field: value.get(field) for field in _ROBUSTNESS_IDENTITY_FIELDS})
    if not isinstance(value.get("robustness_verdict_id"), str):
        raise StrategyRobustnessError("feedback robustness verdict identity is missing")
    if value.get("action") not in {"ADVANCE", "HOLD", "MUTATE", "NEW_FAMILY", "KILL", "FIX_TECHNICAL"}:
        raise StrategyRobustnessError("unsupported feedback action")
    return value


def _validate_action_closure(action: Mapping[str, object]) -> None:
    if action.get("action") in {"MUTATE", "NEW_FAMILY"}:
        ids = (
            action.get("campaign_id"),
            action.get("child_hypothesis_id"),
            action.get("child_strategy_id"),
        )
        generation = action.get("generation")
        if (
            any(
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
                for value in ids
            )
            or not isinstance(action.get("changed_dimension"), str)
            or not action["changed_dimension"]
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
        ):
            raise StrategyRobustnessError("economic mutation lineage is incomplete")
    elif action.get("action") == "FIX_TECHNICAL" and any(
        action.get(field) is not None
        for field in (
            "changed_dimension",
            "child_hypothesis_id",
            "child_strategy_id",
            "generation",
        )
    ):
        raise StrategyRobustnessError("technical robustness action cannot create mutation lineage")
    elif any(
        action.get(field) is not None
        for field in (
            "changed_dimension",
            "child_hypothesis_id",
            "child_strategy_id",
            "generation",
        )
    ):
        raise StrategyRobustnessError("non-mutation action cannot create mutation lineage")


def load_action_v1(payload: bytes) -> dict[str, object]:
    try:
        value = json.loads(payload, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StrategyRobustnessError("strategy action must be UTF-8 JSON") from error
    action_fields = frozenset(
        {
            "action",
            "action_id",
            "action_policy_version",
            "campaign_id",
            "changed_dimension",
            "child_hypothesis_id",
            "child_strategy_id",
            "candidate_id",
            "code_commit",
            "consumed_reason_codes",
            "data_as_of_ns",
            "data_snapshot_id",
            "data_source_id",
            "engine_id",
            "evaluation_context_id",
            "experiment_id",
            "generation",
            "hypothesis_id",
            "policy_id",
            "reason_codes",
            "robustness_verdict_id",
            "runtime_id",
            "schema_version",
            "source_tier",
            "source_verdict_id",
            "status",
            "strategy_id",
        },
    )
    legacy_action_fields = action_fields - {"child_hypothesis_id"}
    if (
        not isinstance(value, dict)
        or set(value) not in {action_fields, legacy_action_fields}
        or value.get("schema_version") != "strategy-action-v1"
    ):
        raise StrategyRobustnessError("unsupported strategy action schema")
    if payload != canonical_json(value):
        raise StrategyRobustnessError("strategy action must use canonical JSON encoding")
    action_id = value.get("action_id")
    if not isinstance(action_id, str):
        raise StrategyRobustnessError("action_id is missing")
    preimage = dict(value)
    del preimage["action_id"]
    if sha256(canonical_json(preimage)).hexdigest() != action_id:
        raise StrategyRobustnessError("strategy action hash mismatch")
    if value.get("action") not in {"ADVANCE", "HOLD", "MUTATE", "NEW_FAMILY", "KILL", "FIX_TECHNICAL"}:
        raise StrategyRobustnessError("unsupported strategy action")
    if value.get("source_tier") != "ROBUSTNESS" or value.get("source_verdict_id") != value.get("robustness_verdict_id"):
        raise StrategyRobustnessError("strategy action source identity is invalid")
    if value.get("consumed_reason_codes") != value.get("reason_codes"):
        raise StrategyRobustnessError("strategy action reason codes are inconsistent")
    _validate_action_closure(value)
    return value


# Explicit contract names kept alongside the concise internal builders.
build_nautilus_verdict_v2 = build_robustness_verdict_v2
load_nautilus_verdict_v2 = load_robustness_verdict_v2
build_strategy_feedback_v2 = build_feedback_v2
load_strategy_feedback_v2 = load_feedback_v2
build_strategy_action_v1 = build_action_v1
load_strategy_action_v1 = load_action_v1


def _mutation_action_inputs(
    candidate: Mapping[str, object],
    source_hypothesis: Any,
    based_on_verdict_id: str,
    policy: RobustnessPolicy,
) -> dict[str, object]:
    strategy = candidate.get("strategy")
    if not isinstance(strategy, Mapping):
        raise StrategyRobustnessError("candidate strategy identity is missing")
    parameters = strategy.get("parameters")
    if not isinstance(parameters, dict) or not parameters:
        raise StrategyRobustnessError("candidate mutation parameters are missing")
    if (
        getattr(source_hypothesis, "identity_schema", None) != "strategy-id-v2"
        or getattr(source_hypothesis, "parameters", None) is None
        or parameters != source_hypothesis.parameters.values
        or strategy.get("family_id") != source_hypothesis.family_id
        or strategy.get("family_version") != source_hypothesis.family_version
        or not isinstance(based_on_verdict_id, str)
        or len(based_on_verdict_id) != 64
        or any(character not in "0123456789abcdef" for character in based_on_verdict_id)
    ):
        raise StrategyRobustnessError("candidate mutation source hypothesis is inconsistent")
    changed_dimension: str | None = None
    mutated = dict(parameters)
    for name in sorted(parameters):
        changed = parameter_neighborhood(
            {name: parameters[name]},
            policy.parameter_relative_offsets[1],
        )[name]
        if changed != parameters[name]:
            changed_dimension = name
            mutated[name] = changed
            break
    if changed_dimension is None:
        raise StrategyRobustnessError("candidate mutation has no effective parameter change")
    child_strategy_document = {
        "bar_type": candidate.get("bar_type"),
        "family_version": strategy.get("family_version"),
        "identity_schema": "strategy-id-v2",
        "instrument_id": candidate.get("instrument_id"),
        "parameters": mutated,
        "strategy_family": strategy.get("family_id"),
    }
    if any(
        not isinstance(child_strategy_document[field], str) or not child_strategy_document[field]
        for field in ("bar_type", "family_version", "instrument_id", "strategy_family")
    ):
        raise StrategyRobustnessError("candidate mutation identity is incomplete")
    child_strategy_id = sha256(canonical_json(child_strategy_document)).hexdigest()
    if child_strategy_id == source_hypothesis.strategy_id:
        raise StrategyRobustnessError("candidate mutation did not change child strategy identity")
    child_document = {
        "bar_type": child_strategy_document["bar_type"],
        "based_on_verdict_id": based_on_verdict_id,
        "falsification": source_hypothesis.falsification,
        "family_version": child_strategy_document["family_version"],
        "instrument_id": child_strategy_document["instrument_id"],
        "parameters": mutated,
        "parent_strategy_id": source_hypothesis.strategy_id,
        "schema_version": "strategy-hypothesis-v2",
        "strategy_family": child_strategy_document["strategy_family"],
        "thesis": source_hypothesis.thesis,
    }
    child_hypothesis_payload = canonical_json(child_document)
    # ponytail: Card 3 emits only the first deterministic child generation. A
    # recursive loop must read and increment the parent action generation.
    return {
        "changed_dimension": changed_dimension,
        "child_hypothesis_id": sha256(child_hypothesis_payload).hexdigest(),
        "child_hypothesis_payload": child_hypothesis_payload,
        "child_strategy_id": child_strategy_id,
        "generation": 1,
    }


def _persisted_run_summary(
    *,
    action: Mapping[str, object],
    campaign_id: str,
    candidate_id: str,
    feedback: Mapping[str, object],
    funnel: Mapping[str, int],
    record: object,
    reused: bool,
    verdict: Mapping[str, object],
) -> dict[str, object]:
    return {
        "action": action["action"],
        "action_id": action["action_id"],
        "action_path": getattr(record, "action_path"),
        "action_sha256": getattr(record, "action_sha256"),
        "campaign_id": campaign_id,
        "candidate_id": candidate_id,
        "child_hypothesis_id": action.get("child_hypothesis_id"),
        "child_strategy_id": action.get("child_strategy_id"),
        "cell_count": verdict["cell_count"],
        "economic_status": getattr(record, "economic_status"),
        "evaluation_context_id": verdict["evaluation_context_id"],
        "experiment_id": verdict["experiment_id"],
        "feedback_id": feedback["feedback_id"],
        "feedback_path": getattr(record, "feedback_path"),
        "feedback_sha256": getattr(record, "feedback_sha256"),
        "funnel": dict(funnel),
        "reason_codes": list(getattr(record, "reason_codes")),
        "reused": reused,
        "robustness_verdict_id": verdict["robustness_verdict_id"],
        "schema_version": "formal-robustness-run-v1",
        "technical_status": getattr(record, "technical_status"),
        "verdict_path": getattr(record, "verdict_path"),
        "verdict_sha256": getattr(record, "verdict_sha256"),
    }


def _verify_formal_cell_artifacts(
    verdict: Mapping[str, object],
    artifact_directory: Path,
) -> None:
    from .candidate_backtest import load_candidate_backtest_verdict

    cells = verdict.get("cells")
    experiment_id = verdict.get("experiment_id")
    if not isinstance(cells, list) or not isinstance(experiment_id, str):
        raise StrategyRobustnessError("formal cell artifact references are invalid")
    claimable_advance = verdict.get("action") == "ADVANCE" and verdict.get("performance_claimable") is True
    for cell in cells:
        if not isinstance(cell, dict):
            raise StrategyRobustnessError("formal cell artifact references are invalid")
        cell_id = cell.get("cell_id")
        artifact_sha256 = cell.get("artifact_sha256")
        verdict_id = cell.get("verdict_id")
        if artifact_sha256 is None and verdict_id is None:
            if claimable_advance:
                raise StrategyRobustnessError("formal cell artifact references are invalid")
            continue
        if any(
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in (cell_id, artifact_sha256, verdict_id)
        ) or artifact_sha256 != verdict_id:
            raise StrategyRobustnessError("formal cell artifact references are invalid")
        path = (
            Path(artifact_directory)
            / experiment_id
            / "cells"
            / cast(str, cell_id)
            / "nautilus-verdict-v1.json"
        )
        try:
            payload = path.read_bytes()
            decoded = load_candidate_backtest_verdict(payload)
        except (OSError, RuntimeError) as error:
            raise StrategyRobustnessError("formal cell artifact readback failed") from error
        if sha256(payload).hexdigest() != artifact_sha256 or canonical_json(decoded) != payload:
            raise StrategyRobustnessError("formal cell artifact readback failed")
        cost_policy = decoded.get("cost_policy")
        execution = decoded.get("execution")
        aggregate_cost_policy = cell.get("cost_policy")
        if (
            not isinstance(cost_policy, dict)
            or not isinstance(execution, dict)
            or not isinstance(aggregate_cost_policy, dict)
            or cost_policy.get("cost_policy_id") != cell.get("cost_policy_id")
            or execution.get("slippage_status")
            != (
                "modeled_one_tick"
                if aggregate_cost_policy.get("slippage_model") == "one_tick"
                else "modeled"
                if aggregate_cost_policy.get("slippage_model") == "none"
                else None
            )
        ):
            raise StrategyRobustnessError("formal cell artifact policy binding is invalid")


def run_persisted_robustness(
    *,
    ledger: Any,
    campaign_id: str,
    market_data_path: Path,
    catalog_path: Path,
    funding_path: Path,
    accounting_policy_path: Path,
    robustness_policy_path: Path,
    artifact_directory: Path,
    candidate_id: str | None = None,
    evaluator: Callable[[object, RobustnessCell], object] | None = None,
) -> dict[str, object]:
    """Evaluate one persisted Card 2 survivor and append its complete Card 3 chain."""
    from .candidate_backtest import (
        _funding_symbols,
        _load_policy,
        CandidateBacktestRequest,
        run_signal_parity_gate,
        validated_candidate_source_bars,
    )
    from .funding_observation import OFFICIAL, read_funding_observations
    from .pybroker_candidate import load_pybroker_candidate
    from .strategy_lab import (
        _atomic_publish,
        _hash_tree,
        experiment_id,
        load_strategy_hypothesis,
        robustness_experiment_identity,
        StrategyLoopPaths,
    )

    policy = load_robustness_policy(robustness_policy_path)
    trial_context = ledger.robustness_trial_context(campaign_id)
    survivor = ledger.robustness_survivor_context(campaign_id, candidate_id)
    source_id = _hash_tree(
        StrategyLoopPaths(
            market_data_path=market_data_path,
            policy_path=accounting_policy_path,
            catalog_path=catalog_path,
            funding_path=funding_path,
            state_path=artifact_directory,
        ),
    )
    if source_id != survivor.base_identity.data_source_id:
        raise StrategyRobustnessError("formal robustness data source identity mismatch")
    loaded_candidate, loaded_candidate_id = load_pybroker_candidate(survivor.candidate_path)
    candidate = cast(dict[str, Any], loaded_candidate)
    if loaded_candidate_id != survivor.candidate_id:
        raise StrategyRobustnessError("persisted survivor candidate identity mismatch")
    signal_parity = run_signal_parity_gate(survivor.candidate_path, catalog_path)
    accounting_policy = _load_policy(accounting_policy_path)
    formal_start_ns = accounting_policy.historical_start_ns
    if accounting_policy.official_only_window_start == "first_official_funding_observation":
        instrument_id = candidate.get("instrument_id")
        if (
            not isinstance(instrument_id, str)
            or not instrument_id.endswith("-PERP.BINANCE")
        ):
            raise StrategyRobustnessError(
                "persisted survivor candidate instrument identity is incomplete",
            )
        symbol = instrument_id.partition("-PERP.")[0]
        observations = read_funding_observations(
            funding_path,
            symbols=_funding_symbols(funding_path),
        ).get(symbol)
        if not observations:
            raise StrategyRobustnessError(
                f"official-only Funding policy has no observations for {symbol}",
            )
        first_official_ns = next(
            (item.funding_time_ns for item in observations if item.truth_status == OFFICIAL),
            None,
        )
        if first_official_ns is None:
            raise StrategyRobustnessError(
                "official-only Funding policy cannot derive a first official "
                f"observation for {symbol}",
            )
        formal_start_ns = max(formal_start_ns, first_official_ns)
    bars = tuple(
        bar
        for bar in validated_candidate_source_bars(candidate, catalog_path)
        if bar.ts_event >= formal_start_ns
    )
    if not bars:
        raise StrategyRobustnessError(
            "official-only Funding policy leaves no persisted survivor bars "
            "after official coverage",
        )
    timestamps = tuple(bar.ts_event for bar in bars)
    data_as_of_ns = survivor.base_identity.data_as_of_ns
    if data_as_of_ns is None or data_as_of_ns != trial_context.get("data_as_of_ns"):
        raise StrategyRobustnessError("persisted survivor data-as-of identity mismatch")
    windows = generate_robustness_windows(
        timestamps,
        policy,
        evaluation_start_ns=timestamps[0],
        evaluation_end_ns=min(timestamps[-1], data_as_of_ns),
        data_as_of_ns=data_as_of_ns,
        evaluation_context_id=survivor.candidate_evaluation_context_id,
        closed_bars=bars,
    )
    strategy = candidate.get("strategy")
    parameters = strategy.get("parameters") if isinstance(strategy, dict) else None
    if not isinstance(parameters, dict):
        raise StrategyRobustnessError("persisted survivor candidate parameters are missing")
    cells = generate_robustness_matrix(parameters, windows, policy)
    evaluation_context_id = robustness_evaluation_context_id(policy, cells)
    identity = robustness_experiment_identity(
        survivor.base_identity,
        policy.policy_id,
        evaluation_context_id,
    )
    derived_experiment_id = experiment_id(identity)
    existing = ledger.existing_robustness(
        derived_experiment_id,
        evaluation_context_id,
        policy.policy_id,
    )
    if existing is not None:
        verdict = load_robustness_verdict_v2(Path(existing.verdict_path).read_bytes())
        feedback = load_feedback_v2(Path(existing.feedback_path).read_bytes())
        action = load_action_v1(Path(existing.action_path).read_bytes())
        if (
            verdict.get("candidate_id") != loaded_candidate_id
            or verdict.get("trial_context") != trial_context
            or action.get("campaign_id") != campaign_id
        ):
            raise StrategyRobustnessError("persisted robustness reuse identity mismatch")
        _verify_formal_cell_artifacts(verdict, artifact_directory)
        return _persisted_run_summary(
            action=action,
            campaign_id=campaign_id,
            candidate_id=loaded_candidate_id,
            feedback=feedback,
            funnel=ledger.robustness_funnel(),
            record=existing,
            reused=True,
            verdict=verdict,
        )

    request = CandidateBacktestRequest(
        candidate_path=survivor.candidate_path,
        catalog_path=catalog_path,
        funding_path=funding_path,
        policy_path=accounting_policy_path,
        hypothesis_id=survivor.hypothesis_id,
        strategy_id=survivor.strategy_id,
        experiment_id=derived_experiment_id,
        code_commit=survivor.code_commit,
        evaluation_start_utc=_utc_z_from_ns(timestamps[0]),
        evaluation_end_utc=_utc_z_from_ns(min(timestamps[-1], data_as_of_ns)),
        data_as_of_ns=data_as_of_ns,
        evaluation_context_id=evaluation_context_id,
        candidate_evaluation_context_id=survivor.candidate_evaluation_context_id,
        signal_parity=signal_parity,
    )
    formal_evaluator = evaluator or FormalNautilusEvaluator()
    created_cell_paths: list[Path] = []

    def publish_cell(request_value: object, cell: RobustnessCell) -> object:
        result = formal_evaluator(request_value, cell)
        payload = getattr(result, "canonical_bytes", None)
        verdict_id = getattr(result, "verdict_id", None)
        if not isinstance(payload, bytes) or sha256(payload).hexdigest() != verdict_id:
            raise StrategyRobustnessError("formal cell artifact identity mismatch")
        path = (
            Path(artifact_directory)
            / derived_experiment_id
            / "cells"
            / cell.cell_id
            / "nautilus-verdict-v1.json"
        )
        existed = path.exists()
        try:
            if _atomic_publish(path, payload) != verdict_id:
                raise StrategyRobustnessError("formal cell artifact readback mismatch")
        finally:
            if not existed and path.exists():
                created_cell_paths.append(path)
        return result

    def cleanup_created_cells() -> None:
        for path in reversed(created_cell_paths):
            path.unlink(missing_ok=True)
        for path in reversed(created_cell_paths):
            try:
                path.parent.rmdir()
            except OSError:
                pass
        for directory in (
            Path(artifact_directory) / derived_experiment_id / "cells",
            Path(artifact_directory) / derived_experiment_id,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass

    try:
        evaluated_cells, results = evaluate_robustness_matrix(
            request,
            windows,
            policy,
            evaluator=publish_cell,
        )
        if evaluated_cells != cells:
            raise StrategyRobustnessError("formal robustness matrix identity drift")
        bound_identity = {
            "candidate_id": loaded_candidate_id,
            "code_commit": survivor.code_commit,
            "data_as_of_ns": data_as_of_ns,
            "data_snapshot_id": identity.data_snapshot_id,
            "data_source_id": identity.data_source_id,
            "engine_id": identity.engine_id,
            "evaluation_context_id": evaluation_context_id,
            "experiment_id": derived_experiment_id,
            "hypothesis_id": survivor.hypothesis_id,
            "policy_id": policy.policy_id,
            "runtime_id": identity.runtime_id,
            "strategy_id": survivor.strategy_id,
        }
        verdict = build_robustness_verdict_v2(
            bound_identity,
            policy,
            cells,
            results,
            trial_context=trial_context,
        )
        feedback = build_feedback_v2(verdict)
        child_hypothesis_payload: bytes | None = None
        if verdict["action"] in {"MUTATE", "NEW_FAMILY"}:
            source_hypothesis = load_strategy_hypothesis(survivor.hypothesis_path)
            if (
                source_hypothesis.hypothesis_id != survivor.hypothesis_id
                or source_hypothesis.strategy_id != survivor.strategy_id
            ):
                raise StrategyRobustnessError("persisted mutation source hypothesis identity mismatch")
            action_inputs = _mutation_action_inputs(
                candidate,
                source_hypothesis,
                survivor.historical_verdict_id,
                policy,
            )
            child_hypothesis_payload = cast(bytes, action_inputs["child_hypothesis_payload"])
            action = build_action_v1(
                verdict,
                campaign_id=campaign_id,
                changed_dimension=cast(str, action_inputs["changed_dimension"]),
                child_hypothesis_id=cast(str, action_inputs["child_hypothesis_id"]),
                child_strategy_id=cast(str, action_inputs["child_strategy_id"]),
                generation=cast(int, action_inputs["generation"]),
            )
        else:
            action = build_action_v1(verdict, campaign_id=campaign_id)
        _verify_formal_cell_artifacts(verdict, artifact_directory)
        publish_kwargs = {
            "experiment_hypothesis_id": survivor.hypothesis_id,
            "experiment_identity": identity,
            **(
                {"child_hypothesis_payload": child_hypothesis_payload}
                if child_hypothesis_payload is not None
                else {}
            ),
        }
        record = ledger.publish_robustness(
            artifact_directory,
            verdict,
            feedback,
            action,
            **publish_kwargs,
        )
    except Exception:
        cleanup_created_cells()
        raise
    return _persisted_run_summary(
        action=action,
        campaign_id=campaign_id,
        candidate_id=loaded_candidate_id,
        feedback=feedback,
        funnel=ledger.robustness_funnel(),
        record=record,
        reused=False,
        verdict=verdict,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run one persisted Card 2 survivor and publish its complete Card 3 chain."""
    parser = argparse.ArgumentParser(description="Run persisted formal Nautilus robustness")
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--candidate-id")
    parser.add_argument("--market-data", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--funding", type=Path, required=True)
    parser.add_argument("--accounting-policy", type=Path, required=True)
    parser.add_argument("--robustness-policy", type=Path, required=True)
    parser.add_argument("--artifact-directory", type=Path, required=True)
    args = parser.parse_args(argv)

    from .strategy_lab import StrategyLedger

    ledger = StrategyLedger(args.ledger)
    ledger.initialize()
    summary = run_persisted_robustness(
        ledger=ledger,
        campaign_id=args.campaign_id,
        candidate_id=args.candidate_id,
        market_data_path=args.market_data,
        catalog_path=args.catalog,
        funding_path=args.funding,
        accounting_policy_path=args.accounting_policy,
        robustness_policy_path=args.robustness_policy,
        artifact_directory=args.artifact_directory,
    )
    print(canonical_json(summary).decode(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
