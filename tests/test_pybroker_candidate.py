from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest

from nautilus_quant.pybroker_candidate import load_pybroker_candidate


def canonical_bytes(candidate: dict[str, object]) -> bytes:
    return (json.dumps(candidate, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n").encode()


def valid_candidate() -> dict[str, object]:
    return {
        "bar_type": "BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "runtime": {"pybroker_version": "1.2.14", "python_version": "3.12.13", "seed": 42},
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


class PyBrokerCandidateTests(unittest.TestCase):
    def test_loads_canonical_plain_data_candidate(self):
        payload = canonical_bytes(valid_candidate())
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "candidate.json"
            path.write_bytes(payload)

            candidate, candidate_id = load_pybroker_candidate(path)

        self.assertEqual(candidate["schema_version"], "pybroker-candidate-v1")
        self.assertEqual(candidate_id, sha256(payload).hexdigest())

    def test_rejects_noncanonical_encoding(self):
        payload = json.dumps(valid_candidate(), indent=2).encode()
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "candidate.json"
            path.write_bytes(payload)

            with self.assertRaisesRegex(ValueError, "canonical JSON"):
                load_pybroker_candidate(path)

    def test_rejects_executable_or_trading_payload_fields(self):
        candidate = valid_candidate()
        strategy = cast(dict[str, object], candidate["strategy"])
        parameters = cast(dict[str, object], strategy["parameters"])
        parameters["import_path"] = "package.module:factory"
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "candidate.json"
            path.write_bytes(canonical_bytes(candidate))

            with self.assertRaisesRegex(ValueError, "forbidden candidate field"):
                load_pybroker_candidate(path)


if __name__ == "__main__":
    unittest.main()
