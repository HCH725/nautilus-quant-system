# noqa: E501  # noqa: SIZE_OK — Task C keeps its fixture and acceptance points together.
from __future__ import annotations

from dataclasses import asdict, replace
from decimal import Decimal
from hashlib import sha256
import json
import platform
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

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
    run_signal_parity_gate,
)
from nautilus_quant.funding_observation import migrate_funding_observations
from nautilus_quant.nautilus_io import make_bar
from nautilus_quant.pybroker_candidate import load_pybroker_candidate
from nautilus_quant.strategy_families import (
    ClosedBar,
    KERNEL_HASH,
    KERNEL_VERSION,
    derive_signal_id,
    evaluate_batch,
)


ROOT = Path(__file__).resolve().parents[1]
INSTRUMENT_ID = "BTCUSDT-PERP.BINANCE"
BAR_TYPE = f"{INSTRUMENT_ID}-1-HOUR-LAST-EXTERNAL"
HOUR_MS = 60 * 60 * 1_000
HOUR_NS = HOUR_MS * 1_000_000
USDT = Currency.from_str("USDT")
BTC = Currency.from_str("BTC")
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
                "fundingTime": 4 * HOUR_MS,
                "fundingRate": "0.01",
                "markPrice": None if self.modeled_first else "1000",
            },
            {
                "symbol": symbol,
                "fundingTime": 12 * HOUR_MS,
                "fundingRate": "0.01",
                "markPrice": "1000",
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
                    open_="1000",
                    high="1000",
                    low="1000",
                    close="1000",
                    volume="10",
                    close_ms=hour * HOUR_MS,
                )
                for hour in range(1, 11)
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
            start_ms=4 * HOUR_MS,
            end_ms=13 * HOUR_MS,
        )
        return path

    def _candidate(self) -> dict[str, JsonValue]:
        return {
            "bar_type": BAR_TYPE,
            "instrument_id": INSTRUMENT_ID,
            "runtime": {
                "pybroker_version": "1.2.14",
                "python_version": "3.12.13",
                "seed": 42,
            },
            "schema_version": "pybroker-candidate-v1",
            "signals": [
                {"intent": "LONG", "score": 0.1, "ts_event_ns": 1 * HOUR_NS},
                {"intent": "LONG", "score": 0.2, "ts_event_ns": 2 * HOUR_NS},
                {"intent": "FLAT", "score": -0.1, "ts_event_ns": 4 * HOUR_NS},
                {"intent": "FLAT", "score": -0.2, "ts_event_ns": 5 * HOUR_NS},
                {"intent": "LONG", "score": 0.1, "ts_event_ns": 7 * HOUR_NS},
            ],
            "source": {
                "first_ts_event_ns": 1 * HOUR_NS,
                "last_ts_event_ns": 10 * HOUR_NS,
                "row_count": 10,
                "sha256": _catalog_digest(self.catalog_path),
            },
            "strategy": {
                "decision_timing": "bar-close; effective no earlier than next event",
                "name": "lookback-momentum-long-flat",
                "parameters": {"entry_threshold": 0.0, "lookback_bars": 2},
            },
            "truth_status": "provisional",
        }

    def _candidate_v2(self) -> dict[str, JsonValue]:
        parameters: dict[str, JsonValue] = {
            "entry_threshold": 0.0,
            "lookback_bars": 2,
        }
        bars = [
            ClosedBar(
                ts_event_ns=hour * HOUR_NS,
                open=1000,
                high=1000,
                low=1000,
                close=1000,
                volume=10,
            )
            for hour in range(1, 11)
        ]
        decisions = evaluate_batch(
            family_id="lookback-momentum-long-flat",
            family_version="lookback-momentum-long-flat-v1",
            parameters=parameters,
            bars=bars,
        )
        source_hash = _catalog_digest(self.catalog_path)
        return {
            "bar_type": BAR_TYPE,
            "evaluation_context_id": "e" * 64,
            "instrument_id": INSTRUMENT_ID,
            "runtime": {
                "environment_id": "d" * 64,
                "pybroker_version": "1.2.14",
                "python_version": "3.12.13",
                "seed": 42,
            },
            "schema_version": "pybroker-candidate-v2",
            "signals": [asdict(item) for item in decisions],
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
                "parameters": parameters,
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

    def test_formal_loader_is_called_and_actual_source_identity_fails_closed(self) -> None:
        with patch(
            "nautilus_quant.candidate_backtest.load_pybroker_candidate",
            wraps=load_pybroker_candidate,
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
                candidate["source"] = {**candidate["source"], field: value}
                self._write_candidate(candidate)
                with self.assertRaisesRegex(RuntimeError, f"source {field} mismatch"):
                    run_candidate_backtest(self.request)

    def test_real_engine_acts_on_next_event_dedupes_and_flattens_boundary(self) -> None:
        result = run_candidate_backtest(self.request)

        execution = result.verdict["execution"]
        fills = execution["fills"]
        self.assertEqual(execution["deduped_signal_count"], 2)
        self.assertEqual(execution["boundary_flattened"], True)
        self.assertEqual(execution["order_count"], 4)
        self.assertEqual(execution["fill_count"], 4)
        self.assertEqual(execution["trade_count"], 2)
        self.assertEqual(
            [
                (fill["source_signal_ts_event_ns"], fill["action_ts_event_ns"])
                for fill in fills
            ],
            [
                (1 * HOUR_NS, 2 * HOUR_NS),
                (4 * HOUR_NS, 5 * HOUR_NS),
                (7 * HOUR_NS, 8 * HOUR_NS),
                (None, 10 * HOUR_NS),
            ],
        )
        self.assertTrue(
            all(
                fill["source_signal_ts_event_ns"] < fill["action_ts_event_ns"]
                for fill in fills
                if fill["source_signal_ts_event_ns"] is not None
            ),
        )
        self.assertTrue(all(fill["quantity"] == "0.001" for fill in fills))
        self.assertEqual(result.verdict["ending_position"], "FLAT")
        self.assertEqual(result.verdict["open_position_count"], 0)

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
        candidate = self._candidate()
        candidate["signals"] = fixture["signals"]
        candidate["source"] = {
            "first_ts_event_ns": fixture["bars"][0]["ts_event_ns"],
            "last_ts_event_ns": fixture["bars"][-1]["ts_event_ns"],
            "row_count": len(fixture["bars"]),
            "sha256": _catalog_digest(catalog_path),
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

        result = run_candidate_backtest(
            replace(
                self.request,
                candidate_path=candidate_path,
                catalog_path=catalog_path,
                funding_path=funding_path,
            ),
        )

        fills = result.verdict["execution"]["fills"]
        first_signal_ns = fixture["signals"][0]["ts_event_ns"]
        expected_action_ns = next(
            bar["ts_event_ns"]
            for bar in fixture["bars"]
            if bar["ts_event_ns"] > first_signal_ns
        )
        self.assertEqual(fills[0]["source_signal_ts_event_ns"], first_signal_ns)
        self.assertEqual(fills[0]["action_ts_event_ns"], expected_action_ns)
        self.assertEqual(fills[0]["fill_ts_event_ns"], expected_action_ns)

    def test_nautilus_fees_funding_and_account_delta_reconcile(self) -> None:
        result = run_candidate_backtest(self.request)

        verdict = result.verdict
        self.assertEqual(verdict["fees"]["source"], "nautilus_instrument_metadata")
        self.assertEqual(verdict["fees"]["taker_rate"], "0.001")
        self.assertEqual(verdict["fees"]["total"], "-0.00400000")
        self.assertEqual(verdict["funding"]["total"], "-0.01000000")
        self.assertEqual(verdict["gross_trading_result"], "0.00000000")
        self.assertEqual(verdict["net_account_delta"], "-0.01400000")
        self.assertEqual(verdict["starting_balance"], "10000.00000000")
        self.assertEqual(verdict["ending_balance"], "9999.98600000")
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
                    "ts_event_ns": 4 * HOUR_NS,
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
        self.assertEqual(funding["events"][0]["mark_price"], "1000.00")
        self.assertFalse(result.verdict["performance_claimable"])

    def test_modeled_funding_fallback_cannot_look_at_the_same_timestamp_bar(self) -> None:
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
                    open_=str(2000 if hour == 4 else 1000),
                    high=str(2000 if hour == 4 else 1000),
                    low=str(2000 if hour == 4 else 1000),
                    close=str(2000 if hour == 4 else 1000),
                    volume="10",
                    close_ms=hour * HOUR_MS,
                )
                for hour in range(1, 11)
            ],
        )
        candidate = self._candidate()
        candidate["source"] = {
            **candidate["source"],
            "sha256": _catalog_digest(catalog_path),
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

        self.assertEqual(result.verdict["funding"]["events"][0]["mark_price"], "1000.00")

    def test_bounded_replay_excludes_first_bar_funding_while_flat(self) -> None:
        candidate = self._candidate_v2()
        self._write_candidate(candidate)
        parity = run_signal_parity_gate(self.candidate_path, self.catalog_path)
        modeled_path = self._write_funding("bounded-modeled-funding", modeled_first=True)

        result = run_candidate_backtest(
            replace(
                self.request,
                funding_path=modeled_path,
                signal_parity=parity,
                evaluation_start_utc="1970-01-01T04:00:00Z",
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
            4 * HOUR_NS,
        )

    def test_v2_parity_pass_recomputes_signals_before_nautilus_accounting(self) -> None:
        candidate = self._candidate_v2()
        self._write_candidate(candidate)

        parity = run_signal_parity_gate(self.candidate_path, self.catalog_path)
        result = run_candidate_backtest(replace(self.request, signal_parity=parity))

        self.assertEqual(parity.outcome, "PASS")
        self.assertEqual(parity.reason_code, "SIGNAL_PARITY_MATCH")
        self.assertIsNone(parity.required_action)
        self.assertEqual(
            [asdict(item) for item in parity.decisions],
            candidate["signals"],
        )
        self.assertEqual(result.verdict["candidate_id"], parity.candidate_id)
        self.assertEqual(
            result.verdict["signal_parity"],
            {
                "artifact_sha256": parity.artifact_sha256,
                "outcome": "PASS",
                "reason_code": "SIGNAL_PARITY_MATCH",
            },
        )

    def test_v2_rejects_forged_pass_decisions_before_engine_construction(self) -> None:
        self._write_candidate(self._candidate_v2())
        parity = run_signal_parity_gate(self.candidate_path, self.catalog_path)
        first = parity.decisions[0]
        forged = replace(
            parity,
            decisions=(
                replace(
                    first,
                    target_intent="FLAT" if first.target_intent == "LONG" else "LONG",
                ),
                *parity.decisions[1:],
            ),
        )

        with patch("nautilus_quant.candidate_backtest.BacktestEngine") as engine:
            with self.assertRaisesRegex(RuntimeError, "signal parity artifact content mismatch"):
                run_candidate_backtest(replace(self.request, signal_parity=forged))

        engine.assert_not_called()

    def test_v2_parity_mismatch_is_fix_technical_and_engine_never_starts(self) -> None:
        candidate = self._candidate_v2()
        strategy = candidate["strategy"]
        signals = candidate["signals"]
        self.assertIsInstance(strategy, dict)
        self.assertIsInstance(signals, list)
        signal = signals[0]
        self.assertIsInstance(signal, dict)
        signal["score"] = "0.000000000001"
        signal["signal_id"] = derive_signal_id(
            family_id=strategy["family_id"],
            family_version=strategy["family_version"],
            kernel_hash=strategy["kernel_hash"],
            kernel_version=strategy["kernel_version"],
            parameters=strategy["parameters"],
            reason=signal["reason"],
            score=signal["score"],
            target_intent=signal["target_intent"],
            ts_event_ns=signal["ts_event_ns"],
        )
        self._write_candidate(candidate)

        parity = run_signal_parity_gate(self.candidate_path, self.catalog_path)
        with patch("nautilus_quant.candidate_backtest.BacktestEngine") as engine:
            with self.assertRaisesRegex(RuntimeError, "signal parity gate did not pass"):
                run_candidate_backtest(replace(self.request, signal_parity=parity))

        self.assertEqual(parity.outcome, "ERROR")
        self.assertEqual(parity.reason_code, "SIGNAL_PARITY_MISMATCH")
        self.assertEqual(parity.required_action, "FIX_TECHNICAL")
        self.assertEqual(parity.mismatch_index, 0)
        engine.assert_not_called()

    def test_v2_without_gate_pass_fails_before_engine_construction(self) -> None:
        self._write_candidate(self._candidate_v2())

        with patch("nautilus_quant.candidate_backtest.BacktestEngine") as engine:
            with self.assertRaisesRegex(RuntimeError, "requires a passed signal parity gate"):
                run_candidate_backtest(self.request)

        engine.assert_not_called()

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

    def test_v2_verdict_binds_research_and_root_python_separately(self) -> None:
        candidate = self._candidate_v2()
        self._write_candidate(candidate)
        parity = run_signal_parity_gate(self.candidate_path, self.catalog_path)

        result = run_candidate_backtest(replace(self.request, signal_parity=parity))

        versions = result.verdict["runtime_versions"]
        self.assertEqual(
            set(versions),
            {"nautilus_trader", "nautilus_python", "pybroker", "research_python"},
        )
        self.assertEqual(versions["research_python"], candidate["runtime"]["python_version"])
        self.assertEqual(versions["nautilus_python"], platform.python_version())
        self.assertNotEqual(versions["research_python"], versions["nautilus_python"])

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
                (1 * HOUR_NS, 3 * HOUR_NS),
                (4 * HOUR_NS, 6 * HOUR_NS),
                (7 * HOUR_NS, 9 * HOUR_NS),
                (None, 10 * HOUR_NS),
            ],
        )


if __name__ == "__main__":
    unittest.main()
