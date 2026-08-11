from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from nautilus_trader.model import FundingRateUpdate, InstrumentId
from nautilus_trader.persistence import ParquetDataCatalog

from nautilus_quant.nautilus_io import FundingJsonStore, make_bar, make_index_instrument


class NautilusIoTests(unittest.TestCase):
    def test_bar_catalog_roundtrip(self):
        with TemporaryDirectory() as tmp:
            catalog = ParquetDataCatalog(tmp)
            bar = make_bar(
                instrument_id="BTCUSDT-PERP.BINANCE",
                interval="5m",
                price_type="LAST",
                price_precision=2,
                size_precision=3,
                open_="100.00",
                high="101.00",
                low="99.00",
                close="100.50",
                volume="1.000",
                close_ms=300_000,
            )
            catalog.write_bars([bar])
            got = catalog.query_bars([str(bar.bar_type)])
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0], bar)

    def test_reference_instrument_catalog_roundtrip(self):
        with TemporaryDirectory() as tmp:
            catalog = ParquetDataCatalog(tmp)
            instrument = make_index_instrument("BTCUSDT")
            catalog.write_instruments([instrument])
            got = catalog.instruments([str(instrument.id)])
            self.assertEqual(got, [instrument])
            self.assertEqual(str(instrument.id), "BTCUSDT-INDEX.BINANCE")

    def test_funding_store_enriches_unknown_tail_only(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "BTCUSDT-PERP.BINANCE.jsonl"
            store = FundingJsonStore(path)
            unknown = FundingRateUpdate(
                InstrumentId.from_str("BTCUSDT-PERP.BINANCE"),
                Decimal("0.0001"),
                1_000_000_000,
                1_000_000_000,
            )
            enriched = FundingRateUpdate(
                InstrumentId.from_str("BTCUSDT-PERP.BINANCE"),
                Decimal("0.0001"),
                1_000_000_000,
                1_000_000_000,
                interval=480,
                next_funding_ns=1_000_000_000 + 480 * 60 * 1_000_000_000,
            )
            self.assertEqual(store.append([unknown]), 1)
            self.assertEqual(store.append([enriched]), 0)
            self.assertEqual(store.load(), [enriched])

    def test_bar_rejects_precision_loss(self):
        with self.assertRaisesRegex(ValueError, "precision"):
            make_bar(
                instrument_id="BTCUSDT-PERP.BINANCE",
                interval="5m",
                price_type="LAST",
                price_precision=2,
                size_precision=3,
                open_="100.001",
                high="101.00",
                low="99.00",
                close="100.50",
                volume="1.000",
                close_ms=300_000,
            )

    def test_funding_store_is_idempotent_and_loads_native_events(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "BTCUSDT-PERP.BINANCE.jsonl"
            store = FundingJsonStore(path)
            event = FundingRateUpdate(
                InstrumentId.from_str("BTCUSDT-PERP.BINANCE"),
                Decimal("0.0001"),
                1_000_000_000,
                1_000_000_000,
                interval=480,
                next_funding_ns=1_000_000_000 + 480 * 60 * 1_000_000_000,
            )
            self.assertEqual(store.append([event]), 1)
            self.assertEqual(store.append([event]), 0)
            loaded = store.load()
            self.assertEqual(loaded, [event])
            self.assertEqual(store.last_timestamp(), event.ts_event)


if __name__ == "__main__":
    unittest.main()
