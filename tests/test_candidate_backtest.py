# noqa: E501  # noqa: SIZE_OK — Task C keeps its fixture and acceptance points together.
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
import json
import platform
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import nautilus_trader
from nautilus_trader.model import (
    CryptoPerpetual,
    Currency,
    InstrumentId,
    Money,
    Price,
    Quantity,
    Symbol,
)
from nautilus_trader.persistence import ParquetDataCatalog

from nautilus_quant.candidate_backtest import (
    CandidateBacktestRequest,
    load_candidate_backtest_verdict,
    run_candidate_backtest,
)
from nautilus_quant.funding_observation import migrate_funding_observations
from nautilus_quant.nautilus_io import make_bar
from nautilus_quant.strategy_candidate import load_strategy_candidate
from nautilus_quant.strategy_families import (
    ClosedBar,
    KERNEL_HASH,
    KERNEL_VERSION,
    evaluate_batch,
)


ROOT = Path(__file__).resolve().parents[1]
INSTRUMENT_ID = "BTCUSDT-PERP.BINANCE"
BAR_TYPE = f"{INSTRUMENT_ID}-1-HOUR-LAST-EXTERNAL"
HOUR_MS = 60 * 60 * 1_000
HOUR_NS = HOUR_MS * 1_000_000
USDT = Currency.from_str("USDT")
BTC = Currency.from_str("BTC")
PARAMETERS = {"entry_threshold": 0.0, "lookback_bars": 2}
CLOSES = [1000, 1000, 1010, 1010, 1000, 1000, 1010, 1010, 1010, 1010]
type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]


def _canonical(value: JsonValue) -> bytes:
    return (
        json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()


def _instrument(*, production_constraints: bool = False) -> CryptoPerpetual:
    return CryptoPerpetual(
        InstrumentId.from_str(INSTRUMENT_ID),
        Symbol("BTCUSDT"),
        BTC,
        USDT,
        USDT,
        False,
        2,
        3,
        Price.from_str("0.10" if production_constraints else "0.01"),
        Quantity.from_str("0.001"),
        0,
        0,
        multiplier=Quantity.from_str("1") if production_constraints else None,
        lot_size=Quantity.from_str("0.001") if production_constraints else None,
        max_quantity=Quantity.from_str("1000") if production_constraints else None,
        min_quantity=Quantity.from_str("0.001") if production_constraints else None,
        min_notional=Money.from_str("50 USDT") if production_constraints else None,
        max_price=Price.from_str("4529764") if production_constraints else None,
        min_price=Price.from_str("556.80") if production_constraints else None,
        margin_init=Decimal("0.1") if production_constraints else Decimal("0.01"),
        margin_maint=Decimal("0.1") if production_constraints else Decimal("0.005"),
        maker_fee=Decimal("0.0002") if production_constraints else Decimal("0.001"),
        taker_fee=Decimal("0.0005") if production_constraints else Decimal("0.001"),
    )


def _catalog_digest(path: Path) -> str:
    digest = sha256()
    files = sorted((path / "data" / "bars" / BAR_TYPE).glob("*.parquet"))
    if not files:
        raise AssertionError("synthetic catalog did not write bars")
    for file_path in files:
        digest.update(file_path.relative_to(path).as_posix().encode())
        digest.update(b"\0")
        digest.update(file_path.read_bytes())
    return digest.hexdigest()


class _FundingClient:
    def __init__(self, *, modeled_first: bool) -> None:
        self.modeled_first = modeled_first

    def funding(
        self,
        symbol: str,
        start_ms: int,
        end_ms: int,
    ) -> list[dict[str, JsonValue]]:
        del start_ms, end_ms
        return [
            {
                "symbol": symbol,
                "fundingTime": 5 * HOUR_MS,
                "fundingRate": "0.01",
                "markPrice": None if self.modeled_first else "1000",
            },
            {
                "symbol": symbol,
                "fundingTime": 12 * HOUR_MS,
                "fundingRate": "0.01",
                "markPrice": "1010",
            },
        ]


class CandidateBacktestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.catalog_path = self.root / "catalog"
        self.catalog_path.mkdir()
        catalog = ParquetDataCatalog(str(self.catalog_path))
        catalog.write_instruments([_instrument()])
        catalog.write_bars(
            [
                make_bar(
                    instrument_id=INSTRUMENT_ID,
                    interval="1h",
                    price_type="LAST",
                    price_precision=2,
                    size_precision=3,
                    open_=str(close),
                    high=str(close),
                    low=str(close),
                    close=str(close),
                    volume="10",
                    close_ms=hour * HOUR_MS,
                )
                for hour, close in zip(range(1, 11), CLOSES, strict=True)
            ],
        )
        self.funding_path = self._write_funding("funding", modeled_first=False)
        self.candidate_path = self.root / "candidate.json"
        self._write_candidate()
        self.request = CandidateBacktestRequest(
            candidate_path=self.candidate_path,
            catalog_path=self.catalog_path,
            funding_path=self.funding_path,
            policy_path=self._policy("1970-01-01T00:00:00Z"),
            hypothesis_id="1" * 64,
            strategy_id="2" * 64,
            experiment_id="3" * 64,
            code_commit="test-worktree",
        )

    def _write_funding(
        self,
        name: str,
        *,
        modeled_first: bool,
        symbols: tuple[str, ...] = ("BTCUSDT",),
    ) -> Path:
        path = self.root / name
        migrate_funding_observations(
            client=_FundingClient(modeled_first=modeled_first),
            funding_path=path,
            symbols=symbols,
            start_ms=5 * HOUR_MS,
            end_ms=13 * HOUR_MS,
        )
        return path

    def _candidate(
        self,
        *,
        parameters: dict[str, JsonValue] | None = None,
        evaluation_context_id: str = "e" * 64,
    ) -> dict[str, JsonValue]:
        source_hash = _catalog_digest(self.catalog_path)
        return {
            "bar_type": BAR_TYPE,
            "evaluation_context_id": evaluation_context_id,
            "instrument_id": INSTRUMENT_ID,
            "runtime": {
                "nautilus_trader": nautilus_trader.__version__,
                "python_version": platform.python_version(),
            },
            "schema_version": "strategy-candidate-v1",
            "source": {
                "data_as_of_ns": 10 * HOUR_NS,
                "data_snapshot_id": source_hash,
                "first_ts_event_ns": HOUR_NS,
                "last_ts_event_ns": 10 * HOUR_NS,
                "row_count": 10,
                "sha256": source_hash,
            },
            "strategy": {
                "decision_timing": "bar-close; effective no earlier than next event",
                "family_id": "lookback-momentum-long-flat",
                "family_version": "lookback-momentum-long-flat-v1",
                "kernel_hash": KERNEL_HASH,
                "kernel_version": KERNEL_VERSION,
                "parameters": dict(PARAMETERS) if parameters is None else parameters,
            },
            "truth_status": "provisional",
        }

    def _write_candidate(self, candidate: dict[str, JsonValue] | None = None) -> None:
        self.candidate_path.write_bytes(_canonical(self._candidate() if candidate is None else candidate))

    def _policy(self, historical_start: str) -> Path:
        policy = json.loads((ROOT / "config/strategy_loop_policy.json").read_bytes())
        policy["historical_start"] = historical_start
        path = self.root / f"policy-{historical_start.replace(':', '-')}.json"
        path.write_bytes(_canonical(policy))
        return path

    @staticmethod
    def _tree(path: Path) -> dict[str, bytes]:
        return {
            item.relative_to(path).as_posix(): item.read_bytes()
            for item in sorted(path.rglob("*"))
            if item.is_file()
        }

    def test_strategy_loader_is_called_and_actual_source_identity_fails_closed(self) -> None:
        with patch(
            "nautilus_quant.candidate_backtest.load_strategy_candidate",
            wraps=load_strategy_candidate,
        ) as loader:
            run_candidate_backtest(self.request)

        loader.assert_called_once_with(self.candidate_path)

        for field, value in (
            ("sha256", "f" * 64),
            ("first_ts_event_ns", 0),
            ("last_ts_event_ns", 9 * HOUR_NS),
            ("row_count", 9),
        ):
            with self.subTest(field=field):
                candidate = self._candidate()
                source = dict(candidate["source"])
                source[field] = value
                if field == "sha256":
                    source["data_snapshot_id"] = value
                if field == "last_ts_event_ns":
                    source["data_as_of_ns"] = value
                candidate["source"] = source
                self._write_candidate(candidate)
                with self.assertRaisesRegex(RuntimeError, f"source {field} mismatch"):
                    run_candidate_backtest(self.request)

    def test_kernel_decisions_drive_next_event_dedupes_and_boundary(self) -> None:
        result = run_candidate_backtest(self.request)

        execution = result.verdict["execution"]
        fills = execution["fills"]
        self.assertEqual(execution["deduped_signal_count"], 4)
        self.assertEqual(execution["boundary_flattened"], False)
        self.assertEqual(execution["order_count"], 4)
        self.assertEqual(execution["fill_count"], 4)
        self.assertEqual(execution["trade_count"], 2)
        self.assertEqual(
            [
                (fill["source_signal_ts_event_ns"], fill["action_ts_event_ns"])
                for fill in fills
            ],
            [
                (3 * HOUR_NS, 4 * HOUR_NS),
                (4 * HOUR_NS, 5 * HOUR_NS),
                (7 * HOUR_NS, 8 * HOUR_NS),
                (8 * HOUR_NS, 9 * HOUR_NS),
            ],
        )
        self.assertTrue(
            all(
                fill["source_signal_ts_event_ns"] < fill["action_ts_event_ns"]
                for fill in fills
            ),
        )
        self.assertTrue(all(fill["quantity"] == "0.001" for fill in fills))
        self.assertEqual(result.verdict["ending_position"], "FLAT")
        self.assertEqual(result.verdict["open_position_count"], 0)

    def test_candidate_id_is_generic_content_identity_without_signals(self) -> None:
        candidate = self._candidate()
        self.assertNotIn("signals", candidate)
        self._write_candidate(candidate)

        result = run_candidate_backtest(self.request)

        self.assertEqual(result.verdict["candidate_id"], sha256(self.candidate_path.read_bytes()).hexdigest())
        self.assertNotIn("signal_parity", result.verdict)

    def test_strategy_parameters_override_stresses_params_through_nautilus(self) -> None:
        base = run_candidate_backtest(self.request).verdict
        stressed = run_candidate_backtest(
            replace(
                self.request,
                code_commit="a" * 40,
                evaluation_start_utc="1970-01-01T00:00:00Z",
                evaluation_end_utc="1970-01-01T10:00:00Z",
                data_as_of_ns=10 * HOUR_NS,
                evaluation_context_id="e" * 64,
                strategy_parameters_override={"entry_threshold": 0.0, "lookback_bars": 3},
            ),
        ).verdict

        self.assertEqual(stressed["candidate_id"], base["candidate_id"])
        self.assertEqual(stressed["status"], "EVALUATED")
        self.assertNotEqual(
            [
                (fill["source_signal_ts_event_ns"], fill["action_ts_event_ns"])
                for fill in stressed["execution"]["fills"]
            ],
            [
                (fill["source_signal_ts_event_ns"], fill["action_ts_event_ns"])
                for fill in base["execution"]["fills"]
            ],
        )
        self.assertEqual(
            [
                (fill["source_signal_ts_event_ns"], fill["action_ts_event_ns"])
                for fill in stressed["execution"]["fills"]
            ],
            [
                (3 * HOUR_NS, 4 * HOUR_NS),
                (5 * HOUR_NS, 6 * HOUR_NS),
                (7 * HOUR_NS, 8 * HOUR_NS),
                (9 * HOUR_NS, 10 * HOUR_NS),
            ],
        )

    def test_tampered_candidate_schema_fails_before_engine_construction(self) -> None:
        candidate = self._candidate()
        candidate["schema_version"] = "strategy-candidate-v999"
        self._write_candidate(candidate)

        with patch("nautilus_quant.candidate_backtest.BacktestEngine") as engine:
            with self.assertRaisesRegex(ValueError, "candidate|schema|encoding"):
                run_candidate_backtest(self.request)

        engine.assert_not_called()

    def test_tampered_candidate_parameters_change_identity_and_replay(self) -> None:
        base = run_candidate_backtest(self.request).verdict
        candidate = self._candidate(
            parameters={"entry_threshold": 0.0, "lookback_bars": 3},
        )
        self._write_candidate(candidate)

        stressed = run_candidate_backtest(self.request).verdict

        self.assertNotEqual(stressed["candidate_id"], base["candidate_id"])
        self.assertNotEqual(
            stressed["execution"]["fills"],
            base["execution"]["fills"],
        )

    def test_replay_starts_at_the_configured_historical_boundary(self) -> None:
        request = replace(
            self.request,
            policy_path=self._policy("1970-01-01T05:00:00Z"),
        )

        result = run_candidate_backtest(request)

        self.assertEqual(result.verdict["evaluation_windows"]["actual_first_ts_event_ns"], 5 * HOUR_NS)
        self.assertEqual(result.verdict["execution"]["fill_count"], 2)
        self.assertTrue(
            all(
                fill["action_ts_event_ns"] >= 5 * HOUR_NS
                for fill in result.verdict["execution"]["fills"]
            ),
        )

    def test_bearish_action_bar_fills_on_the_next_event_instead_of_staying_submitted(self) -> None:
        fixture = json.loads(
            (ROOT / "tests/fixtures/candidate_backtest_submitted_prefix.json").read_bytes(),
        )
        catalog_path = self.root / "bearish-catalog"
        catalog_path.mkdir()
        catalog = ParquetDataCatalog(str(catalog_path))
        catalog.write_instruments([_instrument(production_constraints=True)])
        catalog.write_bars(
            [
                make_bar(
                    instrument_id=INSTRUMENT_ID,
                    interval="1h",
                    price_type="LAST",
                    price_precision=2,
                    size_precision=3,
                    open_=bar["open"],
                    high=bar["high"],
                    low=bar["low"],
                    close=bar["close"],
                    volume=bar["volume"],
                    close_ms=bar["ts_event_ns"] // 1_000_000,
                )
                for bar in fixture["bars"]
            ],
        )
        source_hash = _catalog_digest(catalog_path)
        candidate = self._candidate()
        candidate["source"] = {
            "data_as_of_ns": fixture["bars"][-1]["ts_event_ns"],
            "data_snapshot_id": source_hash,
            "first_ts_event_ns": fixture["bars"][0]["ts_event_ns"],
            "last_ts_event_ns": fixture["bars"][-1]["ts_event_ns"],
            "row_count": len(fixture["bars"]),
            "sha256": source_hash,
        }
        candidate_path = self.root / "bearish-candidate.json"
        candidate_path.write_bytes(_canonical(candidate))
        funding_path = self.root / "prefix-funding"
        client = type(
            "PrefixFundingClient",
            (),
            {"funding": lambda _self, _symbol, _start, _end: fixture["funding"]},
        )()
        migrate_funding_observations(
            client=client,
            funding_path=funding_path,
            symbols=("BTCUSDT",),
            start_ms=fixture["funding"][0]["fundingTime"],
            end_ms=fixture["funding"][-1]["fundingTime"] + 1,
        )
        decisions = evaluate_batch(
            family_id="lookback-momentum-long-flat",
            family_version="lookback-momentum-long-flat-v1",
            parameters=dict(PARAMETERS),
            bars=[
                ClosedBar(
                    ts_event_ns=bar["ts_event_ns"],
                    open=float(bar["open"]),
                    high=float(bar["high"]),
                    low=float(bar["low"]),
                    close=float(bar["close"]),
                    volume=float(bar["volume"]),
                )
                for bar in fixture["bars"]
            ],
        )
        first_changed = next(
            item for item in decisions if item.target_intent == "LONG"
        )
        expected_action_ns = next(
            bar["ts_event_ns"]
            for bar in fixture["bars"]
            if bar["ts_event_ns"] > first_changed.ts_event_ns
        )

        result = run_candidate_backtest(
            replace(
                self.request,
                candidate_path=candidate_path,
                catalog_path=catalog_path,
                funding_path=funding_path,
            ),
        )

        fills = result.verdict["execution"]["fills"]
        self.assertGreater(len(fills), 0)
        self.assertEqual(fills[0]["source_signal_ts_event_ns"], first_changed.ts_event_ns)
        self.assertEqual(fills[0]["action_ts_event_ns"], expected_action_ns)
        self.assertEqual(fills[0]["fill_ts_event_ns"], expected_action_ns)

    def test_nautilus_fees_funding_and_account_delta_reconcile(self) -> None:
        result = run_candidate_backtest(self.request)

        verdict = result.verdict
        self.assertEqual(verdict["fees"]["source"], "nautilus_instrument_metadata")
        self.assertEqual(verdict["fees"]["taker_rate"], "0.001")
        self.assertEqual(verdict["fees"]["total"], "-0.00403000")
        self.assertEqual(verdict["funding"]["total"], "-0.01000000")
        self.assertEqual(verdict["gross_trading_result"], "-0.01000000")
        self.assertEqual(verdict["net_account_delta"], "-0.02403000")
        self.assertEqual(verdict["starting_balance"], "10000.00000000")
        self.assertEqual(verdict["ending_balance"], "9999.97597000")
        self.assertTrue(verdict["accounting_reconciled"])

    def test_official_funding_marks_before_rate_settles_once_and_store_is_read_only(self) -> None:
        before = self._tree(self.funding_path)

        result = run_candidate_backtest(self.request)

        funding = result.verdict["funding"]
        self.assertEqual(self._tree(self.funding_path), before)
        self.assertEqual(funding["same_timestamp_order"], "mark_then_funding")
        self.assertEqual(
            funding["truth_counts"],
            {"missing_mark": 0, "modeled_funding": 0, "official": 1},
        )
        self.assertEqual(
            funding["events"],
            [
                {
                    "amount": "-0.01000000",
                    "mark_price": "1000",
                    "price_source": "binance_funding_history_mark_price",
                    "rate": "0.01",
                    "truth_status": "official",
                    "ts_event_ns": 5 * HOUR_NS,
                },
            ],
        )

    def test_selects_btc_from_multi_symbol_canonical_funding_generation(self) -> None:
        funding_path = self._write_funding(
            "multi-symbol-funding",
            modeled_first=False,
            symbols=("BTCUSDT", "ETHUSDT"),
        )

        result = run_candidate_backtest(replace(self.request, funding_path=funding_path))

        self.assertEqual(result.verdict["funding"]["truth_counts"]["official"], 1)

    def test_modeled_missing_mark_uses_labeled_bar_fallback_and_is_not_claimable(self) -> None:
        modeled_path = self._write_funding("modeled-funding", modeled_first=True)

        result = run_candidate_backtest(replace(self.request, funding_path=modeled_path))

        funding = result.verdict["funding"]
        self.assertEqual(
            funding["truth_counts"],
            {"missing_mark": 1, "modeled_funding": 1, "official": 0},
        )
        self.assertEqual(funding["events"][0]["price_source"], "bar_close_fallback")
        self.assertEqual(funding["events"][0]["mark_price"], "1010.00")
        self.assertFalse(result.verdict["performance_claimable"])

    def test_modeled_funding_fallback_cannot_look_at_the_same_timestamp_bar(self) -> None:
        closes = [1000, 1000, 1010, 2000, 1000, 1000, 1010, 1010, 1010, 1010]
        catalog_path = self.root / "changing-catalog"
        catalog_path.mkdir()
        catalog = ParquetDataCatalog(str(catalog_path))
        catalog.write_instruments([_instrument()])
        catalog.write_bars(
            [
                make_bar(
                    instrument_id=INSTRUMENT_ID,
                    interval="1h",
                    price_type="LAST",
                    price_precision=2,
                    size_precision=3,
                    open_=str(close),
                    high=str(close),
                    low=str(close),
                    close=str(close),
                    volume="10",
                    close_ms=hour * HOUR_MS,
                )
                for hour, close in zip(range(1, 11), closes, strict=True)
            ],
        )
        candidate = self._candidate()
        source_hash = _catalog_digest(catalog_path)
        candidate["source"] = {
            "data_as_of_ns": 10 * HOUR_NS,
            "data_snapshot_id": source_hash,
            "first_ts_event_ns": HOUR_NS,
            "last_ts_event_ns": 10 * HOUR_NS,
            "row_count": 10,
            "sha256": source_hash,
        }
        candidate_path = self.root / "changing-candidate.json"
        candidate_path.write_bytes(_canonical(candidate))
        modeled_path = self._write_funding("changing-modeled-funding", modeled_first=True)

        result = run_candidate_backtest(
            replace(
                self.request,
                candidate_path=candidate_path,
                catalog_path=catalog_path,
                funding_path=modeled_path,
                policy_path=self._policy("1970-01-01T00:00:00Z"),
            ),
        )

        self.assertEqual(result.verdict["funding"]["events"][0]["mark_price"], "2000.00")

    def test_bounded_replay_excludes_first_bar_funding_while_flat(self) -> None:
        modeled_path = self._write_funding("bounded-modeled-funding", modeled_first=True)

        result = run_candidate_backtest(
            replace(
                self.request,
                funding_path=modeled_path,
                evaluation_start_utc="1970-01-01T06:00:00Z",
                evaluation_end_utc="1970-01-01T10:00:00Z",
                data_as_of_ns=10 * HOUR_NS,
                evaluation_context_id="f" * 64,
                candidate_evaluation_context_id="e" * 64,
            ),
        )

        self.assertEqual(result.verdict["funding"]["events"], [])
        self.assertEqual(result.verdict["funding"]["total"], "0.00000000")
        self.assertEqual(
            result.verdict["evaluation_windows"]["actual_first_ts_event_ns"],
            6 * HOUR_NS,
        )
        self.assertEqual(result.verdict["execution"]["fill_count"], 2)

    def test_technical_engine_error_raises_instead_of_returning_revise(self) -> None:
        with patch(
            "nautilus_quant.candidate_backtest.BacktestEngine.run",
            side_effect=RuntimeError("engine exploded"),
        ):
            with self.assertRaisesRegex(RuntimeError, "engine exploded"):
                run_candidate_backtest(self.request)

    def test_canonical_verdict_bytes_and_hashes_are_stable_and_never_promote(self) -> None:
        first = run_candidate_backtest(self.request)
        second = run_candidate_backtest(self.request)

        self.assertEqual(first.canonical_bytes, second.canonical_bytes)
        self.assertEqual(first.verdict_id, second.verdict_id)
        self.assertEqual(first.verdict["schema_version"], "nautilus-verdict-v1")
        self.assertEqual(first.verdict["status"], "EVALUATED")
        self.assertIn(first.verdict["decision"], {"REVISE", "RETAIN_FOR_RESEARCH"})
        self.assertNotEqual(first.verdict["decision"], "PROMOTE")
        self.assertFalse(first.verdict["performance_claimable"])
        self.assertEqual(first.verdict["execution"]["slippage_status"], "unmodeled")
        body = dict(first.verdict)
        claimed_hash = body.pop("canonical_result_hash")
        self.assertEqual(claimed_hash, sha256(_canonical(body)).hexdigest())
        self.assertEqual(first.verdict_id, sha256(first.canonical_bytes).hexdigest())
        self.assertEqual(first.canonical_bytes, _canonical(first.verdict))

    def test_verdict_binds_root_nautilus_and_python_versions(self) -> None:
        result = run_candidate_backtest(self.request)

        versions = result.verdict["runtime_versions"]
        self.assertEqual(set(versions), {"nautilus_trader", "nautilus_python"})
        self.assertEqual(versions["nautilus_trader"], nautilus_trader.__version__)
        self.assertEqual(versions["nautilus_python"], platform.python_version())

    def test_verdict_loader_rejects_an_economically_impossible_balance_delta(self) -> None:
        result = run_candidate_backtest(replace(self.request, code_commit="a" * 40))
        invalid = dict(result.verdict)
        invalid["ending_balance"] = "1.00000000"
        invalid.pop("canonical_result_hash")
        invalid["canonical_result_hash"] = sha256(_canonical(invalid)).hexdigest()

        with self.assertRaisesRegex(RuntimeError, "balance delta"):
            load_candidate_backtest_verdict(_canonical(invalid))

    def test_verdict_loader_rejects_claimable_modeled_slippage(self) -> None:
        result = run_candidate_backtest(replace(self.request, code_commit="a" * 40))
        invalid = json.loads(json.dumps(result.verdict))
        invalid["execution"]["slippage_status"] = "modeled"
        invalid["performance_claimable"] = True
        invalid.pop("canonical_result_hash")
        invalid["canonical_result_hash"] = sha256(_canonical(invalid)).hexdigest()

        with self.assertRaisesRegex(RuntimeError, "slippage"):
            load_candidate_backtest_verdict(_canonical(invalid))

    def test_verdict_loader_rejects_forged_nautilus_runtime_version(self) -> None:
        result = run_candidate_backtest(replace(self.request, code_commit="a" * 40))
        invalid = json.loads(json.dumps(result.verdict))
        invalid["runtime_versions"]["nautilus_trader"] = "forged-not-installed"
        invalid.pop("canonical_result_hash")
        invalid["canonical_result_hash"] = sha256(_canonical(invalid)).hexdigest()

        with self.assertRaisesRegex(RuntimeError, "runtime versions"):
            load_candidate_backtest_verdict(_canonical(invalid))

    def test_verdict_loader_rejects_frozen_execution_policy_drift(self) -> None:
        result = run_candidate_backtest(replace(self.request, code_commit="a" * 40))
        mutations = (
            (None, "policy_decision_version", "forged-policy"),
            ("execution", "signal_timing", "same-bar"),
            ("execution", "fixed_quantity_btc", "0.002"),
        )
        for parent, field, value in mutations:
            with self.subTest(field=field):
                invalid = json.loads(json.dumps(result.verdict))
                target = invalid if parent is None else invalid[parent]
                target[field] = value
                invalid.pop("canonical_result_hash")
                invalid["canonical_result_hash"] = sha256(_canonical(invalid)).hexdigest()

                with self.assertRaisesRegex(RuntimeError, "policy|terminal"):
                    load_candidate_backtest_verdict(_canonical(invalid))

    def test_verdict_loader_rejects_fill_quantity_and_next_event_timing_drift(self) -> None:
        result = run_candidate_backtest(replace(self.request, code_commit="a" * 40))
        for field, value in (
            ("quantity", "0.002"),
            ("action_ts_event_ns", result.verdict["execution"]["fills"][0]["source_signal_ts_event_ns"]),
        ):
            with self.subTest(field=field):
                invalid = json.loads(json.dumps(result.verdict))
                invalid["execution"]["fills"][0][field] = value
                invalid.pop("canonical_result_hash")
                invalid["canonical_result_hash"] = sha256(_canonical(invalid)).hexdigest()

                with self.assertRaisesRegex(RuntimeError, "quantity|timing"):
                    load_candidate_backtest_verdict(_canonical(invalid))

    def test_tracked_cost_policy_is_bound_to_a_formal_replay(self) -> None:
        request = replace(
            self.request,
            code_commit="a" * 40,
            evaluation_start_utc="1970-01-01T00:00:00Z",
            evaluation_end_utc="1970-01-01T10:00:00Z",
            data_as_of_ns=10 * HOUR_NS,
            evaluation_context_id="e" * 64,
            fee_multiplier=Decimal("2"),
            funding_multiplier=Decimal("2"),
            delay_bars=1,
            slippage_model="one_tick",
            cost_policy_id="f" * 64,
        )
        result = run_candidate_backtest(request)
        verdict = load_candidate_backtest_verdict(result.canonical_bytes)
        self.assertEqual(verdict["cost_policy"], {
            "cost_policy_id": "f" * 64,
            "delay_bars": 1,
            "fee_multiplier": "2",
            "fee_source": "nautilus_instrument_metadata",
            "funding_multiplier": "2",
            "funding_source": "canonical_funding_observation_v1",
            "schema_version": "nautilus-cost-policy-v1",
            "slippage_model": "one_tick",
        })

    def test_one_tick_slippage_is_executed_by_nautilus_not_only_labeled(self) -> None:
        base = replace(
            self.request,
            code_commit="a" * 40,
            evaluation_start_utc="1970-01-01T00:00:00Z",
            evaluation_end_utc="1970-01-01T10:00:00Z",
            data_as_of_ns=10 * HOUR_NS,
            evaluation_context_id="e" * 64,
        )

        unstressed = run_candidate_backtest(base).verdict
        stressed = run_candidate_backtest(
            replace(base, slippage_model="one_tick", cost_policy_id="f" * 64),
        ).verdict

        self.assertEqual(stressed["execution"]["slippage_status"], "modeled_one_tick")
        self.assertLess(
            Decimal(stressed["gross_trading_result"]),
            Decimal(unstressed["gross_trading_result"]),
        )

    def test_one_bar_delay_moves_each_changed_signal_one_additional_bar(self) -> None:
        result = run_candidate_backtest(
            replace(
                self.request,
                code_commit="a" * 40,
                evaluation_start_utc="1970-01-01T00:00:00Z",
                evaluation_end_utc="1970-01-01T10:00:00Z",
                data_as_of_ns=10 * HOUR_NS,
                evaluation_context_id="e" * 64,
                delay_bars=1,
                cost_policy_id="f" * 64,
            ),
        )

        self.assertEqual(
            [
                (fill["source_signal_ts_event_ns"], fill["action_ts_event_ns"])
                for fill in result.verdict["execution"]["fills"]
            ],
            [
                (3 * HOUR_NS, 5 * HOUR_NS),
                (4 * HOUR_NS, 6 * HOUR_NS),
                (7 * HOUR_NS, 9 * HOUR_NS),
                (8 * HOUR_NS, 10 * HOUR_NS),
            ],
        )


if __name__ == "__main__":
    unittest.main()
