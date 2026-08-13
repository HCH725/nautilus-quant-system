from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
import argparse
import hashlib
import json

import nautilus_trader
from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.common import LogLevel
from nautilus_trader.config import BacktestEngineConfig, LoggerConfig
from nautilus_trader.core import UUID4
from nautilus_trader.model import (
    AccountType,
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
    QuoteTick,
    Symbol,
    TimeInForce,
    Venue,
)
from nautilus_trader.trading import Strategy

from .funding_observation import FUNDING_PRICE_SOURCE


HOUR_NS = 60 * 60 * 1_000_000_000
USDT = Currency.from_str("USDT")
BTC = Currency.from_str("BTC")
VENUE = Venue("BINANCE")


def _object(value: object, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"invalid {name} fields")
    return value


def _decimal(value: object, name: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a decimal string") from exc
    if not result.is_finite() or (positive and result <= 0):
        raise ValueError(f"{name} must be {'positive and ' if positive else ''}finite")
    return result


def _time_ns(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value * HOUR_NS


def _validate_scenario(scenario: dict[str, Any], scenario_name: str) -> None:
    quote_times: set[int] = set()
    for value in scenario["quotes"]:
        row = _object(value, {"time_hours", "bid", "ask", "action"}, "quote")
        timestamp = _time_ns(row["time_hours"], "quote time_hours")
        if timestamp in quote_times:
            raise ValueError(f"duplicate quote timestamp: {timestamp}")
        quote_times.add(timestamp)
        bid = _decimal(row["bid"], "bid", positive=True)
        ask = _decimal(row["ask"], "ask", positive=True)
        if bid > ask:
            raise ValueError("quote bid must not exceed ask")
        if row["action"] not in {"BUY", "SELL"}:
            raise ValueError("quote action must be BUY or SELL")

    funding_times: set[int] = set()
    for value in scenario["funding"]:
        row = _object(
            value,
            {"time_hours", "rate", "mark_price", "expected_direction"},
            "funding",
        )
        timestamp = _time_ns(row["time_hours"], "funding time_hours")
        if timestamp in funding_times:
            raise ValueError(f"duplicate funding timestamp: {timestamp}")
        funding_times.add(timestamp)
        _decimal(row["rate"], "rate")
        if row["expected_direction"] not in {"LONG", "SHORT", "FLAT"}:
            raise ValueError("expected_direction must be LONG, SHORT or FLAT")
        if row["mark_price"] is None:
            if scenario_name == "official":
                raise ValueError("official scenario requires mark_price")
        else:
            _decimal(row["mark_price"], "mark_price", positive=True)
            if scenario_name == "modeled_missing_mark":
                raise ValueError("modeled scenario requires missing mark_price")


def _money(value: Decimal) -> str:
    return f"{value:.8f}"


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


class _FixtureStrategy(Strategy):
    def __init__(self) -> None:
        super().__init__()
        self._instrument_id: InstrumentId | None = None
        self._quantity: Quantity | None = None
        self._actions: dict[int, OrderSide] = {}

    def configure(
        self,
        instrument_id: InstrumentId,
        quantity: Quantity,
        actions: dict[int, OrderSide],
    ) -> None:
        self._instrument_id = instrument_id
        self._quantity = quantity
        self._actions = actions

    def on_start(self) -> None:
        if self._instrument_id is None:
            raise RuntimeError("fixture strategy is not configured")
        self.subscribe_quotes(self._instrument_id)

    def on_quote(self, quote: QuoteTick) -> None:
        side = self._actions.get(quote.ts_event)
        if side is None:
            return
        if self.trader_id is None or self._instrument_id is None or self._quantity is None:
            raise RuntimeError("fixture strategy is not configured")
        self.submit_order(
            MarketOrder(
                trader_id=self.trader_id,
                strategy_id=self.strategy_id,
                instrument_id=self._instrument_id,
                client_order_id=self.order_factory.generate_client_order_id(),
                order_side=side,
                quantity=self._quantity,
                init_id=UUID4(),
                ts_init=self.clock.timestamp_ns(),
                time_in_force=TimeInForce.GTC,
                reduce_only=False,
                quote_quantity=False,
                contingency_type=ContingencyType.NO_CONTINGENCY,
            ),
        )

def _load_fixture(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(
            path.read_bytes(),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid backtest fixture: {path}") from exc
    root = _object(
        raw,
        {
            "schema_version",
            "engine",
            "fixture_only",
            "instrument_id",
            "starting_balance",
            "quantity",
            "taker_fee",
            "official",
            "modeled_missing_mark",
        },
        "backtest fixture",
    )
    if type(root["schema_version"]) is not int or root["schema_version"] != 1 or root["fixture_only"] is not True:
        raise ValueError("backtest fixture must be synthetic schema v1")
    if root["engine"] != f"nautilus_trader=={nautilus_trader.__version__}":
        raise ValueError("backtest fixture engine version mismatch")
    if root["instrument_id"] != "BTCUSDT-PERP.BINANCE":
        raise ValueError("backtest fixture instrument must be BTCUSDT-PERP.BINANCE")
    _decimal(root["starting_balance"], "starting_balance", positive=True)
    _decimal(root["quantity"], "quantity", positive=True)
    _decimal(root["taker_fee"], "taker_fee")
    for scenario_name in ("official", "modeled_missing_mark"):
        scenario = _object(
            root[scenario_name],
            {
                "truth_status",
                "funding_price_source",
                "performance_claimable",
                "same_timestamp_order",
                "quotes",
                "funding",
            },
            f"{scenario_name} scenario",
        )
        if not isinstance(scenario["quotes"], list) or not isinstance(scenario["funding"], list):
            raise ValueError(f"{scenario_name} quotes and funding must be arrays")
        _validate_scenario(scenario, scenario_name)
    official = root["official"]
    if (
        official["truth_status"] != "official"
        or official["funding_price_source"] != FUNDING_PRICE_SOURCE
        or official["performance_claimable"] is not True
        or official["same_timestamp_order"] != "mark_then_funding"
    ):
        raise ValueError("official scenario truth metadata is invalid")
    modeled = root["modeled_missing_mark"]
    if (
        modeled["truth_status"] != "modeled_funding"
        or modeled["funding_price_source"] is not None
        or modeled["performance_claimable"] is not False
        or modeled["same_timestamp_order"] != "top_of_book_fallback"
    ):
        raise ValueError("modeled scenario truth metadata is invalid")
    return root


def _instrument(instrument_id: InstrumentId, taker_fee: Decimal) -> CryptoPerpetual:
    return CryptoPerpetual(
        instrument_id,
        Symbol("BTCUSDT"),
        BTC,
        USDT,
        USDT,
        False,
        2,
        3,
        Price.from_str("0.01"),
        Quantity.from_str("0.001"),
        0,
        0,
        margin_init=Decimal("0.01"),
        margin_maint=Decimal("0.005"),
        maker_fee=taker_fee,
        taker_fee=taker_fee,
    )


def run_funding_oracle(
    config_path: Path,
    *,
    scenario_name: str = "official",
) -> dict[str, Any]:
    """Run the synthetic rc2 accounting fixture and return a canonical report."""
    fixture = _load_fixture(Path(config_path))
    if scenario_name not in {"official", "modeled_missing_mark"}:
        raise ValueError(f"unknown scenario: {scenario_name}")
    scenario = fixture[scenario_name]
    instrument_id = InstrumentId.from_str(fixture["instrument_id"])
    quantity = Quantity.from_str(fixture["quantity"])
    starting_balance = _decimal(fixture["starting_balance"], "starting_balance", positive=True)
    taker_fee = _decimal(fixture["taker_fee"], "taker_fee")

    actions: dict[int, OrderSide] = {}
    quote_midpoints: dict[int, Decimal] = {}
    data: list[Any] = []
    for value in scenario["quotes"]:
        row = _object(value, {"time_hours", "bid", "ask", "action"}, "quote")
        timestamp = _time_ns(row["time_hours"], "quote time_hours")
        if timestamp in actions:
            raise ValueError(f"duplicate quote timestamp: {timestamp}")
        bid = _decimal(row["bid"], "bid", positive=True)
        ask = _decimal(row["ask"], "ask", positive=True)
        if bid > ask:
            raise ValueError("quote bid must not exceed ask")
        if row["action"] not in {"BUY", "SELL"}:
            raise ValueError("quote action must be BUY or SELL")
        actions[timestamp] = OrderSide.BUY if row["action"] == "BUY" else OrderSide.SELL
        quote_midpoints[timestamp] = (bid + ask) / 2
        data.append(
            QuoteTick(
                instrument_id,
                Price.from_str(str(bid)),
                Price.from_str(str(ask)),
                Quantity.from_str("10.000"),
                Quantity.from_str("10.000"),
                timestamp,
                timestamp,
            ),
        )

    funding_rows: dict[int, dict[str, Any]] = {}
    for value in scenario["funding"]:
        row = _object(
            value,
            {"time_hours", "rate", "mark_price", "expected_direction"},
            "funding",
        )
        timestamp = _time_ns(row["time_hours"], "funding time_hours")
        if timestamp in funding_rows:
            raise ValueError(f"duplicate funding timestamp: {timestamp}")
        rate = _decimal(row["rate"], "rate")
        mark_price = (
            _decimal(row["mark_price"], "mark_price", positive=True)
            if row["mark_price"] is not None
            else None
        )
        if row["expected_direction"] not in {"LONG", "SHORT", "FLAT"}:
            raise ValueError("expected_direction must be LONG, SHORT or FLAT")
        funding_rows[timestamp] = row
        if mark_price is not None:
            # The insertion order is part of this executable contract: mark first,
            # then the rate using the same row's settlement boundary.
            data.append(MarkPriceUpdate(instrument_id, Price.from_str(str(mark_price)), timestamp, timestamp))
        data.append(
            FundingRateUpdate(
                instrument_id,
                rate,
                timestamp,
                timestamp,
                interval=480,
                next_funding_ns=timestamp,
            ),
        )

    data.sort(key=lambda item: item.ts_event)
    engine = BacktestEngine(
        BacktestEngineConfig(
            run_analysis=False,
            logging=LoggerConfig(stdout_level=LogLevel.OFF),
        ),
    )
    try:
        engine.add_venue(
            venue=VENUE,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            starting_balances=[Money(starting_balance, USDT)],
            base_currency=USDT,
        )
        engine.add_instrument(_instrument(instrument_id, taker_fee))
        strategy = _FixtureStrategy()
        strategy.configure(instrument_id, quantity, actions)
        engine.add_strategy(strategy)
        engine.add_data(data)
        engine.run()

        account = engine.cache.account_for_venue(VENUE)
        if account is None:
            raise RuntimeError("Nautilus account was not created")
        orders = sorted(engine.cache.orders(), key=lambda item: item.last_event.ts_event)
        net_quantity = Decimal()
        direction_at_boundary: dict[int, str] = {}
        for timestamp in sorted(funding_rows):
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
            direction_at_boundary[timestamp] = (
                "LONG" if net_quantity > 0 else "SHORT" if net_quantity < 0 else "FLAT"
            )

        account_events = [
            {
                "ts_event_ns": event.ts_event,
                "is_reported": event.is_reported,
                "total": _money(next(balance.total.as_decimal() for balance in event.balances if balance.currency == USDT)),
            }
            for event in account.events
        ]
        funding_events = []
        adjusted_boundaries: set[int] = set()
        for timestamp, row in funding_rows.items():
            direction = direction_at_boundary[timestamp]
            if direction != row["expected_direction"]:
                raise RuntimeError(f"unexpected position direction at {timestamp}: {direction}")
            settlements = [
                event
                for event in account_events
                if event["ts_event_ns"] == timestamp and event["is_reported"]
            ]
            if direction == "FLAT":
                if settlements:
                    raise RuntimeError(f"flat funding boundary changed the account at {timestamp}")
                continue
            if len(settlements) != 1:
                raise RuntimeError(f"funding did not settle exactly once at {timestamp}")
            account_index = account_events.index(settlements[0])
            if account_index == 0:
                raise RuntimeError(f"funding settlement has no prior account event at {timestamp}")
            amount = Decimal(settlements[0]["total"]) - Decimal(account_events[account_index - 1]["total"])
            adjusted_boundaries.add(timestamp)
            funding_events.append({
                "ts_event_ns": timestamp,
                "direction": direction,
                "rate": str(_decimal(row["rate"], "rate")),
                "mark_price": (
                    str(_decimal(row["mark_price"], "mark_price", positive=True))
                    if row["mark_price"] is not None
                    else str(quote_midpoints[max(value for value in quote_midpoints if value < timestamp)])
                ),
                "price_source": scenario["funding_price_source"],
                "amount": _money(amount),
            })

        flat_boundaries = sorted(
            timestamp
            for timestamp, row in funding_rows.items()
            if row["expected_direction"] == "FLAT"
        )
        if adjusted_boundaries & set(flat_boundaries):
            raise RuntimeError("flat funding boundary changed the account")
        expected_non_flat = {
            timestamp
            for timestamp, row in funding_rows.items()
            if row["expected_direction"] != "FLAT"
        }
        if adjusted_boundaries != expected_non_flat:
            raise RuntimeError(
                "funding boundaries did not settle exactly once: "
                f"expected={sorted(expected_non_flat)}, actual={sorted(adjusted_boundaries)}",
            )

        fee_events = []
        for order in orders:
            fill = order.last_event
            commission = fill.commission.as_decimal()
            fee_events.append({"ts_event_ns": fill.ts_event, "amount": _money(-commission)})
        ending_balance = account.balance_total(USDT).as_decimal()
        funding_total = sum((Decimal(event["amount"]) for event in funding_events), Decimal())
        fee_total = sum((Decimal(event["amount"]) for event in fee_events), Decimal())
        open_positions = engine.cache.positions_open()
        report: dict[str, Any] = {
            "schema_version": 1,
            "engine": fixture["engine"],
            "instrument_id": fixture["instrument_id"],
            "truth_status": scenario["truth_status"],
            "funding_price_source": scenario["funding_price_source"],
            "performance_claimable": scenario["performance_claimable"],
            "same_timestamp_order": scenario["same_timestamp_order"],
            "starting_balance": _money(starting_balance),
            "ending_balance": _money(ending_balance),
            "account_delta": _money(ending_balance - starting_balance),
            "fees": {"events": fee_events, "total": _money(fee_total)},
            "funding": {"events": sorted(funding_events, key=lambda item: item["ts_event_ns"]), "total": _money(funding_total)},
            "flat_funding_boundaries_ns": flat_boundaries,
            "account_events": account_events,
            "ending_position": "FLAT" if not open_positions else str(open_positions[0].side),
            "open_position_count": len(open_positions),
        }
        if funding_total + fee_total != ending_balance - starting_balance:
            raise RuntimeError(
                "Nautilus account delta does not equal fee plus funding events: "
                f"funding={funding_total}, fees={fee_total}, "
                f"account_delta={ending_balance - starting_balance}",
            )
        report["canonical_result_hash"] = hashlib.sha256(_canonical(report)).hexdigest()
        return report
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the synthetic Nautilus funding accounting oracle")
    parser.add_argument("config", type=Path)
    parser.add_argument(
        "--scenario",
        choices=("official", "modeled_missing_mark"),
        default="official",
    )
    args = parser.parse_args(argv)
    print(
        _canonical(
            run_funding_oracle(args.config, scenario_name=args.scenario),
        ).decode("utf-8"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
