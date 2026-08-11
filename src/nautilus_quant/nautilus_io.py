from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Iterable

from nautilus_trader.model import (
    Bar,
    BarType,
    Currency,
    FundingRateUpdate,
    IndexInstrument,
    InstrumentId,
    Price,
    Quantity,
    Symbol,
)


_INTERVAL_TOKEN = {
    "5m": "5-MINUTE",
    "15m": "15-MINUTE",
    "30m": "30-MINUTE",
    "1h": "1-HOUR",
    "4h": "4-HOUR",
    "1d": "1-DAY",
    "1w": "1-WEEK",
}


def bar_type_string(instrument_id: str, interval: str, price_type: str) -> str:
    return f"{instrument_id}-{_INTERVAL_TOKEN[interval]}-{price_type}-EXTERNAL"


def _exact_fixed(value: str, precision: int) -> str:
    decimal = Decimal(value)
    fixed = decimal.quantize(Decimal(1).scaleb(-precision))
    if fixed != decimal:
        raise ValueError(f"{value} exceeds configured precision {precision}")
    return f"{fixed:.{precision}f}"


def make_reference_instrument(symbol: str, kind: str) -> IndexInstrument:
    if kind not in {"index", "premium"}:
        raise ValueError(f"unsupported reference kind: {kind}")
    suffix = kind.upper()
    return IndexInstrument(
        instrument_id=InstrumentId.from_str(f"{symbol}-{suffix}.BINANCE"),
        raw_symbol=Symbol(f"{symbol}-{suffix}"),
        currency=Currency.from_str("USDT"),
        price_precision=12,
        size_precision=0,
        price_increment=Price.from_str("0.000000000001"),
        size_increment=Quantity.from_str("1"),
        ts_event=0,
        ts_init=0,
    )


def make_bar(
    *,
    instrument_id: str,
    interval: str,
    price_type: str,
    price_precision: int,
    size_precision: int,
    open_: str,
    high: str,
    low: str,
    close: str,
    volume: str,
    close_ms: int,
) -> Bar:
    bar_type = BarType.from_str(bar_type_string(instrument_id, interval, price_type))
    ts_ns = close_ms * 1_000_000
    return Bar(
        bar_type,
        Price.from_str(_exact_fixed(open_, price_precision)),
        Price.from_str(_exact_fixed(high, price_precision)),
        Price.from_str(_exact_fixed(low, price_precision)),
        Price.from_str(_exact_fixed(close, price_precision)),
        Quantity.from_str(_exact_fixed(volume, size_precision)),
        ts_ns,
        ts_ns,
    )


class FundingJsonStore:
    """Native FundingRateUpdate JSONL store.

    ponytail: NautilusTrader 2.0.0rc2 exposes no public funding Parquet writer.
    Replace this with ParquetDataCatalog once that API lands upstream.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> list[FundingRateUpdate]:
        if not self.path.exists():
            return []
        events: list[FundingRateUpdate] = []
        for line in self.path.read_bytes().splitlines():
            if line:
                events.append(FundingRateUpdate.from_json(line))
        return events

    def last_timestamp(self) -> int | None:
        events = self.load()
        return events[-1].ts_event if events else None

    def append(self, events: Iterable[FundingRateUpdate]) -> int:
        existing = self.load()
        by_timestamp = {event.ts_event: event for event in existing}
        added = 0
        changed = False
        for event in sorted(events, key=lambda item: item.ts_event):
            current = by_timestamp.get(event.ts_event)
            if current is not None:
                if current == event:
                    continue
                enriches_tail = (
                    current.instrument_id == event.instrument_id
                    and current.rate == event.rate
                    and current.interval is None
                    and event.interval is not None
                )
                if not enriches_tail:
                    raise ValueError(f"conflicting funding event at {event.ts_event}")
                by_timestamp[event.ts_event] = event
                changed = True
                continue
            by_timestamp[event.ts_event] = event
            added += 1
        if not added and not changed:
            return 0
        ordered = [by_timestamp[key] for key in sorted(by_timestamp)]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=self.path.parent, prefix=f".{self.path.name}.", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            try:
                for event in ordered:
                    tmp.write(event.to_json())
                    tmp.write(b"\n")
                tmp.flush()
                os.fsync(tmp.fileno())
            except BaseException:
                tmp_path.unlink(missing_ok=True)
                raise
        os.replace(tmp_path, self.path)
        directory_fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return added
