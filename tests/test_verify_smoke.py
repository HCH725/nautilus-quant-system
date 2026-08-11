from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from nautilus_trader.persistence import ParquetDataCatalog

from nautilus_quant.config import MarketDataConfig
from nautilus_quant.nautilus_io import FundingJsonStore, make_bar
from nautilus_quant.sync import funding_events
from scripts.verify_smoke import verify_config


class VerifySmokeTests(unittest.TestCase):
    def test_verifies_multiple_symbols_and_intervals(self):
        start = datetime(2026, 7, 27, tzinfo=timezone.utc)
        now = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
        symbols = ("BTCUSDT", "ETHUSDT")
        intervals = {"1d": 86_400_000, "1w": 604_800_000}
        datasets = ("trade", "mark", "index", "premium", "funding")
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = MarketDataConfig(
                base_url="https://fapi.binance.com",
                start=start,
                symbols=symbols,
                intervals=tuple(intervals),
                datasets=datasets,
                chunk_days=30,
                catalog_path=root / "catalog",
                funding_path=root / "funding",
            )
            config.catalog_path.mkdir()
            catalog = ParquetDataCatalog(str(config.catalog_path))
            end_ms = int(datetime(2026, 8, 10, tzinfo=timezone.utc).timestamp() * 1000)
            start_ms = int(start.timestamp() * 1000)
            bars = []
            for symbol in symbols:
                for dataset in datasets[:-1]:
                    instrument_id = (
                        f"{symbol}-PERP.BINANCE"
                        if dataset in {"trade", "mark"}
                        else f"{symbol}-{dataset.upper()}.BINANCE"
                    )
                    price_type = "MARK" if dataset == "mark" else "LAST"
                    for interval, step_ms in intervals.items():
                        bars.extend(
                            make_bar(
                                instrument_id=instrument_id,
                                interval=interval,
                                price_type=price_type,
                                price_precision=2,
                                size_precision=0,
                                open_="1",
                                high="1",
                                low="1",
                                close="1",
                                volume="0",
                                close_ms=close_ms,
                            )
                            for close_ms in range(start_ms + step_ms, end_ms + 1, step_ms)
                        )
                funding_rows = [
                    {"fundingTime": timestamp, "fundingRate": "0.0001"}
                    for timestamp in range(start_ms, end_ms, 8 * 60 * 60_000)
                ]
                FundingJsonStore(config.funding_path / f"{symbol}-PERP.BINANCE.jsonl").append(
                    funding_events(f"{symbol}-PERP.BINANCE", funding_rows),
                )
            bars_by_type = {}
            for bar in bars:
                bars_by_type.setdefault(str(bar.bar_type), []).append(bar)
            for stream in bars_by_type.values():
                catalog.write_bars(stream)

            result = verify_config(config, now)

            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["bar_stream_count"], 16)
            self.assertEqual(result["funding_stream_count"], 2)
            self.assertEqual(result["total_stream_count"], 18)


if __name__ == "__main__":
    unittest.main()
