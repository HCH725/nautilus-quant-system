from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from nautilus_quant.backtest import run_funding_oracle


ROOT = Path(__file__).resolve().parents[1]
HOUR_NS = 60 * 60 * 1_000_000_000


class FundingBacktestTests(unittest.TestCase):
    def _run_mutated_fixture(self, mutate):
        fixture = json.loads((ROOT / "config/backtest_pilot.json").read_text())
        mutate(fixture)
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "fixture.json"
            path.write_text(json.dumps(fixture))
            return run_funding_oracle(path)

    def test_official_fixture_proves_exact_funding_fees_account_events_and_flat_end(self):
        report = run_funding_oracle(ROOT / "config/backtest_pilot.json")

        self.assertEqual(report["engine"], "nautilus_trader==2.0.0rc2")
        self.assertEqual(report["truth_status"], "official")
        self.assertEqual(report["funding_price_source"], "binance_funding_history_mark_price")
        self.assertTrue(report["performance_claimable"])
        self.assertEqual(report["same_timestamp_order"], "mark_then_funding")
        self.assertEqual(report["starting_balance"], "1000.00000000")
        self.assertEqual(report["ending_balance"], "998.50000000")
        self.assertEqual(report["account_delta"], "-1.50000000")
        self.assertEqual(report["fees"]["total"], "-4.00000000")
        self.assertEqual(
            [(event["ts_event_ns"], event["amount"]) for event in report["fees"]["events"]],
            [
                (1 * HOUR_NS, "-1.00000000"),
                (17 * HOUR_NS, "-1.00000000"),
                (21 * HOUR_NS, "-1.00000000"),
                (33 * HOUR_NS, "-1.00000000"),
            ],
        )
        self.assertEqual(report["funding"]["total"], "2.50000000")
        self.assertEqual(
            report["funding"]["events"],
            [
                {
                    "ts_event_ns": 8 * HOUR_NS,
                    "direction": "LONG",
                    "rate": "0.001",
                    "mark_price": "1000",
                    "price_source": "binance_funding_history_mark_price",
                    "amount": "-1.00000000",
                },
                {
                    "ts_event_ns": 16 * HOUR_NS,
                    "direction": "LONG",
                    "rate": "-0.002",
                    "mark_price": "2000",
                    "price_source": "binance_funding_history_mark_price",
                    "amount": "4.00000000",
                },
                {
                    "ts_event_ns": 24 * HOUR_NS,
                    "direction": "SHORT",
                    "rate": "0.0015",
                    "mark_price": "1000",
                    "price_source": "binance_funding_history_mark_price",
                    "amount": "1.50000000",
                },
                {
                    "ts_event_ns": 32 * HOUR_NS,
                    "direction": "SHORT",
                    "rate": "-0.001",
                    "mark_price": "2000",
                    "price_source": "binance_funding_history_mark_price",
                    "amount": "-2.00000000",
                },
            ],
        )
        self.assertEqual(report["flat_funding_boundaries_ns"], [20 * HOUR_NS, 40 * HOUR_NS])
        self.assertEqual(report["ending_position"], "FLAT")
        self.assertEqual(report["open_position_count"], 0)
        self.assertGreaterEqual(len(report["account_events"]), 9)
        self.assertEqual(Decimal(report["funding"]["total"]) + Decimal(report["fees"]["total"]), Decimal(report["account_delta"]))
        self.assertEqual(
            report["canonical_result_hash"],
            "8b8ea7b5f96d669d5282dd3b8636bbff44bb8095c8f1e62f2cc6d6de29846e60",
        )

    def test_missing_mark_is_modeled_from_top_of_book_and_not_claimable(self):
        report = run_funding_oracle(
            ROOT / "config/backtest_pilot.json",
            scenario_name="modeled_missing_mark",
        )

        self.assertEqual(report["truth_status"], "modeled_funding")
        self.assertIsNone(report["funding_price_source"])
        self.assertFalse(report["performance_claimable"])
        self.assertEqual(report["same_timestamp_order"], "top_of_book_fallback")
        self.assertEqual(report["funding"]["total"], "-0.75000000")
        self.assertEqual(report["funding"]["events"][0]["mark_price"], "750.00")
        self.assertIsNone(report["funding"]["events"][0]["price_source"])
        self.assertEqual(report["fees"]["total"], "-2.00000000")
        self.assertEqual(report["ending_balance"], "997.25000000")
        self.assertEqual(report["ending_position"], "FLAT")

    def test_boolean_schema_version_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "schema v1"):
            self._run_mutated_fixture(lambda fixture: fixture.__setitem__("schema_version", True))

    def test_duplicate_json_key_fails_closed(self):
        source = (ROOT / "config/backtest_pilot.json").read_text()
        duplicate = source.replace('"schema_version": 1,', '"schema_version": 1,\n  "schema_version": 1,')
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "fixture.json"
            path.write_text(duplicate)
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                run_funding_oracle(path)

    def test_official_scenario_without_mark_fails_closed(self):
        def remove_mark(fixture):
            fixture["official"]["funding"][0]["mark_price"] = None

        with self.assertRaisesRegex(ValueError, "official.*mark_price"):
            self._run_mutated_fixture(remove_mark)

    def test_modeled_scenario_with_mark_fails_closed(self):
        def add_mark(fixture):
            fixture["modeled_missing_mark"]["funding"][0]["mark_price"] = "750"

        with self.assertRaisesRegex(ValueError, "modeled.*mark_price"):
            self._run_mutated_fixture(add_mark)

    def test_unselected_scenario_rows_are_validated(self):
        def corrupt_modeled_quote(fixture):
            fixture["modeled_missing_mark"]["quotes"][0]["action"] = "HOLD"

        with self.assertRaisesRegex(ValueError, "quote action"):
            self._run_mutated_fixture(corrupt_modeled_quote)

    def test_unselected_scenario_duplicate_boundaries_fail_closed(self):
        def duplicate_modeled_quote(fixture):
            fixture["modeled_missing_mark"]["quotes"].append(
                dict(fixture["modeled_missing_mark"]["quotes"][0]),
            )

        with self.assertRaisesRegex(ValueError, "duplicate quote timestamp"):
            self._run_mutated_fixture(duplicate_modeled_quote)


if __name__ == "__main__":
    unittest.main()
