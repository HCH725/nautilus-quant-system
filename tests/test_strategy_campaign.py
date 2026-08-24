from __future__ import annotations

from collections.abc import Callable
from contextlib import closing, redirect_stdout
from hashlib import sha256
import json
from io import StringIO
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import nautilus_quant.strategy_lab as strategy_lab
import nautilus_quant.strategy_campaign as campaign
from nautilus_quant.candidate_backtest import CandidateBacktestResult, SignalParityResult
from nautilus_quant.pybroker_candidate import derive_signal_id
from nautilus_quant.strategy_families import (
    FamilyDecision,
    KERNEL_HASH,
    KERNEL_VERSION,
    canonical_decision_bytes,
)


def _canonical(value: object) -> bytes:
    return (json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n").encode()


def _hypothesis() -> dict[str, object]:
    return {
        "bar_type": "BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",
        "based_on_verdict_id": None,
        "falsification": "No activity or excessive provisional drawdown",
        "family_version": "lookback-momentum-long-flat-v1",
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "parameters": {"entry_threshold": 0.0, "lookback_bars": 2},
        "parent_strategy_id": None,
        "schema_version": "strategy-hypothesis-v2",
        "strategy_family": "lookback-momentum-long-flat",
        "thesis": "Momentum persists into the next event",
    }


def _candidate_process(
    metrics: dict[str, float | int],
    *,
    candidate_family_id: str = "lookback-momentum-long-flat",
    candidate_family_version: str = "lookback-momentum-long-flat-v1",
    candidate_parameters: dict[str, object] | None = None,
    candidate_signal_count: int | None = None,
    candidate_environment_id: str | None = None,
    source_data_snapshot_id: str = "a" * 64,
    source_last_timestamp: int | None = None,
) -> Callable[..., strategy_lab._ProcessResult]:
    parameters = candidate_parameters or {"entry_threshold": 0.0, "lookback_bars": 2}

    def research_process(argv: list[str], **_kwargs: object) -> strategy_lab._ProcessResult:
        context = argv[argv.index("--evaluation-context-id") + 1]
        environment = (
            argv[argv.index("--environment-id") + 1]
            if candidate_environment_id is None
            else candidate_environment_id
        )
        signal_count = (
            int(metrics["signal_count"])
            if candidate_signal_count is None
            else candidate_signal_count
        )
        signals = []
        for index in range(signal_count):
            reason = f"test-signal-{index}"
            score = "1" if index % 2 == 0 else "-1"
            target_intent = "LONG" if index % 2 == 0 else "FLAT"
            timestamp = index + 1
            signals.append(
                {
                    "family_id": candidate_family_id,
                    "family_version": candidate_family_version,
                    "kernel_hash": KERNEL_HASH,
                    "kernel_version": KERNEL_VERSION,
                    "reason": reason,
                    "score": score,
                    "signal_id": derive_signal_id(
                        family_id=candidate_family_id,
                        family_version=candidate_family_version,
                        kernel_hash=KERNEL_HASH,
                        kernel_version=KERNEL_VERSION,
                        parameters=parameters,
                        reason=reason,
                        score=score,
                        target_intent=target_intent,
                        ts_event_ns=timestamp,
                    ),
                    "target_intent": target_intent,
                    "ts_event_ns": timestamp,
                },
            )
        last_timestamp = (
            max(1, signal_count)
            if source_last_timestamp is None
            else source_last_timestamp
        )
        candidate = {
            "bar_type": "BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",
            "evaluation_context_id": context,
            "instrument_id": "BTCUSDT-PERP.BINANCE",
            "runtime": {
                "environment_id": environment,
                "pybroker_version": "1.2.14",
                "python_version": "3.13.0",
                "seed": 42,
            },
            "schema_version": "pybroker-candidate-v2",
            "signals": signals,
            "source": {
                "data_as_of_ns": last_timestamp,
                "data_snapshot_id": source_data_snapshot_id,
                "first_ts_event_ns": 1,
                "last_ts_event_ns": last_timestamp,
                "row_count": last_timestamp,
                "sha256": source_data_snapshot_id,
            },
            "strategy": {
                "decision_timing": "bar-close; effective no earlier than next event",
                "family_id": candidate_family_id,
                "family_version": candidate_family_version,
                "kernel_hash": KERNEL_HASH,
                "kernel_version": KERNEL_VERSION,
                "parameters": parameters,
            },
            "truth_status": "provisional",
        }
        candidate_bytes = _canonical(candidate)
        output = Path(argv[argv.index("--output") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(candidate_bytes)
        result = {
            "candidate_id": sha256(candidate_bytes).hexdigest(),
            "provisional_metrics": metrics,
            "schema_version": "research-result-v2",
            "truth_status": "provisional",
        }
        return strategy_lab._ProcessResult(0, _canonical(result), b"", False, False, False)

    return research_process


def _parity_for_candidate(candidate_path: Path, _catalog_path: Path) -> SignalParityResult:
    candidate_id = sha256(candidate_path.read_bytes()).hexdigest()
    candidate = json.loads(candidate_path.read_bytes())
    signal_count = len(candidate["signals"])
    decisions = tuple(FamilyDecision(**row) for row in candidate["signals"])
    decision_payload = b"".join(canonical_decision_bytes(item) for item in decisions)
    payload = _canonical(
        {
            "candidate_id": candidate_id,
            "candidate_signal_count": signal_count,
            "detail": None,
            "mismatch_index": None,
            "outcome": "PASS",
            "reason_code": "SIGNAL_PARITY_MATCH",
            "recomputed_signal_count": signal_count,
            "recomputed_signals_sha256": sha256(decision_payload).hexdigest(),
            "required_action": None,
            "schema_version": "signal-parity-result-v1",
        },
    )
    return SignalParityResult(
        candidate_id=candidate_id,
        outcome="PASS",
        reason_code="SIGNAL_PARITY_MATCH",
        required_action=None,
        mismatch_index=None,
        decisions=decisions,
        canonical_bytes=payload,
        artifact_sha256=sha256(payload).hexdigest(),
    )


def _backtest_for_request(request: object) -> CandidateBacktestResult:
    candidate_path = getattr(request, "candidate_path")
    candidate = json.loads(Path(candidate_path).read_bytes())
    candidate_id = sha256(Path(candidate_path).read_bytes()).hexdigest()
    source = candidate["source"]
    runtime = candidate["runtime"]
    parity = getattr(request, "signal_parity")
    verdict = {
        "accounting_reconciled": True,
        "candidate_id": candidate_id,
        "code_commit": getattr(request, "code_commit"),
        "decision": "REVISE",
        "ending_balance": "1000000.00000000",
        "ending_position": "FLAT",
        "evaluation_windows": {
            "actual_first_ts_event_ns": source["first_ts_event_ns"],
            "actual_last_ts_event_ns": source["last_ts_event_ns"],
            "configured_historical_start": "2022-01-01T00:00:00Z",
            "first_official_funding_ns": None,
        },
        "execution": {
            "boundary_flattened": True,
            "deduped_signal_count": 0,
            "fill_count": 0,
            "fills": [],
            "fixed_quantity_btc": "0.001",
            "order_count": 0,
            "signal_timing": "bar-close; effective no earlier than next event",
            "slippage_status": "unmodeled",
            "trade_count": 0,
        },
        "experiment_id": getattr(request, "experiment_id"),
        "fees": {
            "maker_rate": "0.0002",
            "source": "nautilus_instrument_metadata",
            "taker_rate": "0.0004",
            "total": "0.00000000",
        },
        "funding": {
            "events": [],
            "same_timestamp_order": "mark_then_funding",
            "source": "canonical_funding_observation_v1",
            "total": "0.00000000",
            "truth_counts": {"missing_mark": 0, "modeled_funding": 0, "official": 0},
            "truth_status": "missing",
        },
        "gross_trading_result": "0.00000000",
        "hypothesis_id": getattr(request, "hypothesis_id"),
        "net_account_delta": "0.00000000",
        "open_position_count": 0,
        "performance_claimable": False,
        "policy_decision_version": "strategy-loop-decision-v1",
        "realized_balance_drawdown": "0.00000000",
        "reason_codes": [
            "NON_POSITIVE_NET_RESULT",
            "UNMODELED_SLIPPAGE",
            "FUNDING_TRUTH_MISSING",
        ],
        "runtime_versions": {
            "nautilus_trader": getattr(strategy_lab.nautilus_trader, "__version__"),
            "nautilus_python": sys.version.split()[0],
            "pybroker": runtime["pybroker_version"],
            "research_python": runtime["python_version"],
        },
        "schema_version": "nautilus-verdict-v1",
        "signal_parity": {
            "artifact_sha256": parity.artifact_sha256,
            "outcome": parity.outcome,
            "reason_code": parity.reason_code,
        },
        "source": {
            "first_ts_event_ns": source["first_ts_event_ns"],
            "last_ts_event_ns": source["last_ts_event_ns"],
            "row_count": source["row_count"],
            "sha256": source["sha256"],
        },
        "starting_balance": "1000000.00000000",
        "status": "EVALUATED",
        "strategy_id": getattr(request, "strategy_id"),
    }
    verdict["canonical_result_hash"] = sha256(_canonical(verdict)).hexdigest()
    verdict_bytes = _canonical(verdict)
    return CandidateBacktestResult(
        verdict=verdict,
        canonical_bytes=verdict_bytes,
        verdict_id=sha256(verdict_bytes).hexdigest(),
    )


def _test_screen_policy() -> campaign.ScreenPolicy:
    document = {
        "max_provisional_drawdown": 0.5,
        "max_turnover": 10.0,
        "minimum_signal_count": 1,
        "minimum_trade_count": 1,
        "policy_version": "test-screen-v1",
        "reject_no_signal": True,
        "schema_version": "strategy-research-policy-v1",
    }
    return campaign.ScreenPolicy(
        sha256(_canonical(document)).hexdigest(),
        "test-screen-v1",
        1,
        1,
        0.5,
        10.0,
        True,
    )


def _persisted_terminal_fixture(
    root: Path,
    ledger: strategy_lab.StrategyLedger,
    *,
    family_id: str = "lookback-momentum-long-flat",
    family_version: str = "lookback-momentum-long-flat-v1",
    search_space: dict[str, tuple[campaign.JsonValue, ...]] | None = None,
    survived: bool = False,
    experiment_data_source_id: str = "a" * 64,
    experiment_policy_id: str | None = None,
    experiment_base_engine_id: str = "engine-v2",
    experiment_runtime_id: str = "d" * 64,
    experiment_data_as_of_ns: int = 1,
    candidate_data_snapshot_id: str = "a" * 64,
    candidate_data_as_of_ns: int = 1,
    campaign_data_source_id: str = "a" * 64,
    campaign_data_as_of_ns: int = 1,
) -> tuple[
    campaign.CampaignAttempt,
    campaign.TrialEvidence,
    strategy_lab.VerdictRecord | None,
    str,
]:
    policy = _test_screen_policy()
    spec = campaign.CampaignSpec(
        family_id=family_id,
        family_version=family_version,
        search_space=search_space
        or {"entry_threshold": (0.0,), "lookback_bars": (2,)},
        approved_instruments=("BTCUSDT-PERP.BINANCE",),
        approved_bar_types=("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",),
        parameter_search_policy_id="f" * 64,
        seed=42,
        data_as_of_ns=campaign_data_as_of_ns,
        generation_budget=1,
        maximum_candidates=1,
        screen_policy_id=policy.policy_id,
    )
    attempt = campaign.expand_campaign(spec)[0]
    hypothesis_path = root / "hypothesis.json"
    hypothesis_path.write_bytes(
        _canonical(strategy_lab._campaign_hypothesis_document(spec, attempt)),
    )
    hypothesis = strategy_lab.load_strategy_hypothesis(hypothesis_path)
    ledger.record_hypothesis(hypothesis)

    correct_policy_id = sha256(
        _canonical(
            {
                "accounting_policy_id": "b" * 64,
                "screen_policy_id": policy.policy_id,
            },
        ),
    ).hexdigest()
    code_commit = "e" * 40
    candidate_context = strategy_lab._evaluation_context_id(
        hypothesis,
        data_source_id="a" * 64,
        data_snapshot_id="a" * 64,
        data_as_of_ns=experiment_data_as_of_ns,
        policy_id=correct_policy_id,
        engine_id="engine-v2",
        runtime_id="d" * 64,
        code_commit=code_commit,
        screen_policy_id=policy.policy_id,
    )
    identity = strategy_lab.ExperimentIdentity(
        hypothesis.strategy_id,
        experiment_data_source_id,
        experiment_policy_id or correct_policy_id,
        f"{experiment_base_engine_id}-evaluation-{candidate_context}",
        experiment_runtime_id,
        experiment_data_as_of_ns,
        "a" * 64,
    )
    experiment_id = ledger.record_experiment(hypothesis.hypothesis_id, identity)

    metrics: dict[str, float | int] = (
        {
            "trade_count": 1,
            "signal_count": 1,
            "total_return": 0.1,
            "max_drawdown": 0.1,
            "turnover": 1.0,
        }
        if survived
        else {
            "trade_count": 0,
            "signal_count": 0,
            "total_return": 0.0,
            "max_drawdown": 0.0,
            "turnover": 0.0,
        }
    )
    candidate_path = root / "candidate.json"
    completed = _candidate_process(
        metrics,
        source_data_snapshot_id=candidate_data_snapshot_id,
        source_last_timestamp=candidate_data_as_of_ns,
    )(
        [
            "python",
            "research.py",
            "--evaluation-context-id",
            candidate_context,
            "--environment-id",
            "d" * 64,
            "--output",
            str(candidate_path),
        ],
    )
    raw_path = root / "research-result-v2-raw.json"
    raw_path.write_bytes(completed.stdout)
    ledger.record_stage(
        strategy_lab.StageRecord(
            experiment_id,
            "PyBroker completed",
            "PASSED",
            "PYBROKER_COMPLETED",
            str(raw_path),
            sha256(raw_path.read_bytes()).hexdigest(),
        ),
    )
    result = campaign.load_research_result_v2(completed.stdout)
    decision = campaign.screen_research_result(result, policy)
    screen_path = root / "research-screen-result-v1.json"
    screen_path.write_bytes(
        _canonical(campaign.screened_result_document(result, decision, policy)),
    )
    ledger.record_stage(
        strategy_lab.StageRecord(
            experiment_id,
            "Research screened",
            "PASSED" if survived else "REJECTED",
            "SCREEN_PASSED" if survived else decision.reason_codes[0],
            str(screen_path),
            sha256(screen_path.read_bytes()).hexdigest(),
        ),
    )
    ledger.record_campaign(
        spec,
        campaign.CampaignPreflight(
            policy.policy_id,
            campaign_data_as_of_ns,
            campaign_data_source_id,
        ),
    )

    if not survived:
        return (
            attempt,
            campaign.TrialEvidence(
                campaign.TerminalStatus.SCREEN_REJECTED,
                True,
                decision.reason_codes,
                experiment_id,
                result.candidate_id,
            ),
            None,
            code_commit,
        )

    parity = _parity_for_candidate(candidate_path, root / "catalog")
    parity_path = root / "signal-parity-result.json"
    parity_path.write_bytes(parity.canonical_bytes)
    ledger.record_signal_parity(
        strategy_lab.SignalParityRecord(
            experiment_id,
            parity.candidate_id,
            candidate_context,
            candidate_data_snapshot_id,
            "PASS",
            "SIGNAL_PARITY_MATCH",
            None,
            str(parity_path),
            parity.artifact_sha256,
            parity.decisions,
        ),
    )
    backtest = _backtest_for_request(
        SimpleNamespace(
            candidate_path=candidate_path,
            code_commit=code_commit,
            experiment_id=experiment_id,
            hypothesis_id=hypothesis.hypothesis_id,
            signal_parity=parity,
            strategy_id=hypothesis.strategy_id,
        ),
    )
    verdict_path = root / "nautilus-verdict.json"
    verdict_path.write_bytes(backtest.canonical_bytes)
    ledger.record_stage(
        strategy_lab.StageRecord(
            experiment_id,
            "Nautilus replayed",
            "PASSED",
            "NAUTILUS_REPLAY_COMPLETED",
            str(verdict_path),
            backtest.verdict_id,
        ),
    )
    reason_codes = backtest.verdict["reason_codes"]
    assert isinstance(reason_codes, list) and isinstance(reason_codes[0], str)
    return (
        attempt,
        campaign.TrialEvidence(
            campaign.TerminalStatus.SURVIVED,
            True,
            ("SCREEN_PASSED",),
            experiment_id,
            result.candidate_id,
        ),
        strategy_lab.VerdictRecord(
            experiment_id,
            "REJECTION",
            reason_codes[0],
            str(verdict_path),
            backtest.verdict_id,
        ),
        code_commit,
    )


class StrategyCampaignScreenTests(unittest.TestCase):
    @staticmethod
    def _spec(
        *,
        thresholds: tuple[float, ...] = (0.0,),
        screen_policy_id: str = "e" * 64,
    ) -> campaign.CampaignSpec:
        return campaign.CampaignSpec(
            family_id="close-vs-sma-mean-reversion-long-flat",
            family_version="close-vs-sma-mean-reversion-long-flat-v1",
            search_space={"discount_threshold": thresholds, "window_bars": (2,)},
            approved_instruments=("BTCUSDT-PERP.BINANCE",),
            approved_bar_types=("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",),
            parameter_search_policy_id="f" * 64,
            seed=42,
            data_as_of_ns=123,
            generation_budget=len(thresholds),
            maximum_candidates=len(thresholds),
            screen_policy_id=screen_policy_id,
        )

    @staticmethod
    def _insert_stub_experiment(
        ledger: strategy_lab.StrategyLedger,
        attempt: campaign.CampaignAttempt,
        suffix: str,
        *,
        identity_schema: str = "strategy-id-v2",
    ) -> tuple[str, str]:
        assert attempt.strategy_id is not None
        family_id = (
            "lookback-momentum-long-flat"
            if "lookback_bars" in attempt.parameters
            else "close-vs-sma-mean-reversion-long-flat"
        )
        hypothesis_id = sha256(
            f"hypothesis-{suffix}-{attempt.strategy_id}".encode(),
        ).hexdigest()
        experiment_id = sha256(
            f"experiment-{suffix}-{attempt.strategy_id}".encode(),
        ).hexdigest()
        candidate_id = sha256(
            f"candidate-{suffix}-{attempt.strategy_id}".encode(),
        ).hexdigest()
        with closing(sqlite3.connect(ledger.path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                "INSERT OR IGNORE INTO strategies VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    attempt.strategy_id,
                    family_id,
                    f"{family_id}-v1",
                    _canonical(attempt.parameters).decode(),
                    "BTCUSDT-PERP.BINANCE",
                    "BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",
                    identity_schema,
                ),
            )
            connection.execute(
                "INSERT OR IGNORE INTO hypotheses VALUES (?, ?, NULL, NULL, ?, ?)",
                (hypothesis_id, attempt.strategy_id, f"/test/{suffix}.json", hypothesis_id),
            )
            connection.execute(
                "INSERT INTO experiments VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    experiment_id,
                    hypothesis_id,
                    attempt.strategy_id,
                    sha256(f"data-{suffix}".encode()).hexdigest(),
                    sha256(f"policy-{suffix}".encode()).hexdigest(),
                    sha256(f"engine-{suffix}".encode()).hexdigest(),
                    sha256(f"runtime-{suffix}".encode()).hexdigest(),
                ),
            )
        return experiment_id, candidate_id

    def test_campaign_cli_executes_real_campaign_and_emits_one_terminal_summary(self) -> None:
        screen_policy_id = campaign.load_screen_policy(
            Path("config/strategy_research_policy.json"),
        ).policy_id
        spec = campaign.CampaignSpec(
            family_id="lookback-momentum-long-flat",
            family_version="lookback-momentum-long-flat-v1",
            search_space={"entry_threshold": (0.0,), "lookback_bars": (2,)},
            approved_instruments=("BTCUSDT-PERP.BINANCE",),
            approved_bar_types=("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",),
            parameter_search_policy_id="f" * 64,
            seed=42,
            data_as_of_ns=123,
            generation_budget=1,
            maximum_candidates=1,
            screen_policy_id=screen_policy_id,
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec_path = Path(temporary) / "campaign.json"
            spec_path.write_bytes(_canonical(spec.document))
            paths = strategy_lab.StrategyLoopPaths(
                market_data_path=root / "market-data.json",
                policy_path=Path("config/strategy_loop_policy.json"),
                catalog_path=root / "catalog",
                funding_path=root / "funding",
                state_path=root / "state",
                research_policy_path=Path("config/strategy_research_policy.json"),
            )
            stdout = StringIO()
            with (
                patch.object(strategy_lab, "DEFAULT_LOOP_PATHS", paths),
                patch.object(strategy_lab, "_catalog_last_timestamp", return_value=123),
                patch.object(strategy_lab, "_catalog_digest", return_value="a" * 64),
                patch.object(strategy_lab, "_hash_tree", return_value="c" * 64),
                patch.object(strategy_lab, "_runtime_identity", return_value="d" * 64),
                patch.object(strategy_lab, "_engine_identity", return_value="engine-v2"),
                patch.object(strategy_lab, "_code_commit", return_value="e" * 40),
                patch.object(
                    strategy_lab,
                    "_run_bounded_process",
                    side_effect=_candidate_process(
                        {"trade_count": 1, "signal_count": 1, "total_return": 0.1, "max_drawdown": 0.1, "turnover": 1.0},
                        source_last_timestamp=123,
                    ),
                ),
                patch.object(strategy_lab, "validated_candidate_source_bars", return_value=[]),
                patch.object(strategy_lab, "run_signal_parity_gate", side_effect=_parity_for_candidate),
                patch.object(strategy_lab, "run_candidate_backtest", side_effect=_backtest_for_request),
                redirect_stdout(stdout),
            ):
                exit_code = strategy_lab.main(["campaign", "--spec", str(spec_path)])
            with closing(sqlite3.connect(paths.state_path / "ledger.sqlite3")) as connection:
                census = connection.execute(
                    """SELECT terminal_status, execution_started, COUNT(*)
                    FROM campaign_trials GROUP BY terminal_status, execution_started""",
                ).fetchall()
                experiment_count = connection.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]

        self.assertEqual(exit_code, 0)
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["schema_version"], "strategy-cohort-summary-v1")
        self.assertEqual(output["terminal_status_counts"]["SURVIVED"], 1)
        self.assertEqual(census, [("SURVIVED", 1, 1)])
        self.assertEqual(experiment_count, 1)

    def test_campaign_membership_is_persisted_in_the_main_strategy_ledger(self) -> None:
        spec = campaign.CampaignSpec(
            family_id="close-vs-sma-mean-reversion-long-flat",
            family_version="close-vs-sma-mean-reversion-long-flat-v1",
            search_space={"discount_threshold": (0.0,), "window_bars": (2,)},
            approved_instruments=("BTCUSDT-PERP.BINANCE",),
            approved_bar_types=("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",),
            parameter_search_policy_id="f" * 64,
            seed=42,
            data_as_of_ns=123,
            generation_budget=1,
            maximum_candidates=1,
            screen_policy_id="e" * 64,
        )
        with TemporaryDirectory() as temporary:
            ledger = strategy_lab.StrategyLedger(Path(temporary) / "ledger.sqlite3")
            ledger.initialize()
            summary = campaign.run_campaign(
                spec,
                ledger=ledger,
                preflight=campaign.CampaignPreflight("e" * 64, 123, "a" * 64),
                execute=lambda _attempt: campaign.TrialEvidence(
                    campaign.TerminalStatus.TECHNICAL_INVALID,
                    False,
                    ("TEST_TERMINAL",),
                ),
            )
            with closing(sqlite3.connect(ledger.path)) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'",
                    )
                }
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("UPDATE campaigns SET document_json = '{}'" )

        self.assertEqual(summary["trial_count"], 1)
        self.assertIn("campaigns", tables)
        self.assertIn("campaign_trials", tables)

    def test_campaign_trial_rejects_a_nonexistent_experiment_reference(self) -> None:
        spec = campaign.CampaignSpec(
            family_id="close-vs-sma-mean-reversion-long-flat",
            family_version="close-vs-sma-mean-reversion-long-flat-v1",
            search_space={"discount_threshold": (0.0,), "window_bars": (2,)},
            approved_instruments=("BTCUSDT-PERP.BINANCE",),
            approved_bar_types=("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",),
            parameter_search_policy_id="f" * 64,
            seed=42,
            data_as_of_ns=123,
            generation_budget=1,
            maximum_candidates=1,
            screen_policy_id="e" * 64,
        )
        with TemporaryDirectory() as temporary:
            ledger = strategy_lab.StrategyLedger(Path(temporary) / "ledger.sqlite3")
            ledger.initialize()
            ledger.record_campaign(
                spec,
                campaign.CampaignPreflight("e" * 64, 123, "a" * 64),
            )
            attempt = campaign.expand_campaign(spec)[0]

            with self.assertRaises(sqlite3.IntegrityError):
                ledger.record_campaign_trial(
                    attempt,
                    campaign.TrialEvidence(
                        campaign.TerminalStatus.SURVIVED,
                        True,
                        ("SCREEN_PASSED",),
                        "a" * 64,
                        "b" * 64,
                    ),
                )

    def test_campaign_trial_rejects_survival_without_a_valid_v2_terminal_chain(self) -> None:
        spec = self._spec()
        with TemporaryDirectory() as temporary:
            ledger = strategy_lab.StrategyLedger(Path(temporary) / "ledger.sqlite3")
            ledger.initialize()
            ledger.record_campaign(
                spec,
                campaign.CampaignPreflight("e" * 64, 123, "a" * 64),
            )
            attempt = campaign.expand_campaign(spec)[0]
            experiment_id, candidate_id = self._insert_stub_experiment(
                ledger,
                attempt,
                "survived-without-chain",
            )

            with self.assertRaisesRegex(ValueError, "terminal chain"):
                ledger.record_campaign_trial(
                    attempt,
                    campaign.TrialEvidence(
                        campaign.TerminalStatus.SURVIVED,
                        True,
                        ("SCREEN_PASSED",),
                        experiment_id,
                        candidate_id,
                    ),
                )

    def test_campaign_trial_rejects_screen_rejection_without_a_valid_terminal_chain(self) -> None:
        spec = self._spec()
        with TemporaryDirectory() as temporary:
            ledger = strategy_lab.StrategyLedger(Path(temporary) / "ledger.sqlite3")
            ledger.initialize()
            ledger.record_campaign(
                spec,
                campaign.CampaignPreflight("e" * 64, 123, "a" * 64),
            )
            attempt = campaign.expand_campaign(spec)[0]
            experiment_id, candidate_id = self._insert_stub_experiment(
                ledger,
                attempt,
                "screen-rejected-without-chain",
            )

            with self.assertRaisesRegex(ValueError, "terminal chain"):
                ledger.record_campaign_trial(
                    attempt,
                    campaign.TrialEvidence(
                        campaign.TerminalStatus.SCREEN_REJECTED,
                        True,
                        ("NO_SIGNALS",),
                        experiment_id,
                        candidate_id,
                    ),
                )

    def test_record_verdict_rejects_candidate_from_another_persisted_hypothesis(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = strategy_lab.StrategyLedger(root / "ledger.sqlite3")
            ledger.initialize()
            _attempt, _evidence, record, _code_commit = _persisted_terminal_fixture(
                root,
                ledger,
                family_id="close-vs-sma-mean-reversion-long-flat",
                family_version="close-vs-sma-mean-reversion-long-flat-v1",
                search_space={"discount_threshold": (0.0,), "window_bars": (2,)},
                survived=True,
            )
            assert record is not None

            with self.assertRaisesRegex(ValueError, "immutable identity"):
                ledger.record_verdict(record)

    def test_record_verdict_rejects_candidate_from_another_source_snapshot_and_as_of(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = strategy_lab.StrategyLedger(root / "ledger.sqlite3")
            ledger.initialize()
            _attempt, _evidence, record, _code_commit = _persisted_terminal_fixture(
                root,
                ledger,
                survived=True,
                candidate_data_snapshot_id="2" * 64,
                candidate_data_as_of_ns=2,
            )
            assert record is not None
            with closing(sqlite3.connect(ledger.path)) as connection:
                persisted_source = connection.execute(
                    """SELECT data_snapshot_id, data_as_of_ns
                    FROM experiment_sources WHERE experiment_id = ?""",
                    (record.experiment_id,),
                ).fetchone()
            self.assertEqual(persisted_source, ("a" * 64, 1))

            with self.assertRaisesRegex(ValueError, "immutable identity"):
                ledger.record_verdict(record)
            with closing(sqlite3.connect(ledger.path)) as connection:
                verdict_count = connection.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0]
            self.assertEqual(verdict_count, 0)

    def test_existing_execution_rejects_screened_candidate_from_another_snapshot(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = strategy_lab.StrategyLedger(root / "ledger.sqlite3")
            ledger.initialize()
            _attempt, _evidence, _record, code_commit = _persisted_terminal_fixture(
                root,
                ledger,
                candidate_data_snapshot_id="2" * 64,
            )
            hypothesis = strategy_lab.load_strategy_hypothesis(root / "hypothesis.json")
            candidate, _candidate_id = strategy_lab.load_pybroker_candidate(
                root / "candidate.json",
            )
            evaluation_context_id = candidate["evaluation_context_id"]
            assert isinstance(evaluation_context_id, str)
            screen_policy = _test_screen_policy()
            policy_id = sha256(
                _canonical(
                    {
                        "accounting_policy_id": "b" * 64,
                        "screen_policy_id": screen_policy.policy_id,
                    },
                ),
            ).hexdigest()
            identity = strategy_lab.ExperimentIdentity(
                hypothesis.strategy_id,
                "a" * 64,
                policy_id,
                f"engine-v2-evaluation-{evaluation_context_id}",
                "d" * 64,
                1,
                "a" * 64,
            )
            prepared = strategy_lab._PreparedExecution(
                identity,
                evaluation_context_id,
                screen_policy,
                1,
                "d" * 64,
                code_commit,
                "engine-v2",
            )

            with self.assertRaisesRegex(ValueError, "immutable hypothesis"):
                ledger.existing_execution(
                    identity,
                    screen_policy,
                    hypothesis=hypothesis,
                    prepared=prepared,
                )

    def test_campaign_trial_rejects_cross_family_terminal_candidate(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = strategy_lab.StrategyLedger(root / "ledger.sqlite3")
            ledger.initialize()
            attempt, evidence, _record, code_commit = _persisted_terminal_fixture(
                root,
                ledger,
                family_id="close-vs-sma-mean-reversion-long-flat",
                family_version="close-vs-sma-mean-reversion-long-flat-v1",
                search_space={"discount_threshold": (0.0,), "window_bars": (2,)},
            )

            with (
                patch.object(strategy_lab, "_code_commit", return_value=code_commit),
                self.assertRaisesRegex(ValueError, "immutable identity"),
            ):
                ledger.record_campaign_trial(attempt, evidence)

    def test_campaign_trial_rejects_cross_data_or_as_of_terminal_candidate(self) -> None:
        def assert_rejected(
            *,
            experiment_data_source_id: str = "a" * 64,
            campaign_data_as_of_ns: int = 1,
        ) -> None:
            with TemporaryDirectory() as temporary:
                root = Path(temporary)
                ledger = strategy_lab.StrategyLedger(root / "ledger.sqlite3")
                ledger.initialize()
                attempt, evidence, _record, code_commit = _persisted_terminal_fixture(
                    root,
                    ledger,
                    experiment_data_source_id=experiment_data_source_id,
                    campaign_data_as_of_ns=campaign_data_as_of_ns,
                )

                with (
                    patch.object(strategy_lab, "_code_commit", return_value=code_commit),
                    self.assertRaisesRegex(ValueError, "immutable identity"),
                ):
                    ledger.record_campaign_trial(attempt, evidence)

        with self.subTest(identity="data_source"):
            assert_rejected(experiment_data_source_id="2" * 64)
        with self.subTest(identity="data_as_of"):
            assert_rejected(campaign_data_as_of_ns=2)

    def test_campaign_trial_rejects_cross_policy_engine_or_runtime_terminal_candidate(self) -> None:
        def assert_rejected(
            *,
            experiment_policy_id: str | None = None,
            experiment_base_engine_id: str = "engine-v2",
            experiment_runtime_id: str = "d" * 64,
        ) -> None:
            with TemporaryDirectory() as temporary:
                root = Path(temporary)
                ledger = strategy_lab.StrategyLedger(root / "ledger.sqlite3")
                ledger.initialize()
                attempt, evidence, _record, code_commit = _persisted_terminal_fixture(
                    root,
                    ledger,
                    experiment_policy_id=experiment_policy_id,
                    experiment_base_engine_id=experiment_base_engine_id,
                    experiment_runtime_id=experiment_runtime_id,
                )

                with (
                    patch.object(strategy_lab, "_code_commit", return_value=code_commit),
                    self.assertRaisesRegex(ValueError, "immutable identity"),
                ):
                    ledger.record_campaign_trial(attempt, evidence)

        with self.subTest(identity="policy"):
            assert_rejected(experiment_policy_id="3" * 64)
        with self.subTest(identity="engine"):
            assert_rejected(experiment_base_engine_id="other-engine")
        with self.subTest(identity="runtime"):
            assert_rejected(experiment_runtime_id="4" * 64)

    def _cached_screen_fixture(
        self,
        root: Path,
        ledger: strategy_lab.StrategyLedger,
        identity: strategy_lab.ExperimentIdentity,
        *,
        semantic_tamper: bool = False,
        metric_tamper: bool = False,
        candidate_signal_count: int | None = None,
    ) -> tuple[campaign.ScreenPolicy, Path, Path, str]:
        candidate_path = root / "candidate.json"
        completed = _candidate_process(
            {
                "trade_count": 0,
                "signal_count": 0,
                "total_return": 0.0,
                "max_drawdown": 0.0,
                "turnover": 0.0,
            },
            candidate_signal_count=candidate_signal_count,
        )(
            [
                "python",
                "research.py",
                "--evaluation-context-id",
                "e" * 64,
                "--environment-id",
                "d" * 64,
                "--output",
                str(candidate_path),
            ],
        )
        raw_path = root / "research-result-v2-raw.json"
        raw_path.write_bytes(completed.stdout)
        policy_document = {
            "max_provisional_drawdown": 0.5,
            "max_turnover": 10.0,
            "minimum_signal_count": 1,
            "minimum_trade_count": 1,
            "policy_version": "test-screen-v1",
            "reject_no_signal": True,
            "schema_version": "strategy-research-policy-v1",
        }
        policy = campaign.ScreenPolicy(
            policy_id=sha256(_canonical(policy_document)).hexdigest(),
            policy_version="test-screen-v1",
            minimum_trade_count=1,
            minimum_signal_count=1,
            max_provisional_drawdown=0.5,
            max_turnover=10.0,
            reject_no_signal=True,
        )
        parsed = campaign.load_research_result_v2(completed.stdout)
        decision = campaign.screen_research_result(parsed, policy)
        screen_document = campaign.screened_result_document(parsed, decision, policy)
        if semantic_tamper:
            screen_document["screen_outcome"] = "PASSED"
        if metric_tamper:
            metrics = screen_document["provisional_metrics"]
            assert isinstance(metrics, dict)
            metrics["total_return"] = 1.0
        screen_path = root / "research-screen-result-v1.json"
        screen_path.write_bytes(_canonical(screen_document))
        experiment_id = "5" * 64
        with closing(sqlite3.connect(ledger.path)) as connection, connection:
            connection.execute(
                "INSERT INTO experiments VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    experiment_id,
                    "7" * 64,
                    identity.strategy_id,
                    identity.data_source_id,
                    identity.policy_id,
                    identity.engine_id,
                    identity.runtime_id,
                ),
            )
            connection.execute(
                "INSERT INTO stage_results VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    experiment_id,
                    identity.strategy_id,
                    "PyBroker completed",
                    "PASSED",
                    "PYBROKER_COMPLETED",
                    str(raw_path),
                    sha256(raw_path.read_bytes()).hexdigest(),
                ),
            )
            connection.execute(
                "INSERT INTO stage_results VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    experiment_id,
                    identity.strategy_id,
                    "Research screened",
                    "REJECTED",
                    decision.reason_codes[0],
                    str(screen_path),
                    sha256(screen_path.read_bytes()).hexdigest(),
                ),
            )
        return policy, candidate_path, screen_path, parsed.candidate_id

    def test_campaign_trial_rejects_screen_metrics_that_differ_from_raw_research(self) -> None:
        base_attempt = campaign.expand_campaign(self._spec())[0]
        assert base_attempt.strategy_id is not None
        identity = strategy_lab.ExperimentIdentity(
            base_attempt.strategy_id,
            "2" * 64,
            "3" * 64,
            "engine-v2",
            "4" * 64,
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = strategy_lab.StrategyLedger(root / "ledger.sqlite3")
            ledger.initialize()
            policy, _candidate_path, _screen_path, candidate_id = self._cached_screen_fixture(
                root,
                ledger,
                identity,
                metric_tamper=True,
            )
            spec = self._spec(screen_policy_id=policy.policy_id)
            attempt = campaign.expand_campaign(spec)[0]
            ledger.record_campaign(
                spec,
                campaign.CampaignPreflight(policy.policy_id, 123, "a" * 64),
            )

            with self.assertRaisesRegex(ValueError, "terminal chain"):
                ledger.record_campaign_trial(
                    attempt,
                    campaign.TrialEvidence(
                        campaign.TerminalStatus.SCREEN_REJECTED,
                        True,
                        ("NO_SIGNALS", "MINIMUM_TRADE_COUNT", "MINIMUM_SIGNAL_COUNT"),
                        "5" * 64,
                        candidate_id,
                    ),
                )

    def test_campaign_trial_rejects_raw_signal_count_that_differs_from_candidate(self) -> None:
        base_attempt = campaign.expand_campaign(self._spec())[0]
        assert base_attempt.strategy_id is not None
        identity = strategy_lab.ExperimentIdentity(
            base_attempt.strategy_id,
            "2" * 64,
            "3" * 64,
            "engine-v2",
            "4" * 64,
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = strategy_lab.StrategyLedger(root / "ledger.sqlite3")
            ledger.initialize()
            policy, _candidate_path, _screen_path, candidate_id = self._cached_screen_fixture(
                root,
                ledger,
                identity,
                candidate_signal_count=1,
            )
            spec = self._spec(screen_policy_id=policy.policy_id)
            attempt = campaign.expand_campaign(spec)[0]
            ledger.record_campaign(
                spec,
                campaign.CampaignPreflight(policy.policy_id, 123, "a" * 64),
            )

            with self.assertRaisesRegex(ValueError, "terminal chain"):
                ledger.record_campaign_trial(
                    attempt,
                    campaign.TrialEvidence(
                        campaign.TerminalStatus.SCREEN_REJECTED,
                        True,
                        ("NO_SIGNALS", "MINIMUM_TRADE_COUNT", "MINIMUM_SIGNAL_COUNT"),
                        "5" * 64,
                        candidate_id,
                    ),
                )

    def test_campaign_trial_rejects_spec_policy_that_differs_from_frozen_policy(self) -> None:
        spec = self._spec()
        attempt = campaign.expand_campaign(spec)[0]
        assert attempt.strategy_id is not None
        identity = strategy_lab.ExperimentIdentity(
            attempt.strategy_id,
            "2" * 64,
            "3" * 64,
            "engine-v2",
            "4" * 64,
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = strategy_lab.StrategyLedger(root / "ledger.sqlite3")
            ledger.initialize()
            policy, _candidate_path, _screen_path, candidate_id = self._cached_screen_fixture(
                root,
                ledger,
                identity,
            )
            self.assertNotEqual(spec.screen_policy_id, policy.policy_id)
            ledger.record_campaign(
                spec,
                campaign.CampaignPreflight(policy.policy_id, 123, "a" * 64),
            )

            with self.assertRaisesRegex(ValueError, "terminal chain"):
                ledger.record_campaign_trial(
                    attempt,
                    campaign.TrialEvidence(
                        campaign.TerminalStatus.SCREEN_REJECTED,
                        True,
                        ("NO_SIGNALS", "MINIMUM_TRADE_COUNT", "MINIMUM_SIGNAL_COUNT"),
                        "5" * 64,
                        candidate_id,
                    ),
                )

    def test_campaign_reuse_rejects_tampered_cached_stage_and_candidate_artifacts(self) -> None:
        identity = strategy_lab.ExperimentIdentity(
            strategy_id="1" * 64,
            data_source_id="2" * 64,
            policy_id="3" * 64,
            engine_id="engine-v2",
            runtime_id="4" * 64,
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = strategy_lab.StrategyLedger(root / "ledger.sqlite3")
            ledger.initialize()
            policy, candidate_path, artifact_path, candidate_id = self._cached_screen_fixture(
                root,
                ledger,
                identity,
            )

            cached = ledger.existing_execution(identity, policy)
            self.assertIsNotNone(cached)
            assert cached is not None
            self.assertEqual(cached.terminal_status, campaign.TerminalStatus.SCREEN_REJECTED)
            self.assertEqual(cached.candidate_id, candidate_id)
            candidate_bytes = candidate_path.read_bytes()
            candidate_path.unlink()
            with self.assertRaisesRegex(ValueError, "artifact is missing"):
                ledger.existing_execution(identity, policy)
            candidate_path.write_bytes(candidate_bytes)
            artifact_path.write_bytes(
                artifact_path.read_bytes().replace(b'"NO_SIGNALS"', b'"TAMPERED"'),
            )

            with self.assertRaisesRegex(ValueError, "artifact hash mismatch"):
                ledger.existing_execution(identity, policy)

    def test_campaign_reuse_rejects_semantically_inconsistent_screen_with_matching_hash(self) -> None:
        identity = strategy_lab.ExperimentIdentity(
            strategy_id="1" * 64,
            data_source_id="2" * 64,
            policy_id="3" * 64,
            engine_id="engine-v2",
            runtime_id="4" * 64,
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = strategy_lab.StrategyLedger(root / "ledger.sqlite3")
            ledger.initialize()
            policy, _candidate_path, _screen_path, _candidate_id = self._cached_screen_fixture(
                root,
                ledger,
                identity,
                semantic_tamper=True,
            )

            with self.assertRaisesRegex(ValueError, "cached screen artifact does not match"):
                ledger.existing_execution(identity, policy)

    def test_v2_verdict_requires_a_valid_nautilus_terminal_chain(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            hypothesis_path = root / "hypothesis.json"
            hypothesis_path.write_bytes(_canonical(_hypothesis()))
            hypothesis = strategy_lab.load_strategy_hypothesis(hypothesis_path)
            ledger = strategy_lab.StrategyLedger(root / "ledger.sqlite3")
            ledger.initialize()
            ledger.record_hypothesis(hypothesis)
            identity = strategy_lab.ExperimentIdentity(
                hypothesis.strategy_id,
                "a" * 64,
                "b" * 64,
                "engine-v2",
                "d" * 64,
                1,
                "a" * 64,
            )
            experiment_id = ledger.record_experiment(hypothesis.hypothesis_id, identity)
            candidate_path = root / "candidate.json"
            completed = _candidate_process(
                {
                    "trade_count": 1,
                    "signal_count": 1,
                    "total_return": 0.1,
                    "max_drawdown": 0.1,
                    "turnover": 1.0,
                },
            )(
                [
                    "python",
                    "research.py",
                    "--evaluation-context-id",
                    "e" * 64,
                    "--environment-id",
                    "d" * 64,
                    "--output",
                    str(candidate_path),
                ],
            )
            raw_path = root / "research-result-v2-raw.json"
            raw_path.write_bytes(completed.stdout)
            ledger.record_stage(
                strategy_lab.StageRecord(
                    experiment_id,
                    "PyBroker completed",
                    "PASSED",
                    "PYBROKER_COMPLETED",
                    str(raw_path),
                    sha256(raw_path.read_bytes()).hexdigest(),
                ),
            )
            result = campaign.load_research_result_v2(completed.stdout)
            policy_document = {
                "max_provisional_drawdown": 0.5,
                "max_turnover": 10.0,
                "minimum_signal_count": 1,
                "minimum_trade_count": 1,
                "policy_version": "test-screen-v1",
                "reject_no_signal": True,
                "schema_version": "strategy-research-policy-v1",
            }
            policy = campaign.ScreenPolicy(
                sha256(_canonical(policy_document)).hexdigest(),
                "test-screen-v1",
                1,
                1,
                0.5,
                10.0,
                True,
            )
            decision = campaign.screen_research_result(result, policy)
            self.assertEqual(decision.outcome, "PASSED")
            screen_path = root / "research-screen-result-v1.json"
            screen_path.write_bytes(
                _canonical(campaign.screened_result_document(result, decision, policy)),
            )
            ledger.record_stage(
                strategy_lab.StageRecord(
                    experiment_id,
                    "Research screened",
                    "PASSED",
                    "SCREEN_PASSED",
                    str(screen_path),
                    sha256(screen_path.read_bytes()).hexdigest(),
                ),
            )
            parity = _parity_for_candidate(candidate_path, root / "catalog")
            parity_path = root / "signal-parity-result.json"
            parity_path.write_bytes(parity.canonical_bytes)
            ledger.record_signal_parity(
                strategy_lab.SignalParityRecord(
                    experiment_id,
                    parity.candidate_id,
                    "e" * 64,
                    "a" * 64,
                    "PASS",
                    "SIGNAL_PARITY_MATCH",
                    None,
                    str(parity_path),
                    parity.artifact_sha256,
                    parity.decisions,
                ),
            )
            invalid_path = root / "nautilus-verdict.json"
            invalid_path.write_bytes(_canonical({}))
            record = strategy_lab.VerdictRecord(
                experiment_id,
                "SUCCESS",
                "TEST_PASS",
                str(invalid_path),
                sha256(invalid_path.read_bytes()).hexdigest(),
            )

            with self.assertRaisesRegex(strategy_lab.StrategyLabError, "Nautilus replay"):
                ledger.record_verdict(record)
            ledger.record_stage(
                strategy_lab.StageRecord(
                    experiment_id,
                    "Nautilus replayed",
                    "PASSED",
                    "NAUTILUS_REPLAY_COMPLETED",
                    str(invalid_path),
                    record.artifact_sha256,
                ),
            )
            with self.assertRaisesRegex(strategy_lab.StrategyLabError, "Nautilus verdict"):
                ledger.record_verdict(record)

    def test_campaign_load_rejects_empty_approved_lists_and_malformed_content_ids(self) -> None:
        spec = campaign.CampaignSpec(
            family_id="close-vs-sma-mean-reversion-long-flat",
            family_version="close-vs-sma-mean-reversion-long-flat-v1",
            search_space={"discount_threshold": (0.0,), "window_bars": (2,)},
            approved_instruments=("BTCUSDT-PERP.BINANCE",),
            approved_bar_types=("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",),
            parameter_search_policy_id="f" * 64,
            seed=42,
            data_as_of_ns=123,
            generation_budget=1,
            maximum_candidates=1,
            screen_policy_id="e" * 64,
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            empty_path = root / "empty.json"
            empty_document = {**spec.document, "approved_instruments": []}
            empty_path.write_bytes(_canonical(empty_document))
            with self.assertRaises(campaign.StrategyCampaignError):
                campaign.load_campaign_spec(empty_path)

            malformed_path = root / "malformed.json"
            malformed_document = {**spec.document, "screen_policy_id": "G" * 64}
            malformed_path.write_bytes(_canonical(malformed_document))
            with self.assertRaises(campaign.StrategyCampaignError):
                campaign.load_campaign_spec(malformed_path)

            oversized_path = root / "oversized.json"
            oversized_document = {**spec.document, "data_as_of_ns": 1 << 63}
            oversized_path.write_bytes(_canonical(oversized_document))
            with self.assertRaises(campaign.StrategyCampaignError):
                campaign.load_campaign_spec(oversized_path)

    def test_campaign_ledger_rejects_sqlite_integer_overflow_at_the_boundary(self) -> None:
        spec = self._spec()
        with TemporaryDirectory() as temporary:
            ledger = strategy_lab.StrategyLedger(Path(temporary) / "ledger.sqlite3")
            ledger.initialize()

            with self.assertRaisesRegex(strategy_lab.StrategyLabError, "data_as_of_ns"):
                ledger.record_campaign(
                    spec,
                    campaign.CampaignPreflight("e" * 64, 1 << 63, "a" * 64),
                )

    def test_research_result_rejects_huge_integers_as_campaign_errors(self) -> None:
        payload = _canonical(
            {
                "candidate_id": "a" * 64,
                "provisional_metrics": {
                    "max_drawdown": 0.0,
                    "signal_count": 1,
                    "total_return": 10**400,
                    "trade_count": 1,
                    "turnover": 1.0,
                },
                "schema_version": "research-result-v2",
                "truth_status": "provisional",
            },
        )

        with self.assertRaises(campaign.StrategyCampaignError):
            campaign.load_research_result_v2(payload)

    def test_huge_search_parameter_becomes_invalid_attempt_without_overflow(self) -> None:
        spec = campaign.CampaignSpec(
            family_id="close-vs-sma-mean-reversion-long-flat",
            family_version="close-vs-sma-mean-reversion-long-flat-v1",
            search_space={"discount_threshold": (10**400,), "window_bars": (2,)},
            approved_instruments=("BTCUSDT-PERP.BINANCE",),
            approved_bar_types=("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",),
            parameter_search_policy_id="f" * 64,
            seed=42,
            data_as_of_ns=123,
            generation_budget=1,
            maximum_candidates=1,
            screen_policy_id="e" * 64,
        )

        attempt = campaign.expand_campaign(spec)[0]

        self.assertIsNone(attempt.strategy_id)

    def test_valid_v2_candidate_with_no_activity_is_screen_rejected_before_nautilus(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            hypothesis_path = root / "hypothesis.json"
            hypothesis_path.write_bytes(_canonical(_hypothesis()))
            paths = strategy_lab.StrategyLoopPaths(
                market_data_path=root / "market-data.json",
                policy_path=Path("config/strategy_loop_policy.json"),
                catalog_path=root / "catalog",
                funding_path=root / "funding",
                state_path=root / "state",
            )

            research_process = _candidate_process(
                {"trade_count": 0, "signal_count": 0, "total_return": 0.0, "max_drawdown": 0.0, "turnover": 0.0},
            )
            with (
                patch.object(strategy_lab, "_hash_tree", return_value="c" * 64),
                patch.object(strategy_lab, "_catalog_digest", return_value="a" * 64),
                patch.object(strategy_lab, "_catalog_last_timestamp", return_value=1),
                patch.object(strategy_lab, "_runtime_identity", return_value="d" * 64),
                patch.object(strategy_lab, "_engine_identity", return_value="engine-v2"),
                patch.object(strategy_lab, "_code_commit", return_value="e" * 40),
                patch.object(strategy_lab, "_run_bounded_process", side_effect=research_process),
                patch.object(strategy_lab, "validated_candidate_source_bars", return_value=[]),
                patch.object(
                    strategy_lab,
                    "run_candidate_backtest",
                    side_effect=AssertionError("screen-rejected candidate reached Nautilus"),
                ) as run_nautilus,
            ):
                feedback = strategy_lab.run_strategy_loop(hypothesis_path, paths)

            self.assertEqual(feedback["status"], "SCREEN_REJECTED")
            self.assertEqual(
                feedback["reason_codes"],
                ["NO_SIGNALS", "MINIMUM_TRADE_COUNT", "MINIMUM_SIGNAL_COUNT"],
            )
            run_directory = paths.state_path / "runs" / feedback["experiment_id"]
            self.assertTrue((run_directory / "candidate.json").is_file())
            self.assertTrue((run_directory / "research-screen-result-v1.json").is_file())
            result = json.loads((run_directory / "research-screen-result-v1.json").read_bytes())
            self.assertEqual(result["schema_version"], "research-screen-result-v1")
            self.assertEqual(
                campaign.load_screen_result_v1(
                    (run_directory / "research-screen-result-v1.json").read_bytes(),
                ),
                result,
            )
            self.assertEqual(
                result["screen_reason_codes"],
                ["NO_SIGNALS", "MINIMUM_TRADE_COUNT", "MINIMUM_SIGNAL_COUNT"],
            )
            metrics = result["provisional_metrics"]
            self.assertTrue(all(isinstance(value, (int, float)) and value == value for value in metrics.values()))
            run_nautilus.assert_not_called()

    def test_signal_count_mismatch_is_technical_and_never_reaches_nautilus(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            hypothesis_path = root / "hypothesis.json"
            hypothesis_path.write_bytes(_canonical(_hypothesis()))
            paths = strategy_lab.StrategyLoopPaths(
                market_data_path=root / "market-data.json",
                policy_path=Path("config/strategy_loop_policy.json"),
                catalog_path=root / "catalog",
                funding_path=root / "funding",
                state_path=root / "state",
            )
            research_process = _candidate_process(
                {
                    "trade_count": 1,
                    "signal_count": 1,
                    "total_return": 0.1,
                    "max_drawdown": 0.1,
                    "turnover": 1.0,
                },
                candidate_signal_count=0,
            )
            with (
                patch.object(strategy_lab, "_hash_tree", return_value="c" * 64),
                patch.object(strategy_lab, "_catalog_digest", return_value="a" * 64),
                patch.object(strategy_lab, "_catalog_last_timestamp", return_value=1),
                patch.object(strategy_lab, "_runtime_identity", return_value="d" * 64),
                patch.object(strategy_lab, "_engine_identity", return_value="engine-v2"),
                patch.object(strategy_lab, "_code_commit", return_value="e" * 40),
                patch.object(strategy_lab, "_run_bounded_process", side_effect=research_process),
                patch.object(strategy_lab, "validated_candidate_source_bars", return_value=[]),
                patch.object(strategy_lab, "run_signal_parity_gate", side_effect=_parity_for_candidate),
                patch.object(
                    strategy_lab,
                    "run_candidate_backtest",
                    side_effect=AssertionError("mismatched signal count reached Nautilus"),
                ) as run_nautilus,
            ):
                feedback = strategy_lab.run_strategy_loop(hypothesis_path, paths)

        self.assertEqual(feedback["status"], "ERROR")
        self.assertEqual(feedback["reason_codes"], ["RESEARCH_RESULT_INVALID"])
        run_nautilus.assert_not_called()

    def test_valid_v2_candidate_that_passes_screen_is_the_only_path_to_nautilus(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            hypothesis_path = root / "hypothesis.json"
            hypothesis_path.write_bytes(_canonical(_hypothesis()))
            paths = strategy_lab.StrategyLoopPaths(
                market_data_path=root / "market-data.json",
                policy_path=Path("config/strategy_loop_policy.json"),
                catalog_path=root / "catalog",
                funding_path=root / "funding",
                state_path=root / "state",
            )
            research_process = _candidate_process(
                {"trade_count": 1, "signal_count": 1, "total_return": 0.1, "max_drawdown": 0.1, "turnover": 1.0},
            )
            with (
                patch.object(strategy_lab, "_hash_tree", return_value="c" * 64),
                patch.object(strategy_lab, "_catalog_digest", return_value="a" * 64),
                patch.object(strategy_lab, "_catalog_last_timestamp", return_value=1),
                patch.object(strategy_lab, "_runtime_identity", return_value="d" * 64),
                patch.object(strategy_lab, "_engine_identity", return_value="engine-v2"),
                patch.object(strategy_lab, "_code_commit", return_value="e" * 40),
                patch.object(strategy_lab, "_run_bounded_process", side_effect=research_process),
                patch.object(strategy_lab, "validated_candidate_source_bars", return_value=[]),
                patch.object(strategy_lab, "run_signal_parity_gate", side_effect=_parity_for_candidate),
                patch.object(
                    strategy_lab,
                    "run_candidate_backtest",
                    side_effect=_backtest_for_request,
                ) as run_nautilus,
            ):
                feedback = strategy_lab.run_strategy_loop(hypothesis_path, paths)

            self.assertEqual(feedback["status"], "EVALUATED")
            run_nautilus.assert_called_once()
            run_directory = paths.state_path / "runs" / feedback["experiment_id"]
            screen_path = run_directory / "research-screen-result-v1.json"
            self.assertEqual(json.loads(screen_path.read_bytes())["screen_outcome"], "PASSED")
            with closing(sqlite3.connect(paths.state_path / "ledger.sqlite3")) as connection:
                stage = connection.execute(
                    """SELECT outcome, reason_code, artifact_path
                    FROM stage_results WHERE stage = 'Research screened'""",
                ).fetchone()
            self.assertEqual(stage, ("PASSED", "SCREEN_PASSED", str(screen_path)))
            run_directory = paths.state_path / "runs" / str(feedback["experiment_id"])
            candidate_path = run_directory / "candidate.json"
            candidate_bytes = candidate_path.read_bytes()
            candidate_path.unlink()
            with (
                patch.object(strategy_lab, "_hash_tree", return_value="c" * 64),
                patch.object(strategy_lab, "_catalog_digest", return_value="a" * 64),
                patch.object(strategy_lab, "_catalog_last_timestamp", return_value=1),
                patch.object(strategy_lab, "_runtime_identity", return_value="d" * 64),
                patch.object(strategy_lab, "_engine_identity", return_value="engine-v2"),
                patch.object(strategy_lab, "_code_commit", return_value="e" * 40),
                patch.object(
                    strategy_lab,
                    "_run_bounded_process",
                    side_effect=AssertionError("untrusted terminal chain relaunched research"),
                ),
                self.assertRaisesRegex(ValueError, "artifact is missing"),
            ):
                strategy_lab.run_strategy_loop(hypothesis_path, paths)
            candidate_path.write_bytes(candidate_bytes)
            ledger_path = paths.state_path / "ledger.sqlite3"
            with closing(sqlite3.connect(ledger_path)) as connection, connection:
                connection.execute("DROP TRIGGER signal_parity_results_immutable_delete")
                connection.execute("DELETE FROM signal_parity_results")
            with (
                patch.object(strategy_lab, "_hash_tree", return_value="c" * 64),
                patch.object(strategy_lab, "_catalog_digest", return_value="a" * 64),
                patch.object(strategy_lab, "_catalog_last_timestamp", return_value=1),
                patch.object(strategy_lab, "_runtime_identity", return_value="d" * 64),
                patch.object(strategy_lab, "_engine_identity", return_value="engine-v2"),
                patch.object(strategy_lab, "_code_commit", return_value="e" * 40),
                patch.object(
                    strategy_lab,
                    "_run_bounded_process",
                    side_effect=AssertionError("missing parity relaunched research"),
                ),
                self.assertRaisesRegex(ValueError, "PASS parity"),
            ):
                strategy_lab.run_strategy_loop(hypothesis_path, paths)

    def test_campaign_expansion_uses_canonical_strategy_ids_after_parameter_normalization(self) -> None:
        spec = campaign.CampaignSpec(
            family_id="lookback-momentum-long-flat",
            family_version="lookback-momentum-long-flat-v1",
            search_space={"entry_threshold": (0.0, 0.05), "lookback_bars": (2, 4)},
            approved_instruments=("BTCUSDT-PERP.BINANCE",),
            approved_bar_types=("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",),
            parameter_search_policy_id="f" * 64,
            seed=42,
            data_as_of_ns=123,
            generation_budget=4,
            maximum_candidates=4,
            screen_policy_id="e" * 64,
        )

        first = campaign.expand_campaign(spec)
        second = campaign.expand_campaign(spec)

        self.assertEqual(
            [attempt.parameters for attempt in first],
            [
                {"entry_threshold": 0.0, "lookback_bars": 2},
                {"entry_threshold": 0.0, "lookback_bars": 4},
                {"entry_threshold": 0.05, "lookback_bars": 2},
                {"entry_threshold": 0.05, "lookback_bars": 4},
            ],
        )
        self.assertEqual(
            [attempt.strategy_id for attempt in first],
            [attempt.strategy_id for attempt in second],
        )
        self.assertTrue(all(attempt.strategy_id for attempt in first))

        equivalent = campaign.CampaignSpec(
            family_id=spec.family_id,
            family_version=spec.family_version,
            search_space={"entry_threshold": (0, 0.0), "lookback_bars": (2,)},
            approved_instruments=spec.approved_instruments,
            approved_bar_types=spec.approved_bar_types,
            parameter_search_policy_id=spec.parameter_search_policy_id,
            seed=spec.seed,
            data_as_of_ns=spec.data_as_of_ns,
            generation_budget=2,
            maximum_candidates=2,
            screen_policy_id=spec.screen_policy_id,
        )
        equivalent_attempts = campaign.expand_campaign(equivalent)
        self.assertEqual(equivalent_attempts[0].strategy_id, equivalent_attempts[1].strategy_id)

    def test_campaign_budget_fails_before_executor_or_ledger_access(self) -> None:
        spec = campaign.CampaignSpec(
            family_id="lookback-momentum-long-flat",
            family_version="lookback-momentum-long-flat-v1",
            search_space={"entry_threshold": (0.0, 0.05), "lookback_bars": (2, 4)},
            approved_instruments=("BTCUSDT-PERP.BINANCE",),
            approved_bar_types=("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",),
            parameter_search_policy_id="f" * 64,
            seed=42,
            data_as_of_ns=123,
            generation_budget=4,
            maximum_candidates=3,
            screen_policy_id="e" * 64,
        )
        with self.assertRaises(campaign.CampaignBudgetExceeded):
            campaign.run_campaign(
                spec,
                ledger=unittest.mock.Mock(),
                preflight=campaign.CampaignPreflight("e" * 64, 123, "a" * 64),
                execute=lambda _attempt: self.fail("executor launched before budget check"),
            )

    def test_campaign_dedupe_reuse_and_policy_change_reexecute(self) -> None:
        base = {
            "family_id": "lookback-momentum-long-flat",
            "family_version": "lookback-momentum-long-flat-v1",
            "search_space": {"entry_threshold": (0.0, 0.0), "lookback_bars": (2,)},
            "approved_instruments": ("BTCUSDT-PERP.BINANCE",),
            "approved_bar_types": ("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",),
            "parameter_search_policy_id": "f" * 64,
            "seed": 42,
            "data_as_of_ns": 123,
            "generation_budget": 2,
            "maximum_candidates": 2,
        }
        first_spec = campaign.CampaignSpec(**base, screen_policy_id="e" * 64)
        second_spec = campaign.CampaignSpec(
            **{**base, "seed": 43},
            screen_policy_id="e" * 64,
        )
        policy_changed_spec = campaign.CampaignSpec(**base, screen_policy_id="d" * 64)
        calls: list[str] = []

        def execute(attempt: campaign.CampaignAttempt) -> campaign.TrialEvidence:
            calls.append(attempt.strategy_id or "invalid")
            return campaign.TrialEvidence(
                campaign.TerminalStatus.TECHNICAL_INVALID,
                False,
                ("TEST_TERMINAL",),
            )

        with TemporaryDirectory() as temporary:
            ledger = strategy_lab.StrategyLedger(Path(temporary) / "ledger.sqlite3")
            ledger.initialize()
            first_summary = campaign.run_campaign(
                first_spec,
                ledger=ledger,
                preflight=campaign.CampaignPreflight("e" * 64, 123, "a" * 64),
                execute=execute,
            )
            rerun_summary = campaign.run_campaign(
                first_spec,
                ledger=ledger,
                preflight=campaign.CampaignPreflight("e" * 64, 123, "a" * 64),
                execute=execute,
            )
            prior = {
                attempt.strategy_id: campaign.TrialEvidence(
                    campaign.TerminalStatus.TECHNICAL_INVALID,
                    False,
                    ("TEST_TERMINAL",),
                )
                for attempt in campaign.expand_campaign(first_spec)
                if attempt.strategy_id is not None
            }
            second_summary = campaign.run_campaign(
                second_spec,
                ledger=ledger,
                preflight=campaign.CampaignPreflight("e" * 64, 123, "a" * 64),
                execute=execute,
                reuse=lambda attempt: prior.get(attempt.strategy_id),
            )
            policy_changed_summary = campaign.run_campaign(
                policy_changed_spec,
                ledger=ledger,
                preflight=campaign.CampaignPreflight("d" * 64, 123, "a" * 64),
                execute=execute,
            )
            with closing(sqlite3.connect(ledger.path)) as connection:
                memberships = connection.execute("SELECT COUNT(*) FROM campaign_trials").fetchone()[0]
                reasons = connection.execute(
                    "SELECT reason_codes_json FROM campaign_trials WHERE terminal_status = 'DUPLICATE_SUPPRESSED'",
                ).fetchall()

        self.assertEqual(len(calls), 2)
        self.assertEqual(memberships, 6)
        self.assertEqual(first_summary["deduped_count"], 1)
        self.assertEqual(rerun_summary, first_summary)
        self.assertEqual(second_summary["deduped_count"], 1)
        self.assertEqual(policy_changed_summary["deduped_count"], 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            sorted(json.loads(row[0])[0] for row in reasons),
            [
                "DUPLICATE_CONTENT_ID",
                "DUPLICATE_CONTENT_ID",
                "DUPLICATE_CONTENT_ID",
            ],
        )

    def test_campaign_summary_reconciles_terminal_statuses_and_keeps_technical_orthogonal(self) -> None:
        spec = campaign.CampaignSpec(
            family_id="close-vs-sma-mean-reversion-long-flat",
            family_version="close-vs-sma-mean-reversion-long-flat-v1",
            search_space={"discount_threshold": (0.0, 0.1, 0.2), "window_bars": (2,)},
            approved_instruments=("BTCUSDT-PERP.BINANCE",),
            approved_bar_types=("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",),
            parameter_search_policy_id="f" * 64,
            seed=42,
            data_as_of_ns=123,
            generation_budget=3,
            maximum_candidates=3,
            screen_policy_id="e" * 64,
        )

        with TemporaryDirectory() as temporary:
            ledger = strategy_lab.StrategyLedger(Path(temporary) / "ledger.sqlite3")
            ledger.initialize()

            def execute(attempt: campaign.CampaignAttempt) -> campaign.TrialEvidence:
                if attempt.ordinal == 0:
                    raise campaign.CampaignTechnicalError("DATA_INVALID", False)
                experiment_id, candidate_id = self._insert_stub_experiment(
                    ledger,
                    attempt,
                    str(attempt.ordinal),
                )
                if attempt.ordinal == 1:
                    return campaign.TrialEvidence(
                        campaign.TerminalStatus.SCREEN_REJECTED,
                        True,
                        ("NO_SIGNALS",),
                        experiment_id,
                        candidate_id,
                    )
                return campaign.TrialEvidence(
                    campaign.TerminalStatus.SURVIVED,
                    True,
                    ("SCREEN_PASSED",),
                    experiment_id,
                    candidate_id,
                )

            with self.assertRaisesRegex(ValueError, "terminal chain"):
                campaign.run_campaign(
                    spec,
                    ledger=ledger,
                    preflight=campaign.CampaignPreflight("e" * 64, 123, "a" * 64),
                    execute=execute,
                )

    def test_invalid_parameters_and_frozen_cohort_mismatch_launch_no_executor(self) -> None:
        invalid_spec = campaign.CampaignSpec(
            family_id="close-vs-sma-mean-reversion-long-flat",
            family_version="close-vs-sma-mean-reversion-long-flat-v1",
            search_space={"discount_threshold": (0.0,), "window_bars": (1,)},
            approved_instruments=("BTCUSDT-PERP.BINANCE",),
            approved_bar_types=("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",),
            parameter_search_policy_id="f" * 64,
            seed=42,
            data_as_of_ns=123,
            generation_budget=1,
            maximum_candidates=1,
            screen_policy_id="e" * 64,
        )
        mismatch_spec = campaign.CampaignSpec(
            family_id="close-vs-sma-mean-reversion-long-flat",
            family_version="close-vs-sma-mean-reversion-long-flat-v1",
            search_space={"discount_threshold": (0.0, 0.0), "window_bars": (2,)},
            approved_instruments=("BTCUSDT-PERP.BINANCE",),
            approved_bar_types=("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",),
            parameter_search_policy_id="f" * 64,
            seed=42,
            data_as_of_ns=123,
            generation_budget=2,
            maximum_candidates=2,
            screen_policy_id="e" * 64,
        )
        with TemporaryDirectory() as temporary:
            ledger = strategy_lab.StrategyLedger(Path(temporary) / "ledger.sqlite3")
            ledger.initialize()
            invalid_summary = campaign.run_campaign(
                invalid_spec,
                ledger=ledger,
                preflight=campaign.CampaignPreflight("e" * 64, 123, "a" * 64),
                execute=lambda _attempt: self.fail("invalid parameters launched executor"),
            )
            mismatch_summary = campaign.run_campaign(
                mismatch_spec,
                ledger=ledger,
                preflight=campaign.CampaignPreflight("d" * 64, 999, "a" * 64),
                execute=lambda _attempt: self.fail("frozen cohort mismatch launched executor"),
            )
            trials = ledger.campaign_trials(mismatch_spec.campaign_id)
            with closing(sqlite3.connect(ledger.path)) as connection:
                census = connection.execute(
                    """SELECT terminal_status, execution_started, COUNT(*)
                    FROM campaign_trials WHERE campaign_id = ?
                    GROUP BY terminal_status, execution_started""",
                    (mismatch_spec.campaign_id,),
                ).fetchall()

        self.assertEqual(invalid_summary["technical_invalid_count"], 1)
        self.assertEqual(mismatch_summary["technical_invalid_count"], 2)
        self.assertEqual(mismatch_summary["deduped_count"], 0)
        self.assertTrue(all(not trial.evidence.execution_started for trial in trials))
        self.assertEqual(
            sorted(census),
            [("TECHNICAL_INVALID", 0, 2)],
        )

    def test_strategy_campaign_preflight_mismatch_persists_technical_census_without_process(self) -> None:
        spec = campaign.CampaignSpec(
            family_id="lookback-momentum-long-flat",
            family_version="lookback-momentum-long-flat-v1",
            search_space={"entry_threshold": (0.0, 0.0), "lookback_bars": (2,)},
            approved_instruments=("BTCUSDT-PERP.BINANCE",),
            approved_bar_types=("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",),
            parameter_search_policy_id="f" * 64,
            seed=42,
            data_as_of_ns=123,
            generation_budget=2,
            maximum_candidates=2,
            screen_policy_id="e" * 64,
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = strategy_lab.StrategyLoopPaths(
                market_data_path=root / "market-data.json",
                policy_path=Path("config/strategy_loop_policy.json"),
                catalog_path=root / "catalog",
                funding_path=root / "funding",
                state_path=root / "state",
                research_policy_path=Path("config/strategy_research_policy.json"),
            )
            with (
                patch.object(strategy_lab, "_catalog_last_timestamp", return_value=999),
                patch.object(strategy_lab, "_catalog_digest", return_value="a" * 64),
                patch.object(
                    strategy_lab,
                    "_run_bounded_process",
                    side_effect=AssertionError("preflight mismatch launched research process"),
                ) as run_process,
            ):
                summary = strategy_lab.run_strategy_campaign(spec, paths)
            ledger = strategy_lab.StrategyLedger(paths.state_path / "ledger.sqlite3")
            trials = ledger.campaign_trials(spec.campaign_id)
            with closing(sqlite3.connect(ledger.path)) as connection:
                census = connection.execute(
                    """SELECT terminal_status, execution_started, COUNT(*)
                    FROM campaign_trials GROUP BY terminal_status, execution_started""",
                ).fetchall()

        run_process.assert_not_called()
        self.assertEqual(summary["technical_invalid_count"], 2)
        self.assertEqual(summary["deduped_count"], 0)
        self.assertTrue(all(not trial.evidence.execution_started for trial in trials))
        self.assertEqual(
            sorted(census),
            [("TECHNICAL_INVALID", 0, 2)],
        )

    def test_reuse_and_duplicate_memberships_never_claim_started_execution(self) -> None:
        spec = campaign.CampaignSpec(
            family_id="lookback-momentum-long-flat",
            family_version="lookback-momentum-long-flat-v1",
            search_space={"entry_threshold": (0.0, 0.0), "lookback_bars": (2,)},
            approved_instruments=("BTCUSDT-PERP.BINANCE",),
            approved_bar_types=("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",),
            parameter_search_policy_id="f" * 64,
            seed=42,
            data_as_of_ns=123,
            generation_budget=2,
            maximum_candidates=2,
            screen_policy_id="e" * 64,
        )
        with TemporaryDirectory() as temporary:
            ledger = strategy_lab.StrategyLedger(Path(temporary) / "ledger.sqlite3")
            ledger.initialize()
            attempt = campaign.expand_campaign(spec)[0]
            experiment_id, candidate_id = self._insert_stub_experiment(
                ledger,
                attempt,
                "reuse",
            )
            prior = campaign.TrialEvidence(
                campaign.TerminalStatus.SURVIVED,
                True,
                ("SCREEN_PASSED",),
                experiment_id,
                candidate_id,
            )
            with self.assertRaisesRegex(ValueError, "terminal chain"):
                campaign.run_campaign(
                    spec,
                    ledger=ledger,
                    preflight=campaign.CampaignPreflight("e" * 64, 123, "a" * 64),
                    reuse=lambda _attempt: prior,
                    execute=lambda _attempt: self.fail("reuse launched executor"),
                )

    def test_partial_campaign_census_fails_closed_without_execution(self) -> None:
        spec = self._spec(thresholds=(0.0, 0.1, 0.2))
        preflight = campaign.CampaignPreflight("e" * 64, 123, "a" * 64)
        with TemporaryDirectory() as temporary:
            ledger = strategy_lab.StrategyLedger(Path(temporary) / "ledger.sqlite3")
            ledger.initialize()
            ledger.record_campaign(spec, preflight)
            attempts = campaign.expand_campaign(spec)
            first = campaign.TrialEvidence(
                campaign.TerminalStatus.TECHNICAL_INVALID,
                False,
                ("FIRST_ALREADY_RECORDED",),
            )
            ledger.record_campaign_trial(attempts[0], first)

            def never_execute(attempt: campaign.CampaignAttempt) -> campaign.TrialEvidence:
                self.fail(f"partial census launched executor for {attempt.ordinal}")

            def never_reuse(attempt: campaign.CampaignAttempt) -> campaign.TrialEvidence | None:
                self.fail(f"partial census attempted reuse for {attempt.ordinal}")

            with self.assertRaisesRegex(
                campaign.StrategyCampaignError,
                "campaign trial census is incomplete",
            ):
                campaign.run_campaign(
                    spec,
                    ledger=ledger,
                    preflight=preflight,
                    execute=never_execute,
                    reuse=never_reuse,
                )

            stored = ledger.campaign_trials(spec.campaign_id)

        self.assertEqual([trial.ordinal for trial in stored], [0])

    def test_completed_campaign_revalidates_stored_trials_before_summary(self) -> None:
        spec = self._spec()
        preflight = campaign.CampaignPreflight("e" * 64, 123, "a" * 64)
        with TemporaryDirectory() as temporary:
            ledger = strategy_lab.StrategyLedger(Path(temporary) / "ledger.sqlite3")
            ledger.initialize()
            campaign.run_campaign(
                spec,
                ledger=ledger,
                preflight=preflight,
                execute=lambda _attempt: campaign.TrialEvidence(
                    campaign.TerminalStatus.TECHNICAL_INVALID,
                    False,
                    ("TEST_TERMINAL",),
                ),
            )

            def reject_stored(_trial: campaign.CampaignTrial) -> None:
                raise campaign.StrategyCampaignError("stored evidence is untrusted")

            with self.assertRaisesRegex(
                campaign.StrategyCampaignError,
                "stored evidence is untrusted",
            ):
                campaign.run_campaign(
                    spec,
                    ledger=ledger,
                    preflight=preflight,
                    execute=lambda _attempt: self.fail("completed campaign executed"),
                    validate_stored=reject_stored,
                )

    def test_completed_strategy_campaign_revalidates_real_artifacts(self) -> None:
        screen_policy_id = campaign.load_screen_policy(
            Path("config/strategy_research_policy.json"),
        ).policy_id
        spec = campaign.CampaignSpec(
            family_id="lookback-momentum-long-flat",
            family_version="lookback-momentum-long-flat-v1",
            search_space={"entry_threshold": (0.0,), "lookback_bars": (2,)},
            approved_instruments=("BTCUSDT-PERP.BINANCE",),
            approved_bar_types=("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",),
            parameter_search_policy_id="f" * 64,
            seed=42,
            data_as_of_ns=123,
            generation_budget=1,
            maximum_candidates=1,
            screen_policy_id=screen_policy_id,
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = strategy_lab.StrategyLoopPaths(
                market_data_path=root / "market-data.json",
                policy_path=Path("config/strategy_loop_policy.json"),
                catalog_path=root / "catalog",
                funding_path=root / "funding",
                state_path=root / "state",
                research_policy_path=Path("config/strategy_research_policy.json"),
            )
            with (
                patch.object(strategy_lab, "_catalog_last_timestamp", return_value=123),
                patch.object(strategy_lab, "_catalog_digest", return_value="a" * 64),
                patch.object(strategy_lab, "_hash_tree", return_value="a" * 64),
                patch.object(strategy_lab, "_runtime_identity", return_value="d" * 64),
                patch.object(strategy_lab, "_engine_identity", return_value="engine-v2"),
                patch.object(strategy_lab, "_code_commit", return_value="e" * 40),
                patch.object(
                    strategy_lab,
                    "_run_bounded_process",
                    side_effect=_candidate_process(
                        {
                            "trade_count": 1,
                            "signal_count": 1,
                            "total_return": 0.1,
                            "max_drawdown": 0.1,
                            "turnover": 1.0,
                        },
                        source_last_timestamp=123,
                    ),
                ),
                patch.object(strategy_lab, "validated_candidate_source_bars", return_value=[]),
                patch.object(
                    strategy_lab,
                    "run_signal_parity_gate",
                    side_effect=_parity_for_candidate,
                ),
                patch.object(
                    strategy_lab,
                    "run_candidate_backtest",
                    side_effect=_backtest_for_request,
                ),
            ):
                first = strategy_lab.run_strategy_campaign(spec, paths)
            run_directory = paths.state_path / "runs" / str(
                strategy_lab.StrategyLedger(paths.state_path / "ledger.sqlite3")
                .campaign_trials(spec.campaign_id)[0]
                .evidence.experiment_id
            )
            screen_path = run_directory / "research-screen-result-v1.json"
            screen_path.write_bytes(screen_path.read_bytes().replace(b'"PASSED"', b'"BROKEN"'))

            with (
                patch.object(strategy_lab, "_catalog_last_timestamp", return_value=123),
                patch.object(strategy_lab, "_catalog_digest", return_value="a" * 64),
                patch.object(strategy_lab, "_hash_tree", return_value="a" * 64),
                patch.object(strategy_lab, "_runtime_identity", return_value="d" * 64),
                patch.object(strategy_lab, "_engine_identity", return_value="engine-v2"),
                patch.object(strategy_lab, "_code_commit", return_value="e" * 40),
                patch.object(
                    strategy_lab,
                    "_run_bounded_process",
                    side_effect=AssertionError("completed campaign relaunched research"),
                ) as research,
            ):
                with self.assertRaisesRegex(ValueError, "artifact hash mismatch"):
                    strategy_lab.run_strategy_campaign(spec, paths)

        self.assertEqual(first["surviving_count"], 1)
        research.assert_not_called()

    def test_campaign_persists_frozen_preflight_and_rejects_cached_data_drift(self) -> None:
        spec = self._spec()
        preflight = campaign.CampaignPreflight("e" * 64, 123, "a" * 64)
        with TemporaryDirectory() as temporary:
            ledger = strategy_lab.StrategyLedger(Path(temporary) / "ledger.sqlite3")
            ledger.initialize()
            campaign.run_campaign(
                spec,
                ledger=ledger,
                preflight=preflight,
                execute=lambda _attempt: campaign.TrialEvidence(
                    campaign.TerminalStatus.TECHNICAL_INVALID,
                    False,
                    ("TEST_TERMINAL",),
                ),
            )
            with closing(sqlite3.connect(ledger.path)) as connection:
                frozen = connection.execute(
                    """SELECT screen_policy_id, data_as_of_ns, data_source_id
                    FROM campaigns WHERE campaign_id = ?""",
                    (spec.campaign_id,),
                ).fetchone()

            with self.assertRaisesRegex(ValueError, "campaign record conflict"):
                campaign.run_campaign(
                    spec,
                    ledger=ledger,
                    preflight=campaign.CampaignPreflight("e" * 64, 123, "b" * 64),
                    execute=lambda _attempt: self.fail("drifted cached campaign executed"),
                )

        self.assertEqual(frozen, ("e" * 64, 123, "a" * 64))

    def test_data_source_drift_after_preflight_never_launches_research(self) -> None:
        screen_policy_id = campaign.load_screen_policy(
            Path("config/strategy_research_policy.json"),
        ).policy_id
        spec = self._spec(screen_policy_id=screen_policy_id)
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = strategy_lab.StrategyLoopPaths(
                market_data_path=root / "market-data.json",
                policy_path=Path("config/strategy_loop_policy.json"),
                catalog_path=root / "catalog",
                funding_path=root / "funding",
                state_path=root / "state",
                research_policy_path=Path("config/strategy_research_policy.json"),
            )
            with (
                patch.object(strategy_lab, "_catalog_last_timestamp", return_value=123),
                patch.object(strategy_lab, "_catalog_digest", return_value="a" * 64),
                patch.object(strategy_lab, "_hash_tree", side_effect=("a" * 64, "b" * 64)),
                patch.object(strategy_lab, "_runtime_identity", return_value="c" * 64),
                patch.object(strategy_lab, "_engine_identity", return_value="engine-v2"),
                patch.object(strategy_lab, "_code_commit", return_value="d" * 40),
                patch.object(
                    strategy_lab,
                    "_run_bounded_process",
                    side_effect=AssertionError("drifted campaign launched research"),
                ) as run_process,
            ):
                summary = strategy_lab.run_strategy_campaign(spec, paths)

        run_process.assert_not_called()
        self.assertEqual(summary["technical_invalid_count"], 1)
        self.assertEqual(summary["top_reason_codes"][0]["reason_code"], "CAMPAIGN_PREFLIGHT_DRIFT")

    def test_post_run_data_drift_is_terminal_campaign_technical_evidence(self) -> None:
        screen_policy_id = campaign.load_screen_policy(
            Path("config/strategy_research_policy.json"),
        ).policy_id
        spec = campaign.CampaignSpec(
            family_id="lookback-momentum-long-flat",
            family_version="lookback-momentum-long-flat-v1",
            search_space={"entry_threshold": (0.0,), "lookback_bars": (2,)},
            approved_instruments=("BTCUSDT-PERP.BINANCE",),
            approved_bar_types=("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",),
            parameter_search_policy_id="f" * 64,
            seed=42,
            data_as_of_ns=123,
            generation_budget=1,
            maximum_candidates=1,
            screen_policy_id=screen_policy_id,
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = strategy_lab.StrategyLoopPaths(
                market_data_path=root / "market-data.json",
                policy_path=Path("config/strategy_loop_policy.json"),
                catalog_path=root / "catalog",
                funding_path=root / "funding",
                state_path=root / "state",
                research_policy_path=Path("config/strategy_research_policy.json"),
            )
            with (
                patch.object(strategy_lab, "_catalog_last_timestamp", return_value=123),
                patch.object(strategy_lab, "_catalog_digest", return_value="a" * 64),
                patch.object(
                    strategy_lab,
                    "_hash_tree",
                    side_effect=("a" * 64,) * 12 + ("b" * 64,) * 12,
                ),
                patch.object(strategy_lab, "_runtime_identity", return_value="d" * 64),
                patch.object(strategy_lab, "_engine_identity", return_value="engine-v2"),
                patch.object(strategy_lab, "_code_commit", return_value="e" * 40),
                patch.object(
                    strategy_lab,
                    "_run_bounded_process",
                    side_effect=_candidate_process(
                        {
                            "trade_count": 1,
                            "signal_count": 1,
                            "total_return": 0.1,
                            "max_drawdown": 0.1,
                            "turnover": 1.0,
                        },
                        source_last_timestamp=123,
                    ),
                ),
                patch.object(strategy_lab, "validated_candidate_source_bars", return_value=[]),
                patch.object(
                    strategy_lab,
                    "run_signal_parity_gate",
                    side_effect=_parity_for_candidate,
                ),
                patch.object(
                    strategy_lab,
                    "run_candidate_backtest",
                    side_effect=_backtest_for_request,
                ) as run_nautilus,
            ):
                summary = strategy_lab.run_strategy_campaign(spec, paths)
            trials = strategy_lab.StrategyLedger(
                paths.state_path / "ledger.sqlite3",
            ).campaign_trials(spec.campaign_id)

        run_nautilus.assert_called_once()
        self.assertEqual(summary["technical_invalid_count"], 1)
        self.assertEqual(summary["top_reason_codes"][0]["reason_code"], "CAMPAIGN_PREFLIGHT_DRIFT")
        self.assertEqual(trials[0].evidence.reason_codes, ("CAMPAIGN_PREFLIGHT_DRIFT",))
        self.assertTrue(trials[0].evidence.execution_started)
        self.assertIsNotNone(trials[0].evidence.experiment_id)

    def test_campaign_trial_rejects_cross_strategy_experiment_and_started_without_one(self) -> None:
        spec = self._spec()
        preflight = campaign.CampaignPreflight("e" * 64, 123, "a" * 64)
        attempt = campaign.expand_campaign(spec)[0]
        assert attempt.strategy_id is not None
        wrong_strategy_id = "9" * 64
        wrong_experiment_id = "8" * 64
        wrong_hypothesis_id = "7" * 64
        with TemporaryDirectory() as temporary:
            ledger = strategy_lab.StrategyLedger(Path(temporary) / "ledger.sqlite3")
            ledger.initialize()
            ledger.record_campaign(spec, preflight)
            with closing(sqlite3.connect(ledger.path)) as connection, connection:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute(
                    "INSERT INTO strategies VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        wrong_strategy_id,
                        spec.family_id,
                        spec.family_version,
                        _canonical({"discount_threshold": 0.0, "window_bars": 2}).decode(),
                        spec.approved_instruments[0],
                        spec.approved_bar_types[0],
                        "strategy-id-v2",
                    ),
                )
                connection.execute(
                    "INSERT INTO hypotheses VALUES (?, ?, NULL, NULL, ?, ?)",
                    (wrong_hypothesis_id, wrong_strategy_id, "/test/hypothesis.json", wrong_hypothesis_id),
                )
                connection.execute(
                    "INSERT INTO experiments VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        wrong_experiment_id,
                        wrong_hypothesis_id,
                        wrong_strategy_id,
                        "data",
                        "policy",
                        "engine",
                        "runtime",
                    ),
                )

            with self.assertRaises(sqlite3.IntegrityError):
                ledger.record_campaign_trial(
                    attempt,
                    campaign.TrialEvidence(
                        campaign.TerminalStatus.SURVIVED,
                        False,
                        ("REUSED_EXECUTION",),
                        wrong_experiment_id,
                        "6" * 64,
                    ),
                )
            with self.assertRaisesRegex(ValueError, "started execution requires experiment_id"):
                ledger.record_campaign_trial(
                    attempt,
                    campaign.TrialEvidence(
                        campaign.TerminalStatus.TECHNICAL_INVALID,
                        True,
                        ("UNRECORDED",),
                    ),
                )

    def test_candidate_data_cohort_mismatch_is_technical_before_screen_or_nautilus(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            hypothesis_path = root / "hypothesis.json"
            hypothesis_path.write_bytes(_canonical(_hypothesis()))
            paths = strategy_lab.StrategyLoopPaths(
                market_data_path=root / "market-data.json",
                policy_path=Path("config/strategy_loop_policy.json"),
                catalog_path=root / "catalog",
                funding_path=root / "funding",
                state_path=root / "state",
            )
            with (
                patch.object(strategy_lab, "_hash_tree", return_value="c" * 64),
                patch.object(strategy_lab, "_catalog_digest", return_value="a" * 64),
                patch.object(strategy_lab, "_catalog_last_timestamp", return_value=123),
                patch.object(strategy_lab, "_runtime_identity", return_value="d" * 64),
                patch.object(strategy_lab, "_engine_identity", return_value="engine-v2"),
                patch.object(strategy_lab, "_code_commit", return_value="e" * 40),
                patch.object(
                    strategy_lab,
                    "_run_bounded_process",
                    side_effect=_candidate_process(
                        {
                            "trade_count": 0,
                            "signal_count": 0,
                            "total_return": 0.0,
                            "max_drawdown": 0.0,
                            "turnover": 0.0,
                        },
                    ),
                ),
                patch.object(
                    strategy_lab,
                    "run_candidate_backtest",
                    side_effect=AssertionError("stale candidate reached Nautilus"),
                ) as run_nautilus,
            ):
                feedback = strategy_lab.run_strategy_loop(hypothesis_path, paths)

        self.assertEqual(feedback["status"], "ERROR")
        self.assertEqual(feedback["reason_codes"], ["RESEARCH_CANDIDATE_INVALID"])
        run_nautilus.assert_not_called()

    def test_standalone_claim_rejects_identity_drift_before_launch(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            hypothesis_path = root / "hypothesis.json"
            hypothesis_path.write_bytes(_canonical(_hypothesis()))
            hypothesis = strategy_lab.load_strategy_hypothesis(hypothesis_path)
            paths = strategy_lab.StrategyLoopPaths(
                root / "market.json",
                root / "policy.json",
                root / "catalog",
                root / "funding",
                root / "state",
                root / "research-policy.json",
            )
            policy = campaign.ScreenPolicy("e" * 64, "test-v1", 1, 1, 0.5, 10.0, True)
            first = strategy_lab._PreparedExecution(
                strategy_lab.ExperimentIdentity(
                    hypothesis.strategy_id,
                    "a" * 64,
                    "b" * 64,
                    "engine-v2",
                    "c" * 64,
                ),
                "d" * 64,
                policy,
                123,
                "c" * 64,
                "e" * 40,
            )
            changed = strategy_lab._PreparedExecution(
                strategy_lab.ExperimentIdentity(
                    hypothesis.strategy_id,
                    "f" * 64,
                    "b" * 64,
                    "engine-v2",
                    "c" * 64,
                ),
                "1" * 64,
                policy,
                123,
                "c" * 64,
                "e" * 40,
            )
            with (
                patch.object(strategy_lab, "_prepare_execution", side_effect=(first, changed)),
                patch.object(
                    strategy_lab,
                    "_run_strategy_loop_locked",
                    side_effect=AssertionError("drifted identity reached execution"),
                ) as execute,
                self.assertRaisesRegex(ValueError, "execution identity changed before claim"),
            ):
                strategy_lab.run_strategy_loop(hypothesis_path, paths)

        execute.assert_not_called()

    def test_candidate_runtime_attestation_mismatch_never_reaches_nautilus(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            hypothesis_path = root / "hypothesis.json"
            hypothesis_path.write_bytes(_canonical(_hypothesis()))
            paths = strategy_lab.StrategyLoopPaths(
                market_data_path=root / "market.json",
                policy_path=Path("config/strategy_loop_policy.json"),
                catalog_path=root / "catalog",
                funding_path=root / "funding",
                state_path=root / "state",
                research_policy_path=Path("config/strategy_research_policy.json"),
            )
            research_process = _candidate_process(
                {
                    "trade_count": 1,
                    "signal_count": 1,
                    "total_return": 0.1,
                    "max_drawdown": 0.1,
                    "turnover": 1.0,
                },
                candidate_environment_id="f" * 64,
                source_last_timestamp=123,
            )
            with (
                patch.object(strategy_lab, "_hash_tree", return_value="c" * 64),
                patch.object(strategy_lab, "_catalog_digest", return_value="a" * 64),
                patch.object(strategy_lab, "_catalog_last_timestamp", return_value=123),
                patch.object(strategy_lab, "_runtime_identity", return_value="d" * 64),
                patch.object(strategy_lab, "_engine_identity", return_value="engine-v2"),
                patch.object(strategy_lab, "_code_commit", return_value="e" * 40),
                patch.object(strategy_lab, "_run_bounded_process", side_effect=research_process),
                patch.object(strategy_lab, "validated_candidate_source_bars", return_value=[]),
                patch.object(strategy_lab, "run_signal_parity_gate", side_effect=_parity_for_candidate),
                patch.object(
                    strategy_lab,
                    "run_candidate_backtest",
                    side_effect=AssertionError("unbound runtime reached Nautilus"),
                ) as run_nautilus,
            ):
                feedback = strategy_lab.run_strategy_loop(hypothesis_path, paths)

        self.assertEqual(feedback["status"], "ERROR")
        self.assertEqual(feedback["reason_codes"], ["RESEARCH_CANDIDATE_INVALID"])
        run_nautilus.assert_not_called()

    def test_standalone_data_drift_after_pybroker_never_reaches_nautilus(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            hypothesis_path = root / "hypothesis.json"
            hypothesis_path.write_bytes(_canonical(_hypothesis()))
            paths = strategy_lab.StrategyLoopPaths(
                market_data_path=root / "market.json",
                policy_path=Path("config/strategy_loop_policy.json"),
                catalog_path=root / "catalog",
                funding_path=root / "funding",
                state_path=root / "state",
                research_policy_path=Path("config/strategy_research_policy.json"),
            )
            research_process = _candidate_process(
                {
                    "trade_count": 1,
                    "signal_count": 1,
                    "total_return": 0.1,
                    "max_drawdown": 0.1,
                    "turnover": 1.0,
                },
                source_last_timestamp=123,
            )
            with (
                patch.object(
                    strategy_lab,
                    "_hash_tree",
                    side_effect=("a" * 64,) * 4 + ("b" * 64,) * 8,
                ),
                patch.object(strategy_lab, "_catalog_digest", return_value="a" * 64),
                patch.object(strategy_lab, "_catalog_last_timestamp", return_value=123),
                patch.object(strategy_lab, "_runtime_identity", return_value="d" * 64),
                patch.object(strategy_lab, "_engine_identity", return_value="engine-v2"),
                patch.object(strategy_lab, "_code_commit", return_value="e" * 40),
                patch.object(strategy_lab, "_run_bounded_process", side_effect=research_process),
                patch.object(strategy_lab, "validated_candidate_source_bars", return_value=[]),
                patch.object(strategy_lab, "run_signal_parity_gate", side_effect=_parity_for_candidate),
                patch.object(
                    strategy_lab,
                    "run_candidate_backtest",
                    side_effect=AssertionError("drifted standalone reached Nautilus"),
                ) as run_nautilus,
            ):
                feedback = strategy_lab.run_strategy_loop(hypothesis_path, paths)

        self.assertEqual(feedback["status"], "ERROR")
        run_nautilus.assert_not_called()

    def test_existing_nonterminal_experiment_never_relaunches_execution(self) -> None:
        spec = self._spec()
        attempt = campaign.expand_campaign(spec)[0]
        assert attempt.strategy_id is not None
        policy = campaign.ScreenPolicy("e" * 64, "test-v1", 1, 1, 0.5, 10.0, True)
        identity = strategy_lab.ExperimentIdentity(
            attempt.strategy_id,
            "a" * 64,
            "b" * 64,
            "engine-v2",
            "c" * 64,
            123,
            "a" * 64,
        )
        prepared = strategy_lab._PreparedExecution(
            identity,
            "d" * 64,
            policy,
            123,
            "c" * 64,
            "e" * 40,
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = strategy_lab.StrategyLoopPaths(
                market_data_path=root / "market-data.json",
                policy_path=Path("config/strategy_loop_policy.json"),
                catalog_path=root / "catalog",
                funding_path=root / "funding",
                state_path=root / "state",
                research_policy_path=Path("config/strategy_research_policy.json"),
            )
            ledger = strategy_lab.StrategyLedger(paths.state_path / "ledger.sqlite3")
            ledger.initialize()
            hypothesis_id = "f" * 64
            with closing(sqlite3.connect(ledger.path)) as connection, connection:
                connection.execute(
                    "INSERT INTO strategies VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        attempt.strategy_id,
                        spec.family_id,
                        spec.family_version,
                        _canonical(attempt.parameters).decode(),
                        spec.approved_instruments[0],
                        spec.approved_bar_types[0],
                        "strategy-id-v2",
                    ),
                )
                connection.execute(
                    "INSERT INTO hypotheses VALUES (?, ?, NULL, NULL, ?, ?)",
                    (hypothesis_id, attempt.strategy_id, "/test/hypothesis.json", hypothesis_id),
                )
                connection.execute(
                    "INSERT INTO experiments VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        strategy_lab._experiment_id(identity),
                        hypothesis_id,
                        identity.strategy_id,
                        identity.data_source_id,
                        identity.policy_id,
                        identity.engine_id,
                        identity.runtime_id,
                    ),
                )
                connection.execute(
                    "INSERT INTO experiment_sources VALUES (?, ?, ?)",
                    (
                        strategy_lab._experiment_id(identity),
                        identity.data_snapshot_id,
                        identity.data_as_of_ns,
                    ),
                )
            reason_code = None
            poisoned_experiment_id = None
            with (
                patch.object(strategy_lab, "_prepare_execution", return_value=prepared),
                patch.object(
                    strategy_lab,
                    "_run_bounded_process",
                    side_effect=AssertionError("nonterminal campaign relaunched PyBroker"),
                ) as run_process,
            ):
                try:
                    strategy_lab._campaign_execute(
                        spec,
                        paths,
                        ledger,
                        campaign.CampaignPreflight("e" * 64, 123, "a" * 64),
                        attempt,
                    )
                except campaign.CampaignTechnicalError as error:
                    reason_code = error.reason_code
                    poisoned_experiment_id = error.experiment_id

        self.assertEqual(reason_code, "CAMPAIGN_EXISTING_NONTERMINAL")
        self.assertIsNone(poisoned_experiment_id)
        run_process.assert_not_called()

    def test_standalone_nonterminal_experiment_never_relaunches_execution(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            hypothesis_path = root / "hypothesis.json"
            hypothesis_path.write_bytes(_canonical(_hypothesis()))
            hypothesis = strategy_lab.load_strategy_hypothesis(hypothesis_path)
            identity = strategy_lab.ExperimentIdentity(
                hypothesis.strategy_id,
                "a" * 64,
                "b" * 64,
                "engine-v2",
                "c" * 64,
                123,
                "a" * 64,
            )
            prepared = strategy_lab._PreparedExecution(
                identity,
                "d" * 64,
                campaign.ScreenPolicy("e" * 64, "test-v1", 1, 1, 0.5, 10.0, True),
                123,
                "c" * 64,
                "f" * 40,
            )
            paths = strategy_lab.StrategyLoopPaths(
                market_data_path=root / "market-data.json",
                policy_path=Path("config/strategy_loop_policy.json"),
                catalog_path=root / "catalog",
                funding_path=root / "funding",
                state_path=root / "state",
                research_policy_path=Path("config/strategy_research_policy.json"),
            )
            ledger = strategy_lab.StrategyLedger(paths.state_path / "ledger.sqlite3")
            ledger.initialize()
            ledger.record_hypothesis(hypothesis)
            ledger.record_experiment(hypothesis.hypothesis_id, identity)
            with (
                patch.object(strategy_lab, "_prepare_execution", return_value=prepared),
                patch.object(
                    strategy_lab,
                    "_run_bounded_process",
                    side_effect=AssertionError("standalone nonterminal relaunched PyBroker"),
                ) as run_process,
                self.assertRaisesRegex(
                    strategy_lab.StrategyLabError,
                    "existing experiment is non-terminal",
                ),
            ):
                strategy_lab.run_strategy_loop(hypothesis_path, paths)

        run_process.assert_not_called()

    def test_late_preflight_drift_marks_the_entire_unpersisted_cohort_technical(self) -> None:
        spec = self._spec(thresholds=(0.0, 0.1))
        attempts = campaign.expand_campaign(spec)
        with TemporaryDirectory() as temporary:
            ledger = strategy_lab.StrategyLedger(Path(temporary) / "ledger.sqlite3")
            ledger.initialize()
            experiment_id, candidate_id = self._insert_stub_experiment(
                ledger,
                attempts[0],
                "cohort-drift",
            )
            calls = 0

            def execute(attempt: campaign.CampaignAttempt) -> campaign.TrialEvidence:
                nonlocal calls
                self.assertEqual(attempt, attempts[calls])
                calls += 1
                if calls == 1:
                    return campaign.TrialEvidence(
                        campaign.TerminalStatus.SCREEN_REJECTED,
                        True,
                        ("NO_SIGNALS",),
                        experiment_id,
                        candidate_id,
                    )
                raise campaign.CampaignTechnicalError("CAMPAIGN_PREFLIGHT_DRIFT", False)

            summary = campaign.run_campaign(
                spec,
                ledger=ledger,
                preflight=campaign.CampaignPreflight("e" * 64, 123, "a" * 64),
                execute=execute,
            )
            trials = ledger.campaign_trials(spec.campaign_id)

        self.assertEqual(summary["technical_invalid_count"], 2)
        self.assertEqual(
            [trial.evidence.reason_codes for trial in trials],
            [("CAMPAIGN_PREFLIGHT_DRIFT",), ("CAMPAIGN_PREFLIGHT_DRIFT",)],
        )

    def test_research_screen_error_restores_terminal_technical_evidence(self) -> None:
        identity = strategy_lab.ExperimentIdentity(
            "1" * 64,
            "2" * 64,
            "3" * 64,
            "engine-v2",
            "4" * 64,
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = strategy_lab.StrategyLedger(root / "ledger.sqlite3")
            ledger.initialize()
            experiment_id = "5" * 64
            error_path = root / "research-error.json"
            error_path.write_bytes(
                _canonical(
                    {
                        "detail": "invalid provisional result",
                        "experiment_id": experiment_id,
                        "reason_code": "RESEARCH_RESULT_INVALID",
                        "schema_version": "strategy-loop-error-v1",
                        "stage": "RESEARCH",
                    },
                ),
            )
            artifact_hash = sha256(error_path.read_bytes()).hexdigest()
            with closing(sqlite3.connect(ledger.path)) as connection, connection:
                connection.execute(
                    "INSERT INTO experiments VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        experiment_id,
                        "6" * 64,
                        identity.strategy_id,
                        identity.data_source_id,
                        identity.policy_id,
                        identity.engine_id,
                        identity.runtime_id,
                    ),
                )
                connection.execute(
                    "INSERT INTO stage_results VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        experiment_id,
                        identity.strategy_id,
                        "Research screened",
                        "ERROR",
                        "RESEARCH_RESULT_INVALID",
                        str(error_path),
                        artifact_hash,
                    ),
                )
                connection.execute(
                    "INSERT INTO errors VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        "7" * 64,
                        experiment_id,
                        "RESEARCH",
                        "RESEARCH_RESULT_INVALID",
                        str(error_path),
                        artifact_hash,
                    ),
                )

            evidence = ledger.existing_execution(identity, None)

        self.assertIsNotNone(evidence)
        assert evidence is not None
        self.assertEqual(evidence.terminal_status, campaign.TerminalStatus.TECHNICAL_INVALID)
        self.assertEqual(evidence.reason_codes, ("RESEARCH_RESULT_INVALID",))

    def test_campaign_preflight_drift_cannot_mask_started_terminal_execution(self) -> None:
        identity = strategy_lab.ExperimentIdentity(
            "1" * 64,
            "2" * 64,
            "3" * 64,
            "engine-v2",
            "4" * 64,
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = strategy_lab.StrategyLedger(root / "ledger.sqlite3")
            ledger.initialize()
            policy, _candidate_path, _screen_path, candidate_id = self._cached_screen_fixture(
                root,
                ledger,
                identity,
            )
            trial = campaign.CampaignTrial(
                "8" * 64,
                0,
                identity.strategy_id,
                candidate_id,
                campaign.TrialEvidence(
                    campaign.TerminalStatus.TECHNICAL_INVALID,
                    False,
                    ("CAMPAIGN_PREFLIGHT_DRIFT",),
                    "5" * 64,
                    candidate_id,
                ),
            )

            with self.assertRaisesRegex(ValueError, "execution_started mismatch"):
                ledger.verify_campaign_trial(trial, policy)

    def test_unexpected_executor_exception_becomes_terminal_technical_evidence(self) -> None:
        spec = self._spec()
        with TemporaryDirectory() as temporary:
            ledger = strategy_lab.StrategyLedger(Path(temporary) / "ledger.sqlite3")
            ledger.initialize()

            summary = campaign.run_campaign(
                spec,
                ledger=ledger,
                preflight=campaign.CampaignPreflight("e" * 64, 123, "a" * 64),
                execute=lambda _attempt: (_ for _ in ()).throw(RuntimeError("boom")),
            )

            stored = ledger.campaign_trials(spec.campaign_id)

        self.assertEqual(summary["technical_invalid_count"], 1)
        self.assertEqual(stored[0].evidence.reason_codes, ("CAMPAIGN_EXECUTOR_FAILED",))
        self.assertFalse(stored[0].evidence.execution_started)

    def test_campaign_waiter_records_competing_completion_as_reuse(self) -> None:
        spec = self._spec()
        attempt = campaign.expand_campaign(spec)[0]
        assert attempt.strategy_id is not None
        policy = campaign.ScreenPolicy(
            policy_id="e" * 64,
            policy_version="test-screen-v1",
            minimum_trade_count=1,
            minimum_signal_count=1,
            max_provisional_drawdown=0.5,
            max_turnover=10.0,
            reject_no_signal=True,
        )
        identity = strategy_lab.ExperimentIdentity(
            attempt.strategy_id,
            "a" * 64,
            "b" * 64,
            "engine-v2",
            "d" * 64,
        )
        prepared = strategy_lab._PreparedExecution(
            identity,
            "c" * 64,
            policy,
            123,
            "d" * 64,
            "e" * 40,
        )
        experiment_id = strategy_lab._experiment_id(identity)
        prior = campaign.TrialEvidence(
            campaign.TerminalStatus.SURVIVED,
            True,
            ("SCREEN_PASSED",),
            experiment_id,
            "f" * 64,
        )
        ledger = Mock()
        ledger.existing_execution.return_value = prior
        ledger.existing_experiment_id.return_value = None

        def completed_by_competitor(
            _hypothesis_path: Path,
            _paths: strategy_lab.StrategyLoopPaths,
            **kwargs: object,
        ) -> dict[str, object]:
            claim = kwargs.get("_claim")
            if claim is not None:
                setattr(claim, "prepared", prepared)
                setattr(claim, "reused", True)
            require_prepared = kwargs.get("_require_prepared")
            if callable(require_prepared):
                require_prepared(prepared)
            return {"experiment_id": experiment_id}

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = strategy_lab.StrategyLoopPaths(
                root / "market.json",
                root / "policy.json",
                root / "catalog",
                root / "funding",
                root / "state",
                root / "research-policy.json",
            )
            with (
                patch.object(strategy_lab, "_prepare_execution", return_value=prepared),
                patch.object(strategy_lab, "run_strategy_loop", side_effect=completed_by_competitor),
            ):
                evidence = strategy_lab._campaign_execute(
                    spec,
                    paths,
                    ledger,
                    campaign.CampaignPreflight("e" * 64, 123, "a" * 64),
                    attempt,
                )

        self.assertFalse(evidence.execution_started)
        self.assertEqual(evidence.reason_codes, ("SCREEN_PASSED", "REUSED_EXECUTION"))

    def test_campaign_direct_reuse_revalidates_the_full_prepared_snapshot(self) -> None:
        spec = self._spec()
        attempt = campaign.expand_campaign(spec)[0]
        assert attempt.strategy_id is not None
        policy = campaign.ScreenPolicy("e" * 64, "test-v1", 1, 1, 0.5, 10.0, True)
        first = strategy_lab._PreparedExecution(
            strategy_lab.ExperimentIdentity(
                attempt.strategy_id,
                "a" * 64,
                "b" * 64,
                "engine-v2",
                "c" * 64,
            ),
            "d" * 64,
            policy,
            123,
            "c" * 64,
            "e" * 40,
        )
        drifted = strategy_lab._PreparedExecution(
            strategy_lab.ExperimentIdentity(
                attempt.strategy_id,
                "a" * 64,
                "f" * 64,
                "engine-v2-drifted",
                "1" * 64,
            ),
            "2" * 64,
            policy,
            123,
            "1" * 64,
            "3" * 40,
        )
        ledger = Mock()
        ledger.existing_execution.return_value = campaign.TrialEvidence(
            campaign.TerminalStatus.SURVIVED,
            True,
            ("SCREEN_PASSED",),
            strategy_lab._experiment_id(first.identity),
            "4" * 64,
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = strategy_lab.StrategyLoopPaths(
                root / "market.json",
                root / "policy.json",
                root / "catalog",
                root / "funding",
                root / "state",
                root / "research-policy.json",
            )
            with (
                patch.object(strategy_lab, "_prepare_execution", side_effect=(first, drifted)),
                self.assertRaises(campaign.CampaignTechnicalError) as raised,
            ):
                strategy_lab._campaign_reuse_lookup(
                    spec,
                    paths,
                    ledger,
                    campaign.CampaignPreflight("e" * 64, 123, "a" * 64),
                    attempt,
                )

        self.assertEqual(raised.exception.reason_code, "CAMPAIGN_PREFLIGHT_DRIFT")

    def test_ledger_forbids_verdict_and_error_for_one_experiment(self) -> None:
        spec = self._spec()
        attempt = campaign.expand_campaign(spec)[0]
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = strategy_lab.StrategyLedger(root / "ledger.sqlite3")
            ledger.initialize()
            experiment_id, _candidate_id = self._insert_stub_experiment(
                ledger,
                attempt,
                "terminal-conflict",
                identity_schema="strategy-id-v1",
            )
            verdict_path = root / "verdict.json"
            verdict_path.write_bytes(
                _canonical({"decision": "RETAIN_FOR_RESEARCH", "reason_codes": ["VALID"]}),
            )
            error_path = root / "error.json"
            error_path.write_bytes(_canonical({"detail": "contradictory"}))
            ledger.record_verdict(
                strategy_lab.VerdictRecord(
                    experiment_id,
                    "SUCCESS",
                    "VALID",
                    str(verdict_path),
                    sha256(verdict_path.read_bytes()).hexdigest(),
                ),
            )

            with self.assertRaises(sqlite3.IntegrityError):
                ledger.record_error(
                    strategy_lab.ErrorRecord(
                        experiment_id,
                        "RESEARCH",
                        "CONTRADICTORY",
                        str(error_path),
                        sha256(error_path.read_bytes()).hexdigest(),
                    ),
                )

    def test_ledger_forbids_error_then_verdict_for_one_experiment(self) -> None:
        spec = self._spec()
        attempt = campaign.expand_campaign(spec)[0]
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = strategy_lab.StrategyLedger(root / "ledger.sqlite3")
            ledger.initialize()
            experiment_id, _candidate_id = self._insert_stub_experiment(
                ledger,
                attempt,
                "reverse-terminal-conflict",
                identity_schema="strategy-id-v1",
            )
            error_path = root / "error.json"
            error_path.write_bytes(_canonical({"detail": "terminal error"}))
            verdict_path = root / "verdict.json"
            verdict_path.write_bytes(
                _canonical({"decision": "RETAIN_FOR_RESEARCH", "reason_codes": ["VALID"]}),
            )
            ledger.record_error(
                strategy_lab.ErrorRecord(
                    experiment_id,
                    "RESEARCH",
                    "TECHNICAL",
                    str(error_path),
                    sha256(error_path.read_bytes()).hexdigest(),
                ),
            )

            with self.assertRaises(sqlite3.IntegrityError):
                ledger.record_verdict(
                    strategy_lab.VerdictRecord(
                        experiment_id,
                        "SUCCESS",
                        "VALID",
                        str(verdict_path),
                        sha256(verdict_path.read_bytes()).hexdigest(),
                    ),
                )

    def test_existing_feedback_rejects_legacy_verdict_error_conflict(self) -> None:
        spec = self._spec()
        attempt = campaign.expand_campaign(spec)[0]
        assert attempt.strategy_id is not None
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = strategy_lab.StrategyLedger(root / "ledger.sqlite3")
            ledger.initialize()
            experiment_id, _candidate_id = self._insert_stub_experiment(
                ledger,
                attempt,
                "legacy-terminal-conflict",
            )
            verdict_path = root / "verdict.json"
            verdict_path.write_bytes(
                _canonical({"decision": "RETAIN_FOR_RESEARCH", "reason_codes": ["VALID"]}),
            )
            error_path = root / "error.json"
            error_path.write_bytes(_canonical({"detail": "contradictory"}))
            with closing(sqlite3.connect(ledger.path)) as connection, connection:
                connection.execute("DROP TRIGGER IF EXISTS errors_reject_existing_verdict")
                connection.execute("DROP TRIGGER IF EXISTS verdicts_reject_existing_error")
                connection.execute(
                    "INSERT INTO verdicts VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        "1" * 64,
                        experiment_id,
                        attempt.strategy_id,
                        "SUCCESS",
                        "VALID",
                        str(verdict_path),
                        sha256(verdict_path.read_bytes()).hexdigest(),
                    ),
                )
                connection.execute(
                    "INSERT INTO errors VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        "2" * 64,
                        experiment_id,
                        "RESEARCH",
                        "CONTRADICTORY",
                        str(error_path),
                        sha256(error_path.read_bytes()).hexdigest(),
                    ),
                )
            hypothesis = strategy_lab.StrategyHypothesis(
                root / "hypothesis.json",
                None,
                None,
                "thesis",
                "falsification",
                strategy_lab.StrategyParameters(attempt.parameters),
                spec.family_id,
                spec.family_version,
                "strategy-id-v2",
                attempt.strategy_id,
                "3" * 64,
            )

            with self.assertRaisesRegex(ValueError, "contradictory terminal evidence"):
                strategy_lab._existing_feedback(
                    ledger,
                    experiment_id,
                    root / "feedback.json",
                    hypothesis,
                )

if __name__ == "__main__":
    unittest.main()
