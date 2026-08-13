from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import fcntl
import json
import os
from pathlib import Path
from typing import Callable, Iterator

from nautilus_trader.adapters.binance import (
    BinanceDataClientConfig,
    BinanceEnvironment,
    BinanceInstrumentProviderConfig,
    BinanceProductType,
    load_binance_instruments,
)
from nautilus_trader.persistence import ParquetDataCatalog

from .binance_public import BinancePublicClient
from .config import MarketDataConfig
from .funding_observation import (
    MODELED_FUNDING,
    OFFICIAL,
    read_funding_observations,
    sync_funding_generation,
)
from .nautilus_io import bar_type_string, make_index_instrument
from .sync import sync_bar_stream
from .timebound import align_start, interval_millis, target_end, to_millis

Progress = Callable[[dict[str, object]], None]


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def single_writer(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise AlreadyRunning(f"data sync already holds {path}") from exc
            raise
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _instrument_fingerprint(instrument: object) -> tuple[object, ...]:
    return tuple(
        str(getattr(instrument, name, None))
        for name in (
            "id",
            "price_precision",
            "size_precision",
            "price_increment",
            "size_increment",
            "min_quantity",
            "max_quantity",
            "min_notional",
            "max_notional",
        )
    )


async def load_perpetuals(symbols: tuple[str, ...]) -> list[object]:
    load_ids = [f"{symbol}-PERP.BINANCE" for symbol in symbols]
    config = BinanceDataClientConfig(
        product_type=BinanceProductType.USD_M,
        environment=BinanceEnvironment.LIVE,
        instrument_provider=BinanceInstrumentProviderConfig(
            load_all=False,
            load_ids=load_ids,
            query_commission_rates=False,
        ),
    )
    instruments = list(await load_binance_instruments(config))
    received = {str(instrument.id) for instrument in instruments}
    missing = set(load_ids) - received
    if missing:
        raise RuntimeError(f"Binance instrument loader missed: {sorted(missing)}")
    return sorted(instruments, key=lambda item: str(item.id))


def ensure_instruments(
    catalog: ParquetDataCatalog,
    perpetuals: list[object],
    symbols: tuple[str, ...],
    datasets: tuple[str, ...],
) -> int:
    desired = [
        *perpetuals,
        *(make_index_instrument(symbol) for symbol in symbols if "index" in datasets),
    ]
    existing_by_id: dict[str, object] = {}
    for instrument in catalog.instruments():
        existing_by_id[str(instrument.id)] = instrument
    desired_ids = {str(instrument.id) for instrument in desired}
    unexpected = set(existing_by_id) - desired_ids
    if unexpected:
        raise RuntimeError(f"unexpected catalog instruments: {sorted(unexpected)}")
    writes = []
    for instrument in desired:
        existing = existing_by_id.get(str(instrument.id))
        if existing is None or _instrument_fingerprint(existing) != _instrument_fingerprint(instrument):
            writes.append(instrument)
    if writes:
        catalog.write_instruments(writes)
    after = {str(instrument.id): instrument for instrument in catalog.instruments()}
    unexpected = set(after) - desired_ids
    if unexpected:
        raise RuntimeError(f"unexpected catalog instruments: {sorted(unexpected)}")
    mismatched = {
        str(instrument.id)
        for instrument in desired
        if str(instrument.id) not in after
        or _instrument_fingerprint(after[str(instrument.id)]) != _instrument_fingerprint(instrument)
    }
    if mismatched:
        raise RuntimeError(f"catalog instrument readback mismatch: {sorted(mismatched)}")
    return len(writes)


def validate_catalog_scope(catalog: ParquetDataCatalog, config: MarketDataConfig) -> None:
    if "bars" not in catalog.list_data_types():
        return
    expected = set()
    for symbol in config.symbols:
        for dataset in (item for item in config.datasets if item != "funding"):
            instrument_id = f"{symbol}-PERP.BINANCE" if dataset in {"trade", "mark"} else f"{symbol}-INDEX.BINANCE"
            price_type = "MARK" if dataset == "mark" else "LAST"
            for interval in config.intervals:
                expected.add(bar_type_string(instrument_id, interval, price_type))
    unexpected = {str(item) for item in catalog.list_instruments("bars")} - expected
    if unexpected:
        raise RuntimeError(f"unconfigured catalog bars: {sorted(unexpected)}")


def run_sync(
    config: MarketDataConfig,
    *,
    now: datetime | None = None,
    max_chunks: int | None = None,
    progress: Progress | None = None,
) -> dict[str, object]:
    now = now or datetime.now(timezone.utc)
    progress = progress or (lambda _event: None)
    config.catalog_path.mkdir(parents=True, exist_ok=True)
    catalog = ParquetDataCatalog(str(config.catalog_path))
    validate_catalog_scope(catalog, config)
    client = BinancePublicClient(config.base_url)
    perpetuals = asyncio.run(load_perpetuals(config.symbols))
    perpetual_by_symbol = {str(item.id).split("-PERP.", 1)[0]: item for item in perpetuals}
    instrument_writes = ensure_instruments(catalog, perpetuals, config.symbols, config.datasets)
    instrument_count = len(perpetuals) + (len(config.symbols) if "index" in config.datasets else 0)
    progress({"event": "instruments", "writes": instrument_writes, "count": instrument_count})

    results: list[dict[str, object]] = []
    reconstruction_evidence: list[dict[str, object]] = []
    bar_datasets = tuple(dataset for dataset in config.datasets if dataset != "funding")
    ordered_intervals = tuple(sorted(config.intervals, key=interval_millis, reverse=True))
    for symbol in config.symbols:
        perpetual = perpetual_by_symbol[symbol]
        for dataset in bar_datasets:
            if dataset == "trade":
                instrument_id = str(perpetual.id)
                price_type = "LAST"
                price_precision = perpetual.price_precision
                size_precision = perpetual.size_precision
            elif dataset == "mark":
                instrument_id = str(perpetual.id)
                price_type = "MARK"
                price_precision = 12
                size_precision = perpetual.size_precision
            else:
                instrument_id = f"{symbol}-{dataset.upper()}.BINANCE"
                price_type = "LAST"
                price_precision = 12
                size_precision = 0
            for interval in ordered_intervals:
                start_ms = to_millis(align_start(interval, config.start))
                end_ms = to_millis(target_end(interval, now))
                try:
                    result = sync_bar_stream(
                        client=client,
                        catalog=catalog,
                        symbol=symbol,
                        instrument_id=instrument_id,
                        dataset=dataset,
                        interval=interval,
                        price_type=price_type,
                        price_precision=price_precision,
                        size_precision=size_precision,
                        start_ms=start_ms,
                        end_ms=end_ms,
                        chunk_days=config.chunk_days,
                        max_chunks=max_chunks,
                        reconstruction_evidence=reconstruction_evidence,
                    )
                except BaseException as exc:
                    exc.sync_evidence = {
                        "bar_streams": results,
                        "reconstructed_chunks": reconstruction_evidence,
                    }
                    raise
                result.update({"dataset": dataset, "symbol": symbol, "interval": interval})
                results.append(result)
                progress({"event": "bar_stream", **result})

    funding_results: list[dict[str, object]] = []
    funding_generation: str | None = None
    if "funding" in config.datasets:
        end_ms = to_millis(target_end("5m", now))
        start_ms = to_millis(config.start)
        try:
            pointer = sync_funding_generation(
                client=client,
                funding_path=config.funding_path,
                symbols=config.symbols,
                start_ms=start_ms,
                end_ms=end_ms,
            )
            observations_by_symbol = read_funding_observations(config.funding_path, symbols=config.symbols)
        except BaseException as exc:
            setattr(exc, "sync_evidence", {
                "bar_streams": results,
                "funding_streams": funding_results,
                "reconstructed_chunks": reconstruction_evidence,
            })
            raise
        funding_generation = str(pointer["generation"])
        for symbol in config.symbols:
            instrument_id = f"{symbol}-PERP.BINANCE"
            observations = observations_by_symbol[symbol]
            result = {
                "dataset": "funding",
                "symbol": symbol,
                "instrument_id": instrument_id,
                "rows": len(observations),
                "first_ns": observations[0].funding_time_ns,
                "last_ns": observations[-1].funding_time_ns,
                "modeled_rows": sum(item.truth_status == MODELED_FUNDING for item in observations),
                "official_rows": sum(item.truth_status == OFFICIAL for item in observations),
                "generation": funding_generation,
            }
            funding_results.append(result)
            progress({"event": "funding_stream", **result})

    return {
        "status": "PASS" if all(result["complete"] for result in results) else "PARTIAL",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "catalog_path": str(config.catalog_path),
        "funding_path": str(config.funding_path),
        "instrument_writes": instrument_writes,
        "last_used_weight": client.last_used_weight,
        "bar_streams": results,
        "funding_streams": funding_results,
        "funding_generation": funding_generation,
    }


def write_report(report: dict[str, object], directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    path = directory / f"sync-{stamp}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path
