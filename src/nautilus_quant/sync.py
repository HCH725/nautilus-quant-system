from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from nautilus_trader.model import FundingRateUpdate, InstrumentId
from nautilus_trader.persistence import ParquetDataCatalog

from .binance_public import BinancePublicClient
from .nautilus_io import bar_type_string, make_bar
from .timebound import interval_millis

MAX_FUNDING_INTERVAL_MINUTES = 480
FUNDING_MINUTE_NS = 60 * 1_000_000_000


def funding_interval_minutes(gap_ns: int) -> int:
    return (gap_ns + FUNDING_MINUTE_NS // 2) // FUNDING_MINUTE_NS


def _validated_catalog_tail(
    catalog: ParquetDataCatalog,
    bar_type: str,
    start_ms: int,
    step_ms: int,
) -> int | None:
    # ponytail: an O(n) readback prevents a last-timestamp resume from hiding an
    # internal gap. Replace with a catalog-owned integrity manifest only if full
    # history scans become the measured bottleneck.
    bars = catalog.query_bars([bar_type])
    if not bars:
        return None
    expected_ns = (start_ms + step_ms) * 1_000_000
    step_ns = step_ms * 1_000_000
    for bar in bars:
        if bar.ts_event != expected_ns:
            raise ValueError(f"catalog continuity error for {bar_type}: expected {expected_ns}, got {bar.ts_event}")
        expected_ns += step_ns
    return bars[-1].ts_event


def _validate_catalog_readback(
    catalog: ParquetDataCatalog,
    bar_type: str,
    first_close_ms: int,
    last_close_ms: int,
    step_ms: int,
) -> None:
    bars = catalog.query_bars(
        [bar_type],
        start=first_close_ms * 1_000_000,
        end=last_close_ms * 1_000_000,
    )
    expected_ns = first_close_ms * 1_000_000
    step_ns = step_ms * 1_000_000
    for bar in bars:
        if bar.ts_event != expected_ns:
            raise RuntimeError(f"catalog readback continuity error for {bar_type}: expected {expected_ns}, got {bar.ts_event}")
        expected_ns += step_ns
    if expected_ns != (last_close_ms + step_ms) * 1_000_000:
        raise RuntimeError(f"catalog readback continuity error for {bar_type}: incomplete range")


def sync_bar_stream(
    *,
    client: BinancePublicClient,
    catalog: ParquetDataCatalog,
    symbol: str,
    instrument_id: str,
    dataset: str,
    interval: str,
    price_type: str,
    price_precision: int,
    size_precision: int,
    start_ms: int,
    end_ms: int,
    chunk_days: int,
    max_chunks: int | None = None,
) -> dict[str, int | str]:
    bar_type = bar_type_string(instrument_id, interval, price_type)
    step_ms = interval_millis(interval)
    last_ns = _validated_catalog_tail(catalog, bar_type, start_ms, step_ms)
    cursor = max(start_ms, last_ns // 1_000_000 if last_ns is not None else start_ms)
    chunk_steps = max(1, chunk_days * 86_400_000 // step_ms)
    written = 0
    chunks = 0

    while cursor < end_ms and (max_chunks is None or chunks < max_chunks):
        chunk_end = min(end_ms, cursor + chunk_steps * step_ms)
        rows = client.klines(dataset, symbol, interval, cursor, chunk_end, step_ms)
        chunks += 1
        if not rows:
            raise ValueError(f"missing {dataset}/{symbol}/{interval} data at {cursor}")
        if rows[0].open_ms != cursor:
            raise ValueError(f"catalog/API gap for {dataset}/{symbol}/{interval}: {cursor} -> {rows[0].open_ms}")
        if rows[-1].close_ms != chunk_end:
            raise ValueError(f"incomplete {dataset}/{symbol}/{interval} tail: {rows[-1].close_ms} != {chunk_end}")
        bars = [
            make_bar(
                instrument_id=instrument_id,
                interval=interval,
                price_type=price_type,
                price_precision=price_precision,
                size_precision=size_precision,
                open_=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
                close_ms=row.close_ms,
            )
            for row in rows
        ]
        catalog.write_bars(bars)
        _validate_catalog_readback(
            catalog,
            bar_type,
            first_close_ms=rows[0].close_ms,
            last_close_ms=rows[-1].close_ms,
            step_ms=step_ms,
        )
        written += len(bars)
        cursor = rows[-1].close_ms

    return {
        "bar_type": bar_type,
        "written": written,
        "chunks": chunks,
        "cursor_ms": cursor,
        "complete": cursor >= end_ms,
    }


def funding_events(instrument_id: str, rows: Iterable[dict[str, object]]) -> list[FundingRateUpdate]:
    ordered = sorted(rows, key=lambda row: int(row["fundingTime"]))
    timestamps = [int(row["fundingTime"]) for row in ordered]
    if len(timestamps) != len(set(timestamps)):
        raise ValueError("duplicate funding timestamp")
    events: list[FundingRateUpdate] = []
    for index, row in enumerate(ordered):
        if index + 1 < len(ordered):
            gap_ns = (timestamps[index + 1] - timestamps[index]) * 1_000_000
            interval: int | None = funding_interval_minutes(gap_ns)
            if interval <= 0 or interval > MAX_FUNDING_INTERVAL_MINUTES:
                raise ValueError("invalid funding interval")
            next_funding_ns: int | None = timestamps[index + 1] * 1_000_000
        else:
            interval = None
            next_funding_ns = None
        ts_ns = timestamps[index] * 1_000_000
        events.append(
            FundingRateUpdate(
                InstrumentId.from_str(instrument_id),
                Decimal(str(row["fundingRate"])),
                ts_ns,
                ts_ns,
                interval=interval,
                next_funding_ns=next_funding_ns,
            ),
        )
    return events
