from argparse import ArgumentParser
from datetime import datetime, timezone
from pathlib import Path
import json

from nautilus_trader.persistence import ParquetDataCatalog

from nautilus_quant.config import load_config
from nautilus_quant.nautilus_io import FundingJsonStore, bar_type_string
from nautilus_quant.sync import (
    FUNDING_MINUTE_NS,
    MAX_FUNDING_INTERVAL_MINUTES,
    funding_interval_minutes,
)
from nautilus_quant.timebound import align_start, interval_millis, target_end, to_millis

ROOT = Path(__file__).resolve().parents[1]


def verify_config(config, now: datetime) -> dict[str, object]:
    catalog = ParquetDataCatalog(str(config.catalog_path))
    streams = {}
    expected_bar_types = set()
    for symbol in config.symbols:
        instrument_ids = {
            "trade": f"{symbol}-PERP.BINANCE",
            "mark": f"{symbol}-PERP.BINANCE",
            "index": f"{symbol}-INDEX.BINANCE",
        }
        price_types = {"trade": "LAST", "mark": "MARK", "index": "LAST"}
        for interval in config.intervals:
            start_ms = to_millis(align_start(interval, config.start))
            end_ms = to_millis(target_end(interval, now))
            step_ms = interval_millis(interval)
            expected_count = (end_ms - start_ms) // step_ms
            if expected_count <= 0:
                raise ValueError(f"smoke range has no complete {interval} bars")
            for dataset in (item for item in config.datasets if item != "funding"):
                bar_type = bar_type_string(instrument_ids[dataset], interval, price_types[dataset])
                expected_bar_types.add(bar_type)
                bars = catalog.query_bars([bar_type])
                times = [bar.ts_event for bar in bars]
                assert len(bars) == expected_count, (symbol, dataset, interval, len(bars), expected_count)
                assert len(times) == len(set(times))
                assert times[0] == (start_ms + step_ms) * 1_000_000
                assert times[-1] == end_ms * 1_000_000
                assert all(b - a == step_ms * 1_000_000 for a, b in zip(times, times[1:], strict=False))
                streams[f"{symbol}/{dataset}/{interval}"] = {
                    "count": len(bars),
                    "first_ns": times[0],
                    "last_ns": times[-1],
                }

    expected_instrument_ids = {
        *(f"{symbol}-PERP.BINANCE" for symbol in config.symbols),
        *(f"{symbol}-INDEX.BINANCE" for symbol in config.symbols if "index" in config.datasets),
    }
    actual_instrument_ids = {str(instrument.id) for instrument in catalog.instruments()}
    assert actual_instrument_ids == expected_instrument_ids, (actual_instrument_ids, expected_instrument_ids)
    actual_bar_types = {str(item) for item in catalog.list_instruments("bars")}
    assert actual_bar_types == expected_bar_types, (actual_bar_types, expected_bar_types)

    funding_streams = {}
    if "funding" in config.datasets:
        funding_start_ms = to_millis(config.start)
        funding_end_ms = to_millis(target_end("1d", now))
        for symbol in config.symbols:
            expected_id = f"{symbol}-PERP.BINANCE"
            funding = FundingJsonStore(config.funding_path / f"{expected_id}.jsonl").load()
            assert funding
            assert all(type(item).__name__ == "FundingRateUpdate" for item in funding)
            assert len({item.ts_event for item in funding}) == len(funding)
            head_gap_ns = funding[0].ts_event - funding_start_ms * 1_000_000
            assert 0 <= head_gap_ns < FUNDING_MINUTE_NS // 2
            for current, following in zip(funding, funding[1:], strict=False):
                assert str(current.instrument_id) == expected_id
                assert str(following.instrument_id) == expected_id
                interval_minutes = funding_interval_minutes(following.ts_event - current.ts_event)
                assert 0 < interval_minutes <= MAX_FUNDING_INTERVAL_MINUTES
                assert current.interval == interval_minutes
                assert current.next_funding_ns == following.ts_event
            assert str(funding[-1].instrument_id) == expected_id
            tail_gap_ns = funding_end_ms * 1_000_000 - funding[-1].ts_event
            tail_gap_minutes = funding_interval_minutes(tail_gap_ns)
            assert tail_gap_ns > 0
            assert 0 < tail_gap_minutes <= MAX_FUNDING_INTERVAL_MINUTES
            funding_streams[symbol] = {
                "count": len(funding),
                "first_ns": funding[0].ts_event,
                "last_ns": funding[-1].ts_event,
            }

    return {
        "status": "PASS",
        "instrument_count": len(catalog.instruments()),
        "bar_stream_count": len(streams),
        "funding_stream_count": len(funding_streams),
        "total_stream_count": len(streams) + len(funding_streams),
        "bar_count": sum(stream["count"] for stream in streams.values()),
        "funding_count": sum(stream["count"] for stream in funding_streams.values()),
        "streams": streams,
        "funding_streams": funding_streams,
    }


def main() -> None:
    parser = ArgumentParser()
    parser.add_argument("--config", type=Path, default=ROOT / ".local/config/smoke.json")
    parser.add_argument("--now", help="optional aware ISO-8601 time")
    args = parser.parse_args()
    config = load_config(args.config, ROOT)
    now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else datetime.now(timezone.utc)
    print(json.dumps(verify_config(config, now), sort_keys=True))


if __name__ == "__main__":
    main()
