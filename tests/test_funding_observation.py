from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch
import hashlib
import json
import unittest

from nautilus_quant.funding_observation import (
    FundingObservation,
    FundingObservationStore,
    migrate_funding_observations,
    observations_from_api_rows,
    read_funding_observations,
    sync_funding_generation,
)


HOUR_MS = 60 * 60_000


def api_row(time_ms: int, rate: str, mark: object, rate_type: str | None = None) -> dict[str, object]:
    row: dict[str, object] = {
        "symbol": "BTCUSDT",
        "fundingTime": time_ms,
        "fundingRate": rate,
        "markPrice": mark,
    }
    if rate_type is not None:
        row["rateType"] = rate_type
    return row


class FakeFundingClient:
    def __init__(self, rows_by_symbol: dict[str, list[dict[str, object]]]) -> None:
        self.rows_by_symbol = rows_by_symbol

    def funding(self, symbol: str, start_ms: int, end_ms: int) -> list[dict[str, object]]:
        return [
            {**row, "symbol": symbol}
            for row in self.rows_by_symbol[symbol]
            if start_ms <= int(row["fundingTime"]) < end_ms
        ]


class FundingObservationTests(unittest.TestCase):
    def test_official_and_modeled_rows_roundtrip_as_canonical_v1_jsonl(self):
        official = FundingObservation.from_api_row(
            "BTCUSDT-PERP.BINANCE",
            api_row(8 * HOUR_MS, "0.000100", "1000.00", "Regular"),
        )
        modeled = FundingObservation.from_api_row(
            "BTCUSDT-PERP.BINANCE",
            api_row(0, "-0.000200", ""),
        )

        official_bytes = (
            b'{"instrument_id":"BTCUSDT-PERP.BINANCE","funding_time_ns":28800000000000,'
            b'"rate":"0.0001","mark_price":"1000","rate_type":"Regular",'
            b'"truth_status":"official","funding_price_source":"binance_funding_history_mark_price"}\n'
        )
        modeled_bytes = (
            b'{"instrument_id":"BTCUSDT-PERP.BINANCE","funding_time_ns":0,'
            b'"rate":"-0.0002","mark_price":null,"rate_type":null,'
            b'"truth_status":"modeled_funding","funding_price_source":null}\n'
        )
        self.assertEqual(official.to_jsonl(), official_bytes)
        self.assertEqual(modeled.to_jsonl(), modeled_bytes)
        self.assertEqual(FundingObservation.from_jsonl(official_bytes.rstrip()), official)
        self.assertEqual(FundingObservation.from_jsonl(modeled_bytes.rstrip()), modeled)
        self.assertEqual(official.mark_price, Decimal("1000"))
        self.assertIsNone(modeled.mark_price)

    def test_api_trust_boundary_and_truth_transition_fail_closed(self):
        valid = api_row(0, "0.0001", "1000")
        invalid_rows = (
            ({key: value for key, value in valid.items() if key != "fundingTime"}, "fundingTime"),
            ({key: value for key, value in valid.items() if key != "fundingRate"}, "fundingRate"),
            ({key: value for key, value in valid.items() if key != "markPrice"}, "markPrice"),
            ({**valid, "symbol": "ETHUSDT"}, "symbol"),
            ({**valid, "fundingTime": True}, "fundingTime"),
            ({**valid, "fundingTime": -1}, "fundingTime"),
            ({**valid, "fundingRate": "NaN"}, "fundingRate"),
            ({**valid, "markPrice": "0"}, "markPrice"),
            ({**valid, "markPrice": "Infinity"}, "markPrice"),
            ({**valid, "rateType": 1}, "rateType"),
        )
        for row, field in invalid_rows:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                FundingObservation.from_api_row("BTCUSDT-PERP.BINANCE", row)

        rows = [
            api_row(0, "0.0001", ""),
            api_row(8 * HOUR_MS, "0.0002", "1000"),
            api_row(16 * HOUR_MS, "0.0003", ""),
        ]
        with self.assertRaisesRegex(ValueError, "after first official"):
            observations_from_api_rows("BTCUSDT-PERP.BINANCE", rows)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            observations_from_api_rows("BTCUSDT-PERP.BINANCE", [valid, valid])
        with self.assertRaisesRegex(ValueError, "conflicting"):
            observations_from_api_rows("BTCUSDT-PERP.BINANCE", [valid, {**valid, "fundingRate": "0.9"}])

    def test_store_bytes_and_hash_are_deterministic_and_replace_is_atomic(self):
        observations = observations_from_api_rows("BTCUSDT-PERP.BINANCE", [
            api_row(0, "0.0001", ""),
            api_row(8 * HOUR_MS, "-0.0002", "1000"),
        ])
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.jsonl"
            store = FundingObservationStore(path)
            first_hash = store.write(observations)
            first_bytes = path.read_bytes()
            second_hash = store.write(reversed(observations))

            self.assertEqual(store.load(), observations)
            self.assertEqual(path.read_bytes(), first_bytes)
            self.assertEqual(first_hash, second_hash)
            self.assertEqual(first_hash, hashlib.sha256(first_bytes).hexdigest())

            with patch("nautilus_quant.funding_observation.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    store.write(observations[:1])
            self.assertEqual(path.read_bytes(), first_bytes)
            self.assertFalse(any(path.parent.glob(f".{path.name}.*")))

    def test_migration_publishes_deterministic_generation_and_preserves_legacy(self):
        rows = [
            api_row(0, "0.0001", ""),
            api_row(8 * HOUR_MS, "0.0002", "1000"),
            api_row(16 * HOUR_MS, "-0.0003", "1100"),
        ]
        outputs = []
        for _attempt in range(2):
            with TemporaryDirectory() as tmp:
                funding_path = Path(tmp)
                legacy = {
                    "BTCUSDT-PERP.BINANCE.jsonl": b"legacy-btc\n",
                    "ETHUSDT-PERP.BINANCE.jsonl": b"legacy-eth\n",
                }
                for name, content in legacy.items():
                    (funding_path / name).write_bytes(content)

                pointer = migrate_funding_observations(
                    client=FakeFundingClient({"BTCUSDT": rows, "ETHUSDT": rows}),
                    funding_path=funding_path,
                    symbols=("ETHUSDT", "BTCUSDT"),
                    start_ms=0,
                    end_ms=24 * HOUR_MS,
                )
                readback = read_funding_observations(funding_path, symbols=("BTCUSDT", "ETHUSDT"))

                self.assertEqual(pointer["status"], "READY")
                self.assertEqual({name: (funding_path / name).read_bytes() for name in legacy}, legacy)
                self.assertEqual([item.truth_status for item in readback["BTCUSDT"]], [
                    "modeled_funding",
                    "official",
                    "official",
                ])
                generation = funding_path / "funding-observations.v1.generations" / pointer["generation"]
                manifest = json.loads((generation / "manifest.json").read_bytes())
                self.assertEqual(manifest["truth_counts"], {"modeled_funding": 2, "official": 4})
                self.assertTrue((generation / "rollback.json").is_file())
                outputs.append({
                    path.relative_to(funding_path).as_posix(): path.read_bytes()
                    for path in sorted(funding_path.rglob("*"))
                    if path.is_file() and not path.name.endswith("-PERP.BINANCE.jsonl")
                })
        self.assertEqual(outputs[0], outputs[1])

    def test_symbol_failure_publishes_no_generation_or_pointer(self):
        rows = [api_row(0, "0.0001", ""), api_row(8 * HOUR_MS, "0.0002", "1000")]

        class FailingClient(FakeFundingClient):
            def funding(self, symbol: str, start_ms: int, end_ms: int) -> list[dict[str, object]]:
                if symbol == "ETHUSDT":
                    raise RuntimeError("ETH failed")
                return super().funding(symbol, start_ms, end_ms)

        with TemporaryDirectory() as tmp:
            funding_path = Path(tmp)
            legacy = funding_path / "BTCUSDT-PERP.BINANCE.jsonl"
            legacy.write_bytes(b"legacy\n")
            with self.assertRaisesRegex(RuntimeError, "ETH failed"):
                migrate_funding_observations(
                    client=FailingClient({"BTCUSDT": rows, "ETHUSDT": rows}),
                    funding_path=funding_path,
                    symbols=("BTCUSDT", "ETHUSDT"),
                    start_ms=0,
                    end_ms=16 * HOUR_MS,
                )
            self.assertEqual(legacy.read_bytes(), b"legacy\n")
            self.assertFalse((funding_path / "funding-observations.v1.ready.json").exists())
            self.assertFalse(list((funding_path / "funding-observations.v1.generations").glob("*")))

    def test_unpublished_generation_is_safe_to_retry_without_manual_cleanup(self):
        rows = [api_row(0, "0.0001", ""), api_row(8 * HOUR_MS, "0.0002", "1000")]
        with TemporaryDirectory() as tmp:
            funding_path = Path(tmp)

            def crash_before_pointer(stage: str) -> None:
                if stage == "before_pointer":
                    raise RuntimeError("crash before pointer")

            with self.assertRaisesRegex(RuntimeError, "crash before pointer"):
                migrate_funding_observations(
                    client=FakeFundingClient({"BTCUSDT": rows}),
                    funding_path=funding_path,
                    symbols=("BTCUSDT",),
                    start_ms=0,
                    end_ms=16 * HOUR_MS,
                    publication_hook=crash_before_pointer,
                )
            self.assertFalse((funding_path / "funding-observations.v1.ready.json").exists())
            self.assertEqual(len(list((funding_path / "funding-observations.v1.generations").iterdir())), 1)

            pointer = migrate_funding_observations(
                client=FakeFundingClient({"BTCUSDT": rows}),
                funding_path=funding_path,
                symbols=("BTCUSDT",),
                start_ms=0,
                end_ms=16 * HOUR_MS,
            )
            self.assertEqual(pointer["status"], "READY")
            self.assertEqual(len(read_funding_observations(funding_path, symbols=("BTCUSDT",))["BTCUSDT"]), 2)

    def test_crash_after_pointer_leaves_complete_readable_generation(self):
        rows = [api_row(0, "0.0001", ""), api_row(8 * HOUR_MS, "0.0002", "1000")]
        with TemporaryDirectory() as tmp:
            funding_path = Path(tmp)

            def crash_after_pointer(stage: str) -> None:
                if stage == "after_pointer":
                    raise RuntimeError("crash after pointer")

            with self.assertRaisesRegex(RuntimeError, "crash after pointer"):
                migrate_funding_observations(
                    client=FakeFundingClient({"BTCUSDT": rows}),
                    funding_path=funding_path,
                    symbols=("BTCUSDT",),
                    start_ms=0,
                    end_ms=16 * HOUR_MS,
                    publication_hook=crash_after_pointer,
                )
            self.assertEqual(len(read_funding_observations(funding_path, symbols=("BTCUSDT",))["BTCUSDT"]), 2)

    def test_final_generation_readback_failure_does_not_publish_initial_pointer(self):
        rows = [api_row(0, "0.0001", ""), api_row(8 * HOUR_MS, "0.0002", "1000")]
        with TemporaryDirectory() as tmp:
            funding_path = Path(tmp)
            real_load = FundingObservationStore.load

            def fail_from_generation(store: FundingObservationStore):
                if "funding-observations.v1.generations" in store.path.parts:
                    raise ValueError("final generation readback failed")
                return real_load(store)

            with (
                patch.object(FundingObservationStore, "load", fail_from_generation),
                self.assertRaisesRegex(ValueError, "final generation readback failed"),
            ):
                migrate_funding_observations(
                    client=FakeFundingClient({"BTCUSDT": rows}),
                    funding_path=funding_path,
                    symbols=("BTCUSDT",),
                    start_ms=0,
                    end_ms=16 * HOUR_MS,
                )
            self.assertFalse((funding_path / "funding-observations.v1.ready.json").exists())

    def test_failed_final_readback_keeps_previous_generation_visible(self):
        initial = [api_row(0, "0.0001", ""), api_row(8 * HOUR_MS, "0.0002", "1000")]
        extended = [*initial, api_row(16 * HOUR_MS, "-0.0003", "1100")]
        with TemporaryDirectory() as tmp:
            funding_path = Path(tmp)
            migrate_funding_observations(
                client=FakeFundingClient({"BTCUSDT": initial}),
                funding_path=funding_path,
                symbols=("BTCUSDT",),
                start_ms=0,
                end_ms=16 * HOUR_MS,
            )
            pointer_path = funding_path / "funding-observations.v1.ready.json"
            old_pointer = pointer_path.read_bytes()
            old_generation = json.loads(old_pointer)["generation"]
            old_rows = read_funding_observations(funding_path, symbols=("BTCUSDT",))["BTCUSDT"]
            real_load = FundingObservationStore.load

            def fail_new_generation(store: FundingObservationStore):
                if (
                    "funding-observations.v1.generations" in store.path.parts
                    and store.path.parent.name != old_generation
                ):
                    raise ValueError("new generation readback failed")
                return real_load(store)

            with (
                patch.object(FundingObservationStore, "load", fail_new_generation),
                self.assertRaisesRegex(ValueError, "new generation readback failed"),
            ):
                sync_funding_generation(
                    client=FakeFundingClient({"BTCUSDT": extended}),
                    funding_path=funding_path,
                    symbols=("BTCUSDT",),
                    start_ms=0,
                    end_ms=24 * HOUR_MS,
                )
            self.assertEqual(pointer_path.read_bytes(), old_pointer)
            self.assertEqual(
                read_funding_observations(funding_path, symbols=("BTCUSDT",))["BTCUSDT"],
                old_rows,
            )

    def test_reader_rejects_boolean_manifest_counts_after_rebinding_content_hash(self):
        rows = [api_row(0, "0.0001", "")]
        for target, expected_error in (("entry", "manifest entry"), ("truth", "truth counts")):
            with self.subTest(target=target), TemporaryDirectory() as tmp:
                funding_path = Path(tmp)
                pointer = migrate_funding_observations(
                    client=FakeFundingClient({"BTCUSDT": rows}),
                    funding_path=funding_path,
                    symbols=("BTCUSDT",),
                    start_ms=0,
                    end_ms=8 * HOUR_MS,
                )
                generations_path = funding_path / "funding-observations.v1.generations"
                old_generation = generations_path / cast(str, pointer["generation"])
                manifest = json.loads((old_generation / "manifest.json").read_bytes())
                if target == "entry":
                    manifest["observations"][0]["rows"] = True
                else:
                    manifest["truth_counts"]["modeled_funding"] = True
                manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode() + b"\n"
                generation = hashlib.sha256(manifest_bytes).hexdigest()
                new_generation = generations_path / generation
                old_generation.rename(new_generation)
                (new_generation / "manifest.json").write_bytes(manifest_bytes)
                (funding_path / "funding-observations.v1.ready.json").write_bytes(
                    json.dumps({
                        "schema_version": 1,
                        "status": "READY",
                        "generation": generation,
                        "manifest_sha256": generation,
                    }, separators=(",", ":")).encode() + b"\n",
                )

                with self.assertRaisesRegex(ValueError, expected_error):
                    read_funding_observations(funding_path, symbols=("BTCUSDT",))

    def test_daily_sync_failure_before_pointer_keeps_previous_generation_visible(self):
        initial = [api_row(0, "0.0001", ""), api_row(8 * HOUR_MS, "0.0002", "1000")]
        extended = [*initial, api_row(16 * HOUR_MS, "-0.0003", "1100")]
        with TemporaryDirectory() as tmp:
            funding_path = Path(tmp)
            migrate_funding_observations(
                client=FakeFundingClient({"BTCUSDT": initial}),
                funding_path=funding_path,
                symbols=("BTCUSDT",),
                start_ms=0,
                end_ms=16 * HOUR_MS,
            )
            pointer_path = funding_path / "funding-observations.v1.ready.json"
            old_pointer = pointer_path.read_bytes()

            def crash_before_pointer(stage: str) -> None:
                if stage == "before_pointer":
                    raise RuntimeError("daily crash")

            with self.assertRaisesRegex(RuntimeError, "daily crash"):
                sync_funding_generation(
                    client=FakeFundingClient({"BTCUSDT": extended}),
                    funding_path=funding_path,
                    symbols=("BTCUSDT",),
                    start_ms=0,
                    end_ms=24 * HOUR_MS,
                    publication_hook=crash_before_pointer,
                )
            self.assertEqual(pointer_path.read_bytes(), old_pointer)
            self.assertEqual(len(read_funding_observations(funding_path, symbols=("BTCUSDT",))["BTCUSDT"]), 2)

            with self.assertRaisesRegex(ValueError, "conflicting"):
                sync_funding_generation(
                    client=FakeFundingClient({
                        "BTCUSDT": [{**extended[-1], "fundingTime": 8 * HOUR_MS}],
                    }),
                    funding_path=funding_path,
                    symbols=("BTCUSDT",),
                    start_ms=0,
                    end_ms=24 * HOUR_MS,
                )
            self.assertEqual(pointer_path.read_bytes(), old_pointer)


if __name__ == "__main__":
    unittest.main()
