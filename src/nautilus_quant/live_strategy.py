from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Literal, Mapping

from nautilus_trader.core import UUID4
from nautilus_trader.model import (
    Bar,
    BarType,
    ContingencyType,
    InstrumentId,
    MarketOrder,
    MarkPriceUpdate,
    OrderSide,
    Quantity,
    TimeInForce,
)
from nautilus_trader.trading import Strategy

from .strategy_families import (
    ClosedBar,
    FamilyDecision,
    FamilyKernelError,
    IncrementalFamilyEvaluator,
    restore_incremental,
)


class LiveStrategyError(ValueError):
    """Raised when prospective strategy input violates a frozen boundary."""


@dataclass(frozen=True, slots=True)
class RiskExecutionPolicy:
    policy_id: str
    schema_version: str
    allow_live_execution: bool
    position_intents: tuple[str, ...]
    maximum_quantity: Decimal
    maximum_notional: Decimal
    maximum_loss: Decimal
    default_leverage: Decimal
    gross_exposure_limit: Decimal
    net_exposure_limit: Decimal
    per_symbol_exposure_limit: Decimal
    order_mapping: str
    reduce_only_exit: bool
    fee_treatment: str
    slippage_treatment: str
    funding_treatment: str
    stale_data_after_seconds: int
    reconnect_behavior: str
    duplicate_order_prevention: str
    kill_switch: bool
    flatten_on_exit: bool
    maximum_order_rate: str


def _canonical_json(value: object) -> bytes:
    try:
        return (json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode()
    except (TypeError, ValueError) as error:
        raise LiveStrategyError("value must be finite plain JSON") from error


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise LiveStrategyError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _positive_decimal(value: object, field: str) -> Decimal:
    try:
        normalized = Decimal(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else None
    except InvalidOperation as error:
        raise LiveStrategyError(f"{field} must be a positive decimal") from error
    if normalized is None or not normalized.is_finite() or normalized <= 0:
        raise LiveStrategyError(f"{field} must be a positive decimal")
    return normalized


def load_risk_execution_policy(path: Path | bytes) -> RiskExecutionPolicy:
    payload = path if isinstance(path, bytes) else Path(path).read_bytes()
    try:
        value = json.loads(payload, object_pairs_hook=_unique_object, parse_constant=lambda item: (_ for _ in ()).throw(LiveStrategyError(f"non-finite policy value: {item}")))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LiveStrategyError("risk policy must be UTF-8 JSON") from error
    if not isinstance(value, dict) or payload != _canonical_json(value):
        raise LiveStrategyError("risk policy hash requires canonical JSON bytes")
    expected = {
        "allow_live_execution", "default_leverage", "duplicate_order_prevention",
        "fee_treatment", "flatten_on_exit", "funding_treatment", "gross_exposure_limit",
        "kill_switch", "maximum_loss", "maximum_notional", "maximum_order_rate",
        "maximum_quantity", "net_exposure_limit", "order_mapping",
        "per_symbol_exposure_limit", "position_intents", "reconnect_behavior",
        "reduce_only_exit", "schema_version", "slippage_treatment",
        "stale_data_after_seconds",
    }
    if set(value) != expected or value["schema_version"] != "strategy-risk-execution-policy-v1":
        raise LiveStrategyError("invalid risk policy fields or schema")
    if value["allow_live_execution"] is not False:
        raise LiveStrategyError("risk policy must fail closed on live execution")
    if value["position_intents"] != ["FLAT", "LONG"]:
        raise LiveStrategyError("risk policy supports only FLAT and LONG")
    if value["order_mapping"] != "MARKET" or value["reduce_only_exit"] is not True:
        raise LiveStrategyError("risk policy order mapping is invalid")
    if value["kill_switch"] is not True or value["flatten_on_exit"] is not True:
        raise LiveStrategyError("risk policy requires kill switch and flatten-on-exit")
    stale = value["stale_data_after_seconds"]
    if isinstance(stale, bool) or not isinstance(stale, int) or stale < 1:
        raise LiveStrategyError("stale_data_after_seconds must be positive")
    text_fields = (
        "duplicate_order_prevention", "fee_treatment", "funding_treatment",
        "maximum_order_rate", "reconnect_behavior", "slippage_treatment",
    )
    if any(not isinstance(value[field], str) or not value[field] for field in text_fields):
        raise LiveStrategyError("risk policy text fields must be non-empty")
    policy = RiskExecutionPolicy(
        policy_id=sha256(payload).hexdigest(),
        schema_version=value["schema_version"],
        allow_live_execution=False,
        position_intents=("FLAT", "LONG"),
        maximum_quantity=_positive_decimal(value["maximum_quantity"], "maximum_quantity"),
        maximum_notional=_positive_decimal(value["maximum_notional"], "maximum_notional"),
        maximum_loss=_positive_decimal(value["maximum_loss"], "maximum_loss"),
        default_leverage=_positive_decimal(value["default_leverage"], "default_leverage"),
        gross_exposure_limit=_positive_decimal(value["gross_exposure_limit"], "gross_exposure_limit"),
        net_exposure_limit=_positive_decimal(value["net_exposure_limit"], "net_exposure_limit"),
        per_symbol_exposure_limit=_positive_decimal(value["per_symbol_exposure_limit"], "per_symbol_exposure_limit"),
        order_mapping=value["order_mapping"],
        reduce_only_exit=True,
        fee_treatment=value["fee_treatment"],
        slippage_treatment=value["slippage_treatment"],
        funding_treatment=value["funding_treatment"],
        stale_data_after_seconds=stale,
        reconnect_behavior=value["reconnect_behavior"],
        duplicate_order_prevention=value["duplicate_order_prevention"],
        kill_switch=True,
        flatten_on_exit=True,
        maximum_order_rate=value["maximum_order_rate"],
    )
    if policy.maximum_notional > min(
        policy.gross_exposure_limit,
        policy.net_exposure_limit,
        policy.per_symbol_exposure_limit,
    ):
        raise LiveStrategyError("maximum_notional exceeds the frozen exposure envelope")
    return policy


class FamilyStrategy(Strategy):
    """Shared completed-bar strategy for Shadow and sandbox Paper composition roots."""

    def __new__(cls, **_kwargs: object) -> FamilyStrategy:
        return super().__new__(cls)

    def __init__(
        self,
        *,
        family_id: str,
        family_version: str,
        parameters: Mapping[str, Any],
        risk_policy: RiskExecutionPolicy,
        mode: Literal["SHADOW", "PAPER"],
        instrument_id: InstrumentId | str | None = None,
        bar_type: BarType | str | None = None,
        quantity: Quantity | str | None = None,
        expected_interval_ns: int | None = None,
    ) -> None:
        if mode not in {"SHADOW", "PAPER"}:
            raise LiveStrategyError("mode must be SHADOW or PAPER")
        super().__init__()
        self.family_id = family_id
        self.family_version = family_version
        self.parameters = dict(parameters)
        self.risk_policy = risk_policy
        self.mode = mode
        self.instrument_id = (
            InstrumentId.from_str(instrument_id) if isinstance(instrument_id, str) else instrument_id
        )
        self.bar_type = BarType.from_str(bar_type) if isinstance(bar_type, str) else bar_type
        self.quantity = Quantity.from_str(quantity) if isinstance(quantity, str) else quantity
        if self.quantity is not None and Decimal(str(self.quantity)) > risk_policy.maximum_quantity:
            raise LiveStrategyError("quantity exceeds maximum_quantity")
        if expected_interval_ns is not None and (
            isinstance(expected_interval_ns, bool)
            or not isinstance(expected_interval_ns, int)
            or expected_interval_ns < 1
        ):
            raise LiveStrategyError("expected_interval_ns must be positive")
        self.expected_interval_ns = expected_interval_ns
        self._evaluator = IncrementalFamilyEvaluator(
            family_id=family_id,
            family_version=family_version,
            parameters=parameters,
        )
        self._order_intents: list[tuple[str, str]] = []
        self._reason_codes: list[str] = []
        self._closed_bars: list[ClosedBar] = []
        self._decisions: list[FamilyDecision] = []
        self._mark_prices: list[tuple[int, str]] = []
        self._current_intent = "FLAT"
        self.nautilus_order_intents: dict[str, str] = {}
        self.technical_status = "PASS"

    @property
    def order_intents(self) -> tuple[tuple[str, str], ...]:
        return tuple(self._order_intents)

    @property
    def reason_codes(self) -> tuple[str, ...]:
        return tuple(self._reason_codes)

    @property
    def closed_bars(self) -> tuple[ClosedBar, ...]:
        return tuple(self._closed_bars)

    @property
    def decisions(self) -> tuple[FamilyDecision, ...]:
        return tuple(self._decisions)

    @property
    def mark_prices(self) -> tuple[tuple[int, str], ...]:
        return tuple(self._mark_prices)

    def on_closed_bar(self, current_bar: ClosedBar) -> FamilyDecision | None:
        if (
            self.expected_interval_ns is not None
            and self._evaluator.last_ts_event_ns is not None
            and current_bar.ts_event_ns - self._evaluator.last_ts_event_ns
            != self.expected_interval_ns
        ):
            self.technical_status = "ERROR"
            self._reason_codes.append("MISSING_OR_REVISED_CLOSED_BAR")
            raise LiveStrategyError("closed-bar gap or revision detected")
        try:
            decision = self._evaluator.push(current_bar)
        except FamilyKernelError as error:
            self.technical_status = "ERROR"
            self._reason_codes.append("INVALID_CLOSED_BAR")
            raise LiveStrategyError(str(error)) from error
        if decision is None:
            self._closed_bars.append(current_bar)
            return None
        self._closed_bars.append(current_bar)
        self._decisions.append(decision)
        if self.mode == "PAPER" and decision.target_intent != self._current_intent:
            self._order_intents.append((decision.signal_id, decision.target_intent))
        self._current_intent = decision.target_intent
        return decision

    def on_start(self) -> None:
        if self.bar_type is None:
            raise LiveStrategyError("registered FamilyStrategy requires bar_type")
        self.subscribe_bars(self.bar_type)
        if self.instrument_id is not None:
            self.subscribe_mark_prices(self.instrument_id)

    def on_bar(self, value: Bar) -> None:
        before = len(self._order_intents)
        self.on_closed_bar(
            ClosedBar(
                ts_event_ns=value.ts_event,
                open=float(str(value.open)),
                high=float(str(value.high)),
                low=float(str(value.low)),
                close=float(str(value.close)),
                volume=float(str(value.volume)),
            )
        )
        if self.mode == "PAPER" and len(self._order_intents) > before:
            self._submit_intent(self._order_intents[-1][1])

    def on_mark_price(self, value: MarkPriceUpdate) -> None:
        self._mark_prices.append((value.ts_event, str(value.value)))

    def _submit_intent(self, intent: str) -> None:
        if self.trader_id is None or self.instrument_id is None or self.quantity is None:
            raise LiveStrategyError("Paper FamilyStrategy requires execution identity")
        client_order_id = self.order_factory.generate_client_order_id()
        order = MarketOrder(
            trader_id=self.trader_id,
            strategy_id=self.strategy_id,
            instrument_id=self.instrument_id,
            client_order_id=client_order_id,
            order_side=OrderSide.BUY if intent == "LONG" else OrderSide.SELL,
            quantity=self.quantity,
            init_id=UUID4(),
            ts_init=self.clock.timestamp_ns(),
            time_in_force=TimeInForce.GTC,
            reduce_only=intent == "FLAT",
            quote_quantity=False,
            contingency_type=ContingencyType.NO_CONTINGENCY,
        )
        self.nautilus_order_intents[str(client_order_id)] = intent
        self.submit_order(order)

    def trip_circuit_breaker(self, reason: str) -> str | None:
        if not isinstance(reason, str) or not reason:
            raise LiveStrategyError("circuit-breaker reason must be non-empty")
        if self.technical_status != "ERROR":
            self.technical_status = "ERROR"
            self._reason_codes.append(reason)
        if self.mode == "PAPER" and self._current_intent == "LONG":
            signal_id = sha256(_canonical_json({"last_ts_event_ns": self._evaluator.last_ts_event_ns, "reason": reason, "risk_policy_id": self.risk_policy.policy_id})).hexdigest()
            self._order_intents.append((signal_id, "FLAT"))
            self._current_intent = "FLAT"
            return "FLAT"
        return None

    def observe_account_loss(self, loss: Decimal | int | str) -> str | None:
        try:
            amount = Decimal(loss)
        except InvalidOperation as error:
            raise LiveStrategyError("account loss must be a finite decimal") from error
        if not amount.is_finite() or amount < 0:
            raise LiveStrategyError("account loss must be a finite non-negative decimal")
        return self.trip_circuit_breaker("MAXIMUM_LOSS") if amount >= self.risk_policy.maximum_loss else None

    def check_stale(self, now_ns: int) -> str | None:
        if isinstance(now_ns, bool) or not isinstance(now_ns, int) or now_ns < 0:
            raise LiveStrategyError("now_ns must be non-negative")
        last = self._evaluator.last_ts_event_ns
        if last is None or now_ns - last > self.risk_policy.stale_data_after_seconds * 1_000_000_000:
            return self.trip_circuit_breaker("STALE_DATA")
        return None

    def shutdown_flatten(self) -> str | None:
        if "SHUTDOWN_FLATTEN" not in self._reason_codes:
            self._reason_codes.append("SHUTDOWN_FLATTEN")
        if self.mode == "PAPER" and self._current_intent == "LONG":
            signal_id = sha256(_canonical_json({"last_ts_event_ns": self._evaluator.last_ts_event_ns, "reason": "SHUTDOWN_FLATTEN", "risk_policy_id": self.risk_policy.policy_id})).hexdigest()
            self._order_intents.append((signal_id, "FLAT"))
            self._current_intent = "FLAT"
            return "FLAT"
        return None

    def on_stop(self) -> None:
        before = len(self._order_intents)
        self.shutdown_flatten()
        if (
            self.mode == "PAPER"
            and len(self._order_intents) > before
            and self.trader_id is not None
            and self.instrument_id is not None
            and self.quantity is not None
        ):
            self._submit_intent("FLAT")

    def snapshot(self) -> bytes:
        return _canonical_json({
            "current_intent": self._current_intent,
            "evaluator": self._evaluator.snapshot().decode(),
            "expected_interval_ns": self.expected_interval_ns,
            "family_id": self.family_id,
            "family_version": self.family_version,
            "mode": self.mode,
            "order_intents": [list(item) for item in self._order_intents],
            "parameters": self.parameters,
            "reason_codes": self._reason_codes,
            "risk_policy_id": self.risk_policy.policy_id,
            "schema_version": "family-strategy-state-v1",
            "technical_status": self.technical_status,
        })

    @classmethod
    def restore(cls, payload: bytes, risk_policy: RiskExecutionPolicy) -> FamilyStrategy:
        try:
            value = json.loads(payload, object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LiveStrategyError("strategy state must be UTF-8 JSON") from error
        if not isinstance(value, dict) or payload != _canonical_json(value):
            raise LiveStrategyError("strategy state must be canonical JSON")
        expected = {"current_intent", "evaluator", "expected_interval_ns", "family_id", "family_version", "mode", "order_intents", "parameters", "reason_codes", "risk_policy_id", "schema_version", "technical_status"}
        if set(value) != expected or value["schema_version"] != "family-strategy-state-v1":
            raise LiveStrategyError("invalid strategy state fields")
        if value["risk_policy_id"] != risk_policy.policy_id:
            raise LiveStrategyError("strategy state risk policy mismatch")
        strategy = cls(
            family_id=value["family_id"],
            family_version=value["family_version"],
            parameters=value["parameters"],
            risk_policy=risk_policy,
            mode=value["mode"],
            expected_interval_ns=value["expected_interval_ns"],
        )
        try:
            strategy._evaluator = restore_incremental(value["evaluator"].encode())
        except (AttributeError, FamilyKernelError) as error:
            raise LiveStrategyError("invalid incremental strategy state") from error
        if value["current_intent"] not in {"FLAT", "LONG"} or value["technical_status"] not in {"PASS", "ERROR"}:
            raise LiveStrategyError("invalid strategy state status")
        if not isinstance(value["order_intents"], list) or not all(isinstance(item, list) and len(item) == 2 and item[1] in {"FLAT", "LONG"} for item in value["order_intents"]):
            raise LiveStrategyError("invalid strategy order-intent state")
        if not isinstance(value["reason_codes"], list) or not all(isinstance(item, str) and item for item in value["reason_codes"]):
            raise LiveStrategyError("invalid strategy reason-code state")
        strategy._current_intent = value["current_intent"]
        strategy._order_intents = [tuple(item) for item in value["order_intents"]]
        strategy._reason_codes = list(value["reason_codes"])
        strategy.technical_status = value["technical_status"]
        return strategy
