from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest

from nautilus_quant.pybroker_candidate import load_pybroker_candidate
from nautilus_quant.strategy_families import (
    ClosedBar,
    KERNEL_HASH,
    KERNEL_VERSION,
    evaluate_batch,
)


FAMILY_ID = "lookback-momentum-long-flat"
FAMILY_VERSION = "lookback-momentum-long-flat-v1"
PARAMETERS = {"entry_threshold": 0.05, "lookback_bars": 2}


def canonical_bytes(candidate: dict[str, object]) -> bytes:
    return (
        json.dumps(candidate, allow_nan=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()


def valid_candidate() -> dict[str, object]:
    return {
        "bar_type": "BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "runtime": {
            "pybroker_version": "1.2.14",
            "python_version": "3.12.13",
            "seed": 42,
        },
        "schema_version": "pybroker-candidate-v1",
        "signals": [
            {"intent": "LONG", "score": 0.1, "ts_event_ns": 1},
            {"intent": "FLAT", "score": -0.1, "ts_event_ns": 2},
        ],
        "source": {
            "first_ts_event_ns": 1,
            "last_ts_event_ns": 2,
            "row_count": 2,
            "sha256": "0" * 64,
        },
        "strategy": {
            "decision_timing": "bar-close; effective no earlier than next event",
            "name": "sma-long-flat",
            "parameters": {"window": 2},
        },
        "truth_status": "provisional",
    }


def valid_candidate_v2() -> dict[str, object]:
    decisions = evaluate_batch(
        family_id=FAMILY_ID,
        family_version=FAMILY_VERSION,
        parameters=PARAMETERS,
        bars=[
            ClosedBar(1, 100, 100, 100, 100, 1),
            ClosedBar(2, 110, 110, 110, 110, 1),
        ],
    )
    return {
        "bar_type": "BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",
        "evaluation_context_id": "e" * 64,
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "runtime": {
            "environment_id": "d" * 64,
            "pybroker_version": "1.2.14",
            "python_version": "3.12.13",
            "seed": 42,
        },
        "schema_version": "pybroker-candidate-v2",
        "signals": [
            {
                "family_id": item.family_id,
                "family_version": item.family_version,
                "kernel_hash": item.kernel_hash,
                "kernel_version": item.kernel_version,
                "reason": item.reason,
                "score": item.score,
                "signal_id": item.signal_id,
                "target_intent": item.target_intent,
                "ts_event_ns": item.ts_event_ns,
            }
            for item in decisions
        ],
        "source": {
            "data_as_of_ns": 2,
            "data_snapshot_id": "0" * 64,
            "first_ts_event_ns": 1,
            "last_ts_event_ns": 2,
            "row_count": 2,
            "sha256": "0" * 64,
        },
        "strategy": {
            "decision_timing": "bar-close; effective no earlier than next event",
            "family_id": FAMILY_ID,
            "family_version": FAMILY_VERSION,
            "kernel_hash": KERNEL_HASH,
            "kernel_version": KERNEL_VERSION,
            "parameters": dict(PARAMETERS),
        },
        "truth_status": "provisional",
    }


class PyBrokerCandidateTests(unittest.TestCase):
    def _load(self, candidate: dict[str, object]):
        payload = canonical_bytes(candidate)
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        path = Path(temporary_directory.name) / "candidate.json"
        path.write_bytes(payload)
        return load_pybroker_candidate(path), payload

    def test_loads_canonical_plain_data_candidate(self):
        (candidate, candidate_id), payload = self._load(valid_candidate())

        self.assertEqual(candidate["schema_version"], "pybroker-candidate-v1")
        self.assertEqual(candidate_id, sha256(payload).hexdigest())

    def test_v1_candidate_bytes_and_content_id_remain_stable(self):
        payload = canonical_bytes(valid_candidate())
        expected_id = sha256(payload).hexdigest()

        (candidate, candidate_id), loaded_payload = self._load(valid_candidate())

        self.assertEqual(loaded_payload, payload)
        self.assertEqual(candidate["schema_version"], "pybroker-candidate-v1")
        self.assertEqual(candidate_id, expected_id)

    def test_loads_v2_with_complete_family_kernel_source_and_runtime_identity(self):
        (candidate, candidate_id), payload = self._load(valid_candidate_v2())

        self.assertEqual(candidate["schema_version"], "pybroker-candidate-v2")
        self.assertEqual(candidate_id, sha256(payload).hexdigest())
        signal = cast(list[dict[str, object]], candidate["signals"])[0]
        self.assertEqual(signal["kernel_hash"], KERNEL_HASH)
        self.assertEqual(signal["score"], "0.1")
        self.assertEqual(signal["target_intent"], "LONG")

    def test_v2_rejects_missing_identity_or_tampered_signal_id(self):
        missing = valid_candidate_v2()
        missing.pop("evaluation_context_id")
        with self.assertRaisesRegex(ValueError, "candidate v2 fields"):
            self._load(missing)

        tampered = valid_candidate_v2()
        signals = cast(list[dict[str, object]], tampered["signals"])
        signals[0]["reason"] = "TAMPERED"
        with self.assertRaisesRegex(ValueError, "signal_id mismatch"):
            self._load(tampered)

    def test_v2_rejects_noncanonical_score_or_wrong_kernel_identity(self):
        for field, value, message in (
            ("score", "0.1000", "canonical decimal string"),
            ("kernel_hash", "f" * 64, "kernel identity"),
            ("kernel_version", "other", "kernel identity"),
        ):
            with self.subTest(field=field):
                candidate = valid_candidate_v2()
                signals = cast(list[dict[str, object]], candidate["signals"])
                signals[0][field] = value
                with self.assertRaisesRegex(ValueError, message):
                    self._load(candidate)

    def test_rejects_noncanonical_encoding(self):
        payload = json.dumps(valid_candidate(), indent=2).encode()
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "candidate.json"
            path.write_bytes(payload)

            with self.assertRaisesRegex(ValueError, "canonical JSON"):
                load_pybroker_candidate(path)

    def test_rejects_executable_or_trading_payload_fields(self):
        for candidate in (valid_candidate(), valid_candidate_v2()):
            with self.subTest(schema=candidate["schema_version"]):
                strategy = cast(dict[str, object], candidate["strategy"])
                parameters = cast(dict[str, object], strategy["parameters"])
                parameters["import_path"] = "package.module:factory"
                with self.assertRaisesRegex(ValueError, "forbidden candidate field"):
                    self._load(candidate)


if __name__ == "__main__":
    unittest.main()
