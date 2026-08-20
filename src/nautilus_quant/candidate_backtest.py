# noqa: E501  # noqa: SIZE_OK — Task C is explicitly scoped to one evaluator module.
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import platform
from typing import Final, Literal

import nautilus_trader
from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.common import LogLevel
from nautilus_trader.config import BacktestEngineConfig, LoggerConfig
from nautilus_trader.core import UUID4
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
_USDT: Final = Currency.from_str("USDT")
_VENUE: Final = Venue("BINANCE")


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
    signal_parity: SignalParityResult | None = None


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
        or root["signal_timing"]
        != "bar-close; effective no earlier than next event"
        or root["slippage_status"] != "unmodeled"
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
    if sha256(parity.canonical_bytes).hexdigest() != parity.artifact_sha256:
        raise CandidateBacktestError("signal parity artifact hash mismatch")
    if not isinstance(parity.decisions, tuple) or not all(
        isinstance(item, FamilyDecision) for item in parity.decisions
    ):
        raise CandidateBacktestError("signal parity decisions are invalid")
    try:
        document = json.loads(parity.canonical_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CandidateBacktestError("signal parity artifact must be UTF-8 JSON") from error
    decision_payload = b"".join(
        canonical_decision_bytes(item) for item in parity.decisions
    )
    expected: dict[str, JsonValue] = {
        "candidate_id": candidate_id,
        "candidate_signal_count": len(parity.decisions),
        "detail": None,
        "mismatch_index": None,
        "outcome": "PASS",
        "reason_code": "SIGNAL_PARITY_MATCH",
        "recomputed_signal_count": len(parity.decisions),
        "recomputed_signals_sha256": sha256(decision_payload).hexdigest(),
        "required_action": None,
        "schema_version": "signal-parity-result-v1",
    }
    if (
        parity.reason_code != "SIGNAL_PARITY_MATCH"
        or parity.mismatch_index is not None
        or document != expected
        or parity.canonical_bytes != _canonical(expected)
    ):
        raise CandidateBacktestError("signal parity artifact content mismatch")
    return parity.decisions


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
        catalog = ParquetDataCatalog(str(catalog_path))
        source_bars = _source_bars(candidate, catalog, Path(catalog_path))
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
        evaluator = IncrementalFamilyEvaluator(
            family_id=family_id,
            family_version=family_version,
            parameters=parameters,
        )
        recomputed: list[FamilyDecision] = []
        for bar in source_bars:
            decision = evaluator.push(
                ClosedBar(
                    ts_event_ns=bar.ts_event,
                    open=float(str(bar.open)),
                    high=float(str(bar.high)),
                    low=float(str(bar.low)),
                    close=float(str(bar.close)),
                    volume=float(str(bar.volume)),
                )
            )
            if decision is not None:
                recomputed.append(decision)
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
            if is_long:
                source_ns = (
                    changed.ts_event_ns
                    if changed is not None and changed.intent == "FLAT"
                    else None
                )
                self.boundary_flattened = source_ns is None
                self._submit(_Action("FLAT", source_ns, bar.ts_event))
            return
        if changed is None:
            return
        if changed.intent == "LONG" and not is_long:
            self._submit(_Action("LONG", changed.ts_event_ns, bar.ts_event))
        elif changed.intent == "FLAT" and is_long:
            self._submit(_Action("FLAT", changed.ts_event_ns, bar.ts_event))


def _funding_data(
    observations: list[FundingObservation],
    bars: list[Bar],
    instrument_id: InstrumentId,
) -> tuple[list[MarkPriceUpdate | FundingRateUpdate], dict[int, tuple[FundingObservation, Price, str]]]:
    events: list[MarkPriceUpdate | FundingRateUpdate] = []
    evidence: dict[int, tuple[FundingObservation, Price, str]] = {}
    for observation in observations:
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
                observation.rate,
                settlement_timestamp,
                settlement_timestamp,
                interval=480,
                next_funding_ns=settlement_timestamp,
            ),
        )
        evidence[settlement_timestamp] = (observation, mark_price, price_source)
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
) -> CryptoPerpetual:
    increment = instrument.price_increment.as_decimal()
    prices = (
        price.as_decimal()
        for bar in bars
        for price in (bar.open, bar.high, bar.low, bar.close)
    )
    if all(price % increment == 0 for price in prices):
        return instrument
    historical_increment = Decimal(1).scaleb(-instrument.price_precision)
    # ponytail: use the stored bar precision until Catalog carries versioned tick sizes.
    definition = instrument.to_dict()
    definition["price_increment"] = str(historical_increment)
    return CryptoPerpetual.from_dict(definition)


def run_candidate_backtest(request: CandidateBacktestRequest) -> CandidateBacktestResult:
    """Evaluate one validated PyBroker candidate with the real Nautilus engine."""
    candidate, candidate_id = load_pybroker_candidate(request.candidate_path)
    policy = _load_policy(request.policy_path)
    catalog_path = Path(request.catalog_path)
    catalog = ParquetDataCatalog(str(catalog_path))
    source_bars = _source_bars(candidate, catalog, catalog_path)
    if candidate.get("schema_version") == "pybroker-candidate-v2":
        parity = request.signal_parity
        if parity is None:
            raise CandidateBacktestError("Candidate v2 requires a passed signal parity gate")
        replay_signals = [
            _Signal(item.target_intent, item.ts_event_ns)
            for item in _verified_parity_decisions(parity, candidate_id)
        ]
    else:
        replay_signals = _signals(candidate)
    bars = [bar for bar in source_bars if bar.ts_event >= policy.historical_start_ns]
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
    instrument = _historical_instrument(instrument, bars)

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
        if bars[0].ts_event <= item.funding_time_ns <= bars[-1].ts_event
    ]
    funding_data, funding_evidence = _funding_data(
        evaluated_observations,
        bars,
        instrument.id,
    )
    strategy = _CandidateReplayStrategy()
    strategy.configure(
        _ReplayPlan(
            instrument.id,
            BarType.from_str(bar_type_text),
            policy.quantity,
            replay_signals,
            bars[-1].ts_event,
        ),
    )
    engine = BacktestEngine(
        BacktestEngineConfig(
            run_analysis=False,
            logging=LoggerConfig(stdout_level=LogLevel.OFF),
        ),
    )
    try:
        engine.add_venue(
            venue=_VENUE,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            starting_balances=[Money(policy.starting_balance, _USDT)],
            base_currency=_USDT,
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
        reason_codes = [
            "POSITIVE_NET_RESEARCH_ONLY" if account_delta > 0 else "NON_POSITIVE_NET_RESULT",
            "UNMODELED_SLIPPAGE",
        ]
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
                "slippage_status": policy.slippage_status,
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
                truth_status == "official" and policy.slippage_status != "unmodeled"
            ),
            "policy_decision_version": policy.decision_version,
            "realized_balance_drawdown": _money(realized_drawdown),
            "reason_codes": reason_codes,
            "runtime_versions": {
                "nautilus_trader": nautilus_trader.__version__,
                "pybroker": str(runtime["pybroker_version"]),
                "python": platform.python_version(),
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
        verdict["canonical_result_hash"] = sha256(_canonical(verdict)).hexdigest()
        payload = _canonical(verdict)
        return CandidateBacktestResult(verdict, payload, sha256(payload).hexdigest())
    finally:
        engine.dispose()
