from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from nautilus_trader.model import FundingRateUpdate, InstrumentId
from nautilus_trader.persistence import ParquetDataCatalog

from nautilus_quant.binance_public import Kline
from nautilus_quant.nautilus_io import FundingJsonStore, make_bar, make_index_instrument
from nautilus_quant.service import ensure_instruments, sync_funding_stream, validate_catalog_scope
from nautilus_quant.sync import funding_events, legacy_funding_events, sync_bar_stream


class FakeClient:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def klines(self, dataset, symbol, interval, start_ms, end_ms, interval_ms):
        self.calls.append((dataset, symbol, interval, start_ms, end_ms))
        return [row for row in self.rows if start_ms <= row.open_ms and row.close_ms <= end_ms]


class SyncTests(unittest.TestCase):
    def test_catalog_scope_rejects_unconfigured_bar_type(self):
        config = SimpleNamespace(
            symbols=("BTCUSDT",),
            intervals=("5m",),
            datasets=("trade", "mark", "index"),
        )

        class Catalog:
            @staticmethod
            def list_data_types():
                return ["bars"]

            @staticmethod
            def list_instruments(_data_type):
                return ["BTCUSDT-PREMIUM.BINANCE-5-MINUTE-LAST-EXTERNAL"]

        with self.assertRaisesRegex(RuntimeError, "unconfigured catalog bars"):
            validate_catalog_scope(Catalog(), config)

    def test_instruments_only_include_configured_reference_datasets(self):
        fields = {
            "id": "BTCUSDT-PERP.BINANCE",
            "price_precision": 2,
            "size_precision": 3,
            "price_increment": "0.01",
            "size_increment": "0.001",
            "min_quantity": "0.001",
            "max_quantity": "1000",
            "min_notional": "5",
            "max_notional": None,
        }

        class RecordingCatalog:
            def __init__(self):
                self.items = []

            def instruments(self):
                return self.items

            def write_instruments(self, instruments):
                self.items.extend(instruments)

        catalog = RecordingCatalog()
        writes = ensure_instruments(
            catalog,
            [SimpleNamespace(**fields)],
            ("BTCUSDT",),
            ("trade", "index"),
        )

        self.assertEqual(writes, 2)
        self.assertEqual(
            {str(instrument.id) for instrument in catalog.items},
            {"BTCUSDT-PERP.BINANCE", "BTCUSDT-INDEX.BINANCE"},
        )

    def test_instruments_reject_unconfigured_catalog_entries_before_writing(self):
        fields = {
            "price_precision": 2,
            "size_precision": 3,
            "price_increment": "0.01",
            "size_increment": "0.001",
            "min_quantity": "0.001",
            "max_quantity": "1000",
            "min_notional": "5",
            "max_notional": None,
        }
        desired = SimpleNamespace(id="BTCUSDT-PERP.BINANCE", **fields)
        retired = SimpleNamespace(id="BTCUSDT-PREMIUM.BINANCE", **fields)

        class Catalog:
            writes = []

            @staticmethod
            def instruments():
                return [retired]

            @classmethod
            def write_instruments(cls, instruments):
                cls.writes.extend(instruments)

        with self.assertRaisesRegex(RuntimeError, "unexpected catalog instruments"):
            ensure_instruments(Catalog(), [desired], ("BTCUSDT",), ("trade", "index"))
        self.assertEqual(Catalog.writes, [])

    def test_instrument_readback_rejects_stale_same_id_metadata(self):
        fields = {
            "id": "BTCUSDT-PERP.BINANCE",
            "price_precision": 8,
            "size_precision": 3,
            "price_increment": "0.00000001",
            "size_increment": "0.001",
            "min_quantity": "0.001",
            "max_quantity": "1000",
            "min_notional": "5",
            "max_notional": None,
        }
        desired = SimpleNamespace(**fields)
        stale = SimpleNamespace(**{**fields, "price_precision": 2})

        class DroppingCatalog:
            def __init__(self):
                self.items = [
                    stale,
                    make_index_instrument("BTCUSDT"),
                ]

            def instruments(self):
                return self.items

            @staticmethod
            def write_instruments(_instruments):
                return None

        with self.assertRaisesRegex(RuntimeError, "instrument readback"):
            ensure_instruments(DroppingCatalog(), [desired], ("BTCUSDT",), ("trade", "index"))

    def test_bar_stream_resumes_from_exact_catalog_bar_type(self):
        rows = [
            Kline(0, 300_000, "100", "101", "99", "100.5", "1"),
            Kline(300_000, 600_000, "100.5", "102", "100", "101", "2"),
        ]
        client = FakeClient(rows)
        with TemporaryDirectory() as tmp:
            catalog = ParquetDataCatalog(tmp)
            first = sync_bar_stream(
                client=client,
                catalog=catalog,
                symbol="BTCUSDT",
                instrument_id="BTCUSDT-PERP.BINANCE",
                dataset="trade",
                interval="5m",
                price_type="LAST",
                price_precision=2,
                size_precision=3,
                start_ms=0,
                end_ms=600_000,
                chunk_days=30,
            )
            second = sync_bar_stream(
                client=client,
                catalog=catalog,
                symbol="BTCUSDT",
                instrument_id="BTCUSDT-PERP.BINANCE",
                dataset="trade",
                interval="5m",
                price_type="LAST",
                price_precision=2,
                size_precision=3,
                start_ms=0,
                end_ms=600_000,
                chunk_days=30,
            )
            self.assertEqual(first["written"], 2)
            self.assertEqual(second["written"], 0)
            self.assertEqual(len(client.calls), 1)

    def test_bar_stream_reports_one_minute_reconstruction_count(self):
        rows = [
            Kline(0, 300_000, "100", "101", "99", "100.5", "1"),
            Kline(300_000, 600_000, "100.5", "102", "100", "101", "2", source_interval="1m"),
        ]
        with TemporaryDirectory() as tmp:
            result = sync_bar_stream(
                client=FakeClient(rows),
                catalog=ParquetDataCatalog(tmp),
                symbol="BTCUSDT",
                instrument_id="BTCUSDT-PERP.BINANCE",
                dataset="trade",
                interval="5m",
                price_type="LAST",
                price_precision=2,
                size_precision=3,
                start_ms=0,
                end_ms=600_000,
                chunk_days=30,
            )

        self.assertEqual(result["reconstructed"], 1)
        self.assertEqual(result["reconstructed_open_ms"], [300_000])

    def test_existing_catalog_internal_gap_is_rejected_before_resume(self):
        with TemporaryDirectory() as tmp:
            catalog = ParquetDataCatalog(tmp)
            catalog.write_bars([
                make_bar(
                    instrument_id="BTCUSDT-PERP.BINANCE",
                    interval="5m",
                    price_type="LAST",
                    price_precision=2,
                    size_precision=3,
                    open_="1",
                    high="1",
                    low="1",
                    close="1",
                    volume="1",
                    close_ms=close_ms,
                )
                for close_ms in (300_000, 900_000)
            ])
            with self.assertRaisesRegex(ValueError, "catalog continuity"):
                sync_bar_stream(
                    client=FakeClient([]),
                    catalog=catalog,
                    symbol="BTCUSDT",
                    instrument_id="BTCUSDT-PERP.BINANCE",
                    dataset="trade",
                    interval="5m",
                    price_type="LAST",
                    price_precision=2,
                    size_precision=3,
                    start_ms=0,
                    end_ms=900_000,
                    chunk_days=30,
                )

    def test_catalog_write_readback_rejects_dropped_middle_bar(self):
        rows = [
            Kline(open_ms, open_ms + 300_000, "1", "1", "1", "1", "1")
            for open_ms in (0, 300_000, 600_000)
        ]
        with TemporaryDirectory() as tmp:
            underlying = ParquetDataCatalog(tmp)

            class DroppingCatalog:
                def query_bars(self, *args, **kwargs):
                    return underlying.query_bars(*args, **kwargs)

                def query_last_timestamp(self, *args, **kwargs):
                    return underlying.query_last_timestamp(*args, **kwargs)

                def write_bars(self, bars):
                    underlying.write_bars([bars[0], bars[-1]])

            with self.assertRaisesRegex(RuntimeError, "catalog readback continuity"):
                sync_bar_stream(
                    client=FakeClient(rows),
                    catalog=DroppingCatalog(),
                    symbol="BTCUSDT",
                    instrument_id="BTCUSDT-PERP.BINANCE",
                    dataset="trade",
                    interval="5m",
                    price_type="LAST",
                    price_precision=2,
                    size_precision=3,
                    start_ms=0,
                    end_ms=900_000,
                    chunk_days=30,
                )

    def test_initial_empty_or_late_page_is_not_misclassified_as_prelisting(self):
        for rows, expected in (
            ([], "missing"),
            ([Kline(300_000, 600_000, "1", "1", "1", "1", "1")], "gap"),
        ):
            with self.subTest(expected=expected), TemporaryDirectory() as tmp:
                with self.assertRaisesRegex(ValueError, expected):
                    sync_bar_stream(
                        client=FakeClient(rows),
                        catalog=ParquetDataCatalog(tmp),
                        symbol="BTCUSDT",
                        instrument_id="BTCUSDT-PERP.BINANCE",
                        dataset="trade",
                        interval="5m",
                        price_type="LAST",
                        price_precision=2,
                        size_precision=3,
                        start_ms=0,
                        end_ms=600_000,
                        chunk_days=30,
                    )

    def test_funding_gap_over_eight_hours_is_rejected(self):
        rows = [
            {"fundingTime": 0, "fundingRate": "0.0001", "markPrice": "1000"},
            {"fundingTime": 9 * 60 * 60 * 1000, "fundingRate": "0.0002", "markPrice": "1000"},
        ]
        with self.assertRaisesRegex(ValueError, "interval"):
            funding_events("BTCUSDT-PERP.BINANCE", rows)

    def test_subminute_funding_timestamp_jitter_is_not_a_missing_interval(self):
        eight_hours_ms = 8 * 60 * 60_000

        class FundingClient:
            @staticmethod
            def funding(_symbol, _start_ms, _end_ms):
                return [
                    {"fundingTime": 0, "fundingRate": "0.0001"},
                    {"fundingTime": eight_hours_ms + 5, "fundingRate": "0.0002"},
                ]

        with TemporaryDirectory() as tmp:
            store = FundingJsonStore(Path(tmp) / "funding.jsonl")
            result = sync_funding_stream(
                client=FundingClient(),
                store=store,
                symbol="BTCUSDT",
                instrument_id="BTCUSDT-PERP.BINANCE",
                start_ms=0,
                end_ms=2 * eight_hours_ms,
            )
            self.assertEqual(result["written"], 2)
            self.assertEqual(store.load()[0].interval, 480)

    def test_funding_tail_must_reach_within_eight_hours_of_boundary(self):
        class FundingClient:
            @staticmethod
            def funding(_symbol, start_ms, _end_ms):
                return [{"fundingTime": start_ms, "fundingRate": "0.0001"}]

        with TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "tail gap"):
                sync_funding_stream(
                    client=FundingClient(),
                    store=FundingJsonStore(Path(tmp) / "funding.jsonl"),
                    symbol="BTCUSDT",
                    instrument_id="BTCUSDT-PERP.BINANCE",
                    start_ms=0,
                    end_ms=24 * 60 * 60 * 1000,
                )

    def test_initial_funding_head_must_match_requested_start(self):
        class FundingClient:
            @staticmethod
            def funding(_symbol, start_ms, _end_ms):
                return [{"fundingTime": start_ms + 8 * 60 * 60_000, "fundingRate": "0.0001"}]

        with TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "funding head gap"):
                sync_funding_stream(
                    client=FundingClient(),
                    store=FundingJsonStore(Path(tmp) / "funding.jsonl"),
                    symbol="BTCUSDT",
                    instrument_id="BTCUSDT-PERP.BINANCE",
                    start_ms=0,
                    end_ms=16 * 60 * 60_000,
                )

    def test_existing_funding_internal_gap_is_rejected_before_resume(self):
        instrument_id = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
        gap_ms = 16 * 60 * 60_000

        class FundingClient:
            @staticmethod
            def funding(_symbol, start_ms, _end_ms):
                return [{"fundingTime": start_ms, "fundingRate": "0.0001"}]

        with TemporaryDirectory() as tmp:
            store = FundingJsonStore(Path(tmp) / "funding.jsonl")
            store.append([
                FundingRateUpdate(instrument_id, Decimal("0.0001"), 0, 0),
                FundingRateUpdate(instrument_id, Decimal("0.0001"), gap_ms * 1_000_000, gap_ms * 1_000_000),
            ])
            with self.assertRaisesRegex(ValueError, "funding continuity"):
                sync_funding_stream(
                    client=FundingClient(),
                    store=store,
                    symbol="BTCUSDT",
                    instrument_id=str(instrument_id),
                    start_ms=0,
                    end_ms=24 * 60 * 60_000,
                )

    def test_funding_api_gap_fails_before_mutating_store(self):
        interval_ms = 8 * 60 * 60_000
        instrument_id = "BTCUSDT-PERP.BINANCE"

        class FundingClient:
            @staticmethod
            def funding(_symbol, _start_ms, _end_ms):
                return [{"fundingTime": 2 * interval_ms, "fundingRate": "0.0003"}]

        with TemporaryDirectory() as tmp:
            store = FundingJsonStore(Path(tmp) / "funding.jsonl")
            store.append(legacy_funding_events(instrument_id, [
                {"fundingTime": 0, "fundingRate": "0.0001"},
                {"fundingTime": interval_ms, "fundingRate": "0.0002"},
            ]))
            before = store.load()
            with self.assertRaisesRegex(ValueError, "funding API gap"):
                sync_funding_stream(
                    client=FundingClient(),
                    store=store,
                    symbol="BTCUSDT",
                    instrument_id=instrument_id,
                    start_ms=0,
                    end_ms=3 * interval_ms,
                )
            self.assertEqual(store.load(), before)

    def test_each_funding_rate_settles_once_at_its_own_boundary(self):
        rows = [
            {
                "fundingTime": 8 * 60 * 60_000,
                "fundingRate": "0.0001",
                "markPrice": "1000.00",
                "rateType": "Regular",
            },
            {
                "fundingTime": 16 * 60 * 60_000,
                "fundingRate": "-0.0002",
                "markPrice": "2000.00",
                "rateType": "Regular",
            },
        ]
        events = funding_events("BTCUSDT-PERP.BINANCE", rows)
        self.assertEqual([event.rate for event in events], [Decimal("0.0001"), Decimal("-0.0002")])
        self.assertEqual(
            [event.next_funding_ns for event in events],
            [event.ts_event for event in events],
            "Binance fundingTime is this row's settlement boundary; a rate must not shift to the next row",
        )

    def test_funding_row_without_settlement_mark_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "markPrice"):
            funding_events(
                "BTCUSDT-PERP.BINANCE",
                [{"fundingTime": 8 * 60 * 60_000, "fundingRate": "0.0001"}],
            )


if __name__ == "__main__":
    unittest.main()
