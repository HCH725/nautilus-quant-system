from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pybroker_research import decode_fixed, run, write_candidate


class CandidateWriterTests(unittest.TestCase):
    def test_rejects_candidate_output_inside_canonical_data(self):
        with TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            catalog = data / "catalog"
            funding = data / "funding"
            catalog.mkdir(parents=True)
            funding.mkdir()

            for output in (catalog / "candidate.json", funding / "candidate.json"):
                with self.subTest(output=output):
                    with self.assertRaisesRegex(ValueError, "outside canonical data"):
                        run(catalog, output)

    def test_rejects_canonical_output_when_catalog_is_symlink(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            target = root / "catalog-target"
            funding = data / "funding"
            target.mkdir()
            funding.mkdir(parents=True)
            catalog = data / "catalog"
            catalog.symlink_to(target, target_is_directory=True)

            for output in (funding / "candidate.json", target / "candidate.json"):
                with self.subTest(output=output):
                    with self.assertRaisesRegex(ValueError, "outside canonical data"):
                        run(catalog, output)

    def test_decodes_nautilus_high_precision_fixed_bytes(self):
        raw = bytes.fromhex("00403a1fcf3d010d1900000000000000")

        self.assertEqual(decode_fixed(raw), 46210.57)

    def test_atomically_writes_stable_canonical_json(self):
        candidate = {
            "bar_type": "BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",
            "instrument_id": "BTCUSDT-PERP.BINANCE",
            "runtime": {"pybroker_version": "1.2.14", "python_version": "3.12.13", "seed": 42},
            "schema_version": "pybroker-candidate-v1",
            "signals": [{"intent": "LONG", "score": 0.1, "ts_event_ns": 1}],
            "source": {
                "first_ts_event_ns": 1,
                "last_ts_event_ns": 1,
                "row_count": 1,
                "sha256": "0" * 64,
            },
            "strategy": {
                "decision_timing": "bar-close; effective no earlier than next event",
                "name": "sma-long-flat",
                "parameters": {"window": 24},
            },
            "truth_status": "provisional",
        }
        expected = (json.dumps(candidate, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n").encode()
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "candidate.json"

            first_id = write_candidate(candidate, path)
            second_id = write_candidate(candidate, path)

            self.assertEqual(path.read_bytes(), expected)
            self.assertEqual(first_id, sha256(expected).hexdigest())
            self.assertEqual(second_id, first_id)
            self.assertEqual(list(path.parent.iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
