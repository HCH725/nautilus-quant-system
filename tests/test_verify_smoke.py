from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch
import unittest

from nautilus_trader.model import Currency, IndexInstrument, InstrumentId, Price, Quantity, Symbol
from nautilus_trader.persistence import ParquetDataCatalog

from nautilus_quant.config import MarketDataConfig
from nautilus_quant.funding_observation import migrate_funding_observations
from nautilus_quant.nautilus_io import make_bar
from scripts.verify_smoke import verify_config


def test_instrument(instrument_id: str) -> IndexInstrument:
    raw_symbol = instrument_id.split(".", 1)[0]
    return IndexInstrument(
        instrument_id=InstrumentId.from_str(instrument_id),
        raw_symbol=Symbol(raw_symbol),
        currency=Currency.from_str("USDT"),
        price_precision=12,
        size_precision=0,
        price_increment=Price.from_str("0.000000000001"),
        size_increment=Quantity.from_str("1"),
        ts_event=0,
        ts_init=0,
    )


class VerifySmokeTests(unittest.TestCase):
    def test_rejects_each_unconfigured_catalog_inventory_type(self):
        start = datetime(2026, 8, 8, tzinfo=timezone.utc)
        now = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
        step_ns = 86_400_000_000_000
        first_ns = int(start.timestamp() * 1_000_000_000) + step_ns
        expected_bar_types = {
            "BTCUSDT-PERP.BINANCE-1-DAY-LAST-EXTERNAL",
            "BTCUSDT-PERP.BINANCE-1-DAY-MARK-EXTERNAL",
            "BTCUSDT-INDEX.BINANCE-1-DAY-LAST-EXTERNAL",
        }

        config = MarketDataConfig(
            base_url="https://fapi.binance.com",
            start=start,
            symbols=("BTCUSDT",),
            intervals=("1d",),
            datasets=("trade", "mark", "index"),
            chunk_days=30,
            catalog_path=Path("unused"),
            funding_path=Path("unused"),
        )

        for stale_kind in ("instrument", "bar"):
            with self.subTest(stale_kind=stale_kind):
                class Catalog:
                    @staticmethod
                    def query_bars(_bar_types):
                        return [SimpleNamespace(ts_event=first_ns), SimpleNamespace(ts_event=first_ns + step_ns)]

                    @staticmethod
                    def instruments():
                        ids = ["BTCUSDT-PERP.BINANCE", "BTCUSDT-INDEX.BINANCE"]
                        if stale_kind == "instrument":
                            ids.append("BTCUSDT-PREMIUM.BINANCE")
                        return [SimpleNamespace(id=value) for value in ids]

                    @staticmethod
                    def list_instruments(_data_type):
                        values = list(expected_bar_types)
                        if stale_kind == "bar":
                            values.append("BTCUSDT-PREMIUM.BINANCE-1-DAY-LAST-EXTERNAL")
                        return values

                with patch("scripts.verify_smoke.ParquetDataCatalog", return_value=Catalog()):
                    with self.assertRaises(AssertionError):
                        verify_config(config, now)

    def test_verifies_multiple_symbols_and_intervals(self):
        start = datetime(2026, 7, 27, tzinfo=timezone.utc)
        now = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
        symbols = ("BTCUSDT", "ETHUSDT")
        intervals = {"1d": 86_400_000, "1w": 604_800_000}
        datasets = ("trade", "mark", "index", "funding")
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
            catalog.write_instruments([
                test_instrument(instrument_id)
                for symbol in symbols
                for instrument_id in (f"{symbol}-PERP.BINANCE", f"{symbol}-INDEX.BINANCE")
            ])
            end_ms = int(datetime(2026, 8, 10, tzinfo=timezone.utc).timestamp() * 1000)
            start_ms = int(start.timestamp() * 1000)
            bars = []
            funding_by_symbol: dict[str, list[dict[str, object]]] = {}
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
                funding_by_symbol[symbol] = [
                    {
                        "symbol": symbol,
                        "fundingTime": timestamp,
                        "fundingRate": "0.0001",
                        "markPrice": "1000",
                        "rateType": "Regular",
                    }
                    for timestamp in range(start_ms, end_ms, 8 * 60 * 60_000)
                ]
            bars_by_type = {}
            for bar in bars:
                bars_by_type.setdefault(str(bar.bar_type), []).append(bar)
            for stream in bars_by_type.values():
                catalog.write_bars(stream)

            class FundingClient:
                def funding(
                    self,
                    symbol: str,
                    requested_start_ms: int,
                    requested_end_ms: int,
                ) -> list[dict[str, object]]:
                    return [
                        row
                        for row in funding_by_symbol[symbol]
                        if requested_start_ms <= cast(int, row["fundingTime"]) < requested_end_ms
                    ]

            migrate_funding_observations(
                client=FundingClient(),
                funding_path=config.funding_path,
                symbols=symbols,
                start_ms=start_ms,
                end_ms=end_ms,
            )

            result = verify_config(config, now)

            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["bar_stream_count"], 12)
            self.assertEqual(result["funding_stream_count"], 2)
            self.assertEqual(result["total_stream_count"], 14)


if __name__ == "__main__":
    unittest.main()
