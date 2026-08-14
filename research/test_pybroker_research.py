from __future__ import annotations

from contextlib import redirect_stdout
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as parquet

import pybroker_research
from pybroker_research import BAR_TYPE, decode_fixed, run, write_candidate


def hypothesis_document() -> dict[str, object]:
    return {
        "bar_type": BAR_TYPE,
        "based_on_verdict_id": None,
        "falsification": "Momentum does not persist",
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "parameters": {"entry_threshold": 0.05, "lookback_bars": 2},
        "parent_strategy_id": None,
        "schema_version": "strategy-hypothesis-v1",
        "strategy_family": "lookback-momentum-long-flat",
        "thesis": "Momentum persists into the next event",
    }


def write_hypothesis(root: Path, document: dict[str, object] | None = None) -> Path:
    path = root / "hypothesis.json"
    value = hypothesis_document() if document is None else document
    path.write_bytes((json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n").encode())
    return path


def fixed(value: int) -> bytes:
    return (value * 10**16).to_bytes(16, "little", signed=True)


def write_catalog(root: Path, closes: list[int]) -> Path:
    catalog = root / "data" / "catalog"
    bars = catalog / "data" / "bars" / BAR_TYPE
    bars.mkdir(parents=True)
    prices = [fixed(value) for value in closes]
    table = pa.table(
        {
            "open": prices,
            "high": prices,
            "low": prices,
            "close": prices,
            "volume": [fixed(1)] * len(closes),
            "ts_event": [(index + 1) * 3_600_000_000_000 for index in range(len(closes))],
        },
    ).replace_schema_metadata({b"bar_type": BAR_TYPE.encode()})
    parquet.write_table(table, bars / "part-0.parquet")
    return catalog


class CandidateWriterTests(unittest.TestCase):
    def test_repository_canonical_data_is_forbidden_even_with_an_external_catalog(self):
        with TemporaryDirectory() as tmp:
            external_catalog = Path(tmp) / "catalog"
            repository_data = Path(__file__).resolve().parents[1] / "data"

            with self.assertRaisesRegex(ValueError, "outside canonical data"):
                pybroker_research.validate_output_path(
                    external_catalog,
                    repository_data / "candidate.json",
                )

    def test_rejects_candidate_output_inside_canonical_data(self):
        with TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            catalog = data / "catalog"
            funding = data / "funding"
            catalog.mkdir(parents=True)
            funding.mkdir()
            hypothesis = write_hypothesis(Path(tmp))

            for output in (catalog / "candidate.json", funding / "candidate.json"):
                with self.subTest(output=output):
                    with self.assertRaisesRegex(ValueError, "outside canonical data"):
                        run(catalog, output, hypothesis=hypothesis)

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
            output_link = root / "output-link"
            output_link.symlink_to(funding, target_is_directory=True)
            hypothesis = write_hypothesis(root)

            for output in (
                funding / "candidate.json",
                target / "candidate.json",
                output_link / "candidate.json",
            ):
                with self.subTest(output=output):
                    with self.assertRaisesRegex(ValueError, "outside canonical data"):
                        run(catalog, output, hypothesis=hypothesis)

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
            conflicting = json.loads(json.dumps(candidate))
            conflicting["truth_status"] = "conflicting"

            with self.assertRaisesRegex(OSError, "immutable candidate conflict"):
                write_candidate(conflicting, path)

            self.assertEqual(path.read_bytes(), expected)
            self.assertEqual(first_id, sha256(expected).hexdigest())
            self.assertEqual(second_id, first_id)
            self.assertEqual(list(path.parent.iterdir()), [path])


class ParameterizedResearchTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.hypothesis = write_hypothesis(self.root)
        self.catalog = write_catalog(self.root, [100, 104, 110, 100, 100])

    def test_parameters_drive_no_lookahead_signals_and_preserve_contract(self):
        output = self.root / "run" / "candidate.json"

        result = run(self.catalog, output, hypothesis=self.hypothesis)

        candidate = json.loads(output.read_bytes())
        signals = candidate["signals"]
        self.assertEqual(candidate["schema_version"], "pybroker-candidate-v1")
        self.assertEqual(
            candidate["strategy"],
            {
                "decision_timing": "bar-close; effective no earlier than next event",
                "name": "lookback-momentum-long-flat",
                "parameters": {"entry_threshold": 0.05, "lookback_bars": 2},
            },
        )
        self.assertEqual(
            signals,
            [
                {
                    "intent": "LONG",
                    "score": round(110 / 104 - 1, 12),
                    "ts_event_ns": 10_800_000_000_000,
                },
                {
                    "intent": "FLAT",
                    "score": round(100 / 110 - 1, 12),
                    "ts_event_ns": 14_400_000_000_000,
                },
            ],
        )
        self.assertTrue(
            all(left["ts_event_ns"] < right["ts_event_ns"] for left, right in zip(signals, signals[1:])),
        )
        self.assertEqual(set(result), {"candidate_id", "provisional_metrics"})
        self.assertEqual(result["candidate_id"], sha256(output.read_bytes()).hexdigest())
        self.assertEqual(result["provisional_metrics"]["signals"], len(signals))

    def test_same_hypothesis_source_and_seed_produce_identical_candidate(self):
        first_output = self.root / "first" / "candidate.json"
        second_output = self.root / "second" / "candidate.json"

        first = run(self.catalog, first_output, hypothesis=self.hypothesis)
        second = run(self.catalog, second_output, hypothesis=self.hypothesis)

        self.assertEqual(first_output.read_bytes(), second_output.read_bytes())
        self.assertEqual(first["candidate_id"], second["candidate_id"])

    def test_strictly_rejects_invalid_hypothesis_boundary(self):
        cases = []
        for field, value in (
            ("schema_version", "strategy-hypothesis-v2"),
            ("instrument_id", "ETHUSDT-PERP.BINANCE"),
            ("bar_type", "BTCUSDT-PERP.BINANCE-1-MINUTE-LAST-EXTERNAL"),
        ):
            document = hypothesis_document()
            document[field] = value
            cases.append((field, document))
        for field, value in (("lookback_bars", True), ("lookback_bars", 8761), ("entry_threshold", -0.01)):
            document = hypothesis_document()
            document["parameters"] = {**document["parameters"], field: value}
            cases.append((field, document))
        lineage = hypothesis_document()
        lineage["parent_strategy_id"] = "a" * 64
        cases.append(("lineage", lineage))
        parameters = hypothesis_document()
        parameters["parameters"] = {**parameters["parameters"], "leverage": 2}
        cases.append(("parameters fields", parameters))
        extra = hypothesis_document()
        extra["code"] = "pass"
        cases.append(("fields", extra))

        for label, document in cases:
            with self.subTest(label=label):
                hypothesis = write_hypothesis(self.root, document)
                with self.assertRaisesRegex(ValueError, label):
                    run(self.root / "missing-catalog", self.root / "output.json", hypothesis=hypothesis)

        noncanonical = self.root / "hypothesis.json"
        noncanonical.write_text(json.dumps(hypothesis_document(), indent=2))
        with self.assertRaisesRegex(ValueError, "canonical JSON"):
            run(self.root / "missing-catalog", self.root / "output.json", hypothesis=noncanonical)

        negative_zero = hypothesis_document()
        negative_zero["parameters"] = {
            **negative_zero["parameters"],
            "entry_threshold": -0.0,
        }
        with self.assertRaisesRegex(ValueError, "canonical JSON"):
            run(
                self.root / "missing-catalog",
                self.root / "output.json",
                hypothesis=write_hypothesis(self.root, negative_zero),
            )

        canonical = write_hypothesis(self.root).read_bytes()
        for label, payload in (
            ("finite JSON", canonical.replace(b'"entry_threshold":0.05', b'"entry_threshold":NaN')),
            ("duplicate JSON key", canonical.replace(b'"schema_version":', b'"schema_version":"strategy-hypothesis-v1","schema_version":')),
        ):
            with self.subTest(label=label):
                self.hypothesis.write_bytes(payload)
                with self.assertRaisesRegex(ValueError, label):
                    run(self.root / "missing-catalog", self.root / "output.json", hypothesis=self.hypothesis)

    def test_unsupported_family_fails_before_catalog_or_output_access(self):
        document = hypothesis_document()
        document["strategy_family"] = "generated-python"
        hypothesis = write_hypothesis(self.root, document)
        missing_catalog = self.root / "missing" / "catalog"
        output = missing_catalog / "candidate.json"

        with self.assertRaisesRegex(ValueError, "strategy family"):
            run(missing_catalog, output, hypothesis=hypothesis)

        self.assertFalse(missing_catalog.exists())

    def test_cli_requires_hypothesis_and_prints_one_research_result(self):
        expected = {"candidate_id": "a" * 64, "provisional_metrics": {"orders": 1, "signals": 1}}
        output = self.root / "candidate.json"
        argv = [
            "pybroker_research.py",
            "--hypothesis",
            str(self.hypothesis),
            "--catalog",
            str(self.catalog),
            "--output",
            str(output),
        ]
        stdout = StringIO()

        with patch("sys.argv", argv), patch.object(pybroker_research, "run", return_value=expected) as mocked_run:
            with redirect_stdout(stdout):
                self.assertEqual(pybroker_research.main(), 0)

        mocked_run.assert_called_once_with(
            self.catalog, output, hypothesis=self.hypothesis,
        )
        self.assertEqual(stdout.getvalue().splitlines(), [json.dumps(expected, sort_keys=True)])


if __name__ == "__main__":
    unittest.main()
