from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch
import json
import unittest

from nautilus_quant.binance_public import BinancePublicClient
from nautilus_quant.cli import main
from nautilus_quant.funding_observation import migrate_funding_observations


def write_valid_config(project: Path) -> Path:
    config = project / "config/market_data.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps({
            "base_url": "https://fapi.binance.com",
            "start": "2021-01-01T00:00:00Z",
            "symbols": ["BTCUSDT"],
            "intervals": ["5m"],
            "datasets": ["trade"],
            "chunk_days": 30,
            "catalog_path": "data/catalog",
            "funding_path": "data/funding",
        }),
        encoding="utf-8",
    )
    return config


class CliTests(unittest.TestCase):
    def test_migrate_funding_observations_uses_data_sync_lock_and_config_boundaries(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            config = write_valid_config(project)
            raw = json.loads(config.read_text(encoding="utf-8"))
            raw.update({"datasets": ["trade", "funding"], "symbols": ["BTCUSDT", "ETHUSDT"]})
            config.write_text(json.dumps(raw), encoding="utf-8")
            locks = []

            @contextmanager
            def recording_lock(path: Path):
                locks.append(path)
                yield

            output = StringIO()
            with (
                patch("nautilus_quant.cli.PROJECT_ROOT", project),
                patch("nautilus_quant.cli.single_writer", side_effect=recording_lock),
                patch("nautilus_quant.cli.BinancePublicClient") as client,
                patch(
                    "nautilus_quant.cli.migrate_funding_observations",
                    return_value={"status": "READY", "schema_version": 1},
                ) as migrate,
                redirect_stdout(output),
            ):
                result = main([
                    "migrate-funding-observations",
                    "--config",
                    str(config),
                    "--now",
                    "2021-01-02T00:00:00Z",
                ])

            self.assertEqual(result, 0)
            self.assertEqual(locks, [project.resolve() / "var/locks/data-sync.lock"])
            migrate.assert_called_once_with(
                client=client.return_value,
                funding_path=(project / "data/funding").resolve(),
                symbols=("BTCUSDT", "ETHUSDT"),
                start_ms=1_609_459_200_000,
                end_ms=1_609_545_600_000,
            )
            self.assertEqual(json.loads(output.getvalue())["status"], "READY")

    def test_candidate_funding_sync_uses_data_sync_lock_without_live_cutover(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            config = write_valid_config(project)
            raw = json.loads(config.read_text(encoding="utf-8"))
            raw.update({"datasets": ["trade", "funding"], "symbols": ["BTCUSDT"]})
            config.write_text(json.dumps(raw), encoding="utf-8")
            locks = []

            @contextmanager
            def recording_lock(path: Path):
                locks.append(path)
                yield

            with (
                patch("nautilus_quant.cli.PROJECT_ROOT", project),
                patch("nautilus_quant.cli.single_writer", side_effect=recording_lock),
                patch("nautilus_quant.cli.BinancePublicClient") as client,
                patch(
                    "nautilus_quant.cli.sync_funding_generation",
                    return_value={"status": "READY", "schema_version": 1},
                ) as candidate_sync,
                redirect_stdout(StringIO()),
            ):
                result = main([
                    "sync-funding-observations-candidate",
                    "--config",
                    str(config),
                    "--now",
                    "2021-01-02T00:00:00Z",
                ])

            self.assertEqual(result, 0)
            self.assertEqual(locks, [project.resolve() / "var/locks/data-sync.lock"])
            candidate_sync.assert_called_once_with(
                client=client.return_value,
                funding_path=(project / "data/funding").resolve(),
                symbols=("BTCUSDT",),
                start_ms=1_609_459_200_000,
                end_ms=1_609_545_600_000,
            )

    def test_failure_report_retains_read_back_reconstruction_across_resume(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            config = write_valid_config(project)
            raw = json.loads(config.read_text(encoding="utf-8"))
            raw.update({"intervals": ["15m"], "chunk_days": 1})
            config.write_text(json.dumps(raw), encoding="utf-8")
            start_ms = 1_609_459_200_000
            step_ms = 15 * 60_000
            day_ms = 24 * 60 * 60_000
            missing_ms = start_ms + step_ms
            calls = []
            test_case = self

            class LaterFailureClient(BinancePublicClient):
                def __init__(self):
                    super().__init__(request_delay=0)

                def request_json(self, path: str, params: dict[str, object]) -> list[object] | dict[str, object]:
                    interval = str(params["interval"])
                    cursor_ms = int(cast(int, params["startTime"]))
                    calls.append((path, interval, cursor_ms))
                    if cursor_ms >= start_ms + day_ms:
                        raise RuntimeError("later chunk failed")
                    if interval == "1m":
                        test_case.assertEqual(cursor_ms, missing_ms)
                        return [
                            [open_ms, "1", "2", "0.5", "1.5", "1", open_ms + 59_999]
                            for open_ms in range(missing_ms, missing_ms + step_ms, 60_000)
                        ]
                    test_case.assertEqual(interval, "15m")
                    end_ms = int(cast(int, params["endTime"])) + 1
                    return [
                        [open_ms, "1", "2", "0.5", "1.5", "1", open_ms + step_ms - 1]
                        for open_ms in range(cursor_ms, end_ms, step_ms)
                        if open_ms != missing_ms
                    ]

            async def load_perpetuals(_symbols):
                return [SimpleNamespace(id="BTCUSDT-PERP.BINANCE", price_precision=2, size_precision=3)]

            with (
                patch("nautilus_quant.cli.PROJECT_ROOT", project),
                patch("nautilus_quant.service.BinancePublicClient", return_value=LaterFailureClient()),
                patch("nautilus_quant.service.load_perpetuals", side_effect=load_perpetuals),
                patch("nautilus_quant.service.ensure_instruments", return_value=0),
                redirect_stdout(StringIO()),
                redirect_stderr(StringIO()),
            ):
                args = ["sync", "--config", str(config), "--now", "2021-01-03T00:00:00Z"]
                self.assertEqual(main(args), 1)
                self.assertEqual(main(args), 1)

            reports = sorted((project / "var/runs").glob("sync-*.json"))
            self.assertEqual(len(reports), 2)
            first = json.loads(reports[0].read_text(encoding="utf-8"))
            second = json.loads(reports[1].read_text(encoding="utf-8"))
            expected = [{
                "dataset": "trade",
                "symbol": "BTCUSDT",
                "interval": "15m",
                "bar_type": "BTCUSDT-PERP.BINANCE-15-MINUTE-LAST-EXTERNAL",
                "reconstructed_open_ms": [missing_ms],
            }]
            self.assertEqual(first["reconstructed_chunks"], expected)
            self.assertEqual(second["reconstructed_chunks"], [])
            self.assertEqual(
                json.loads(reports[0].read_text(encoding="utf-8"))["reconstructed_chunks"],
                expected,
            )
            self.assertEqual(
                [(interval, cursor_ms) for _path, interval, cursor_ms in calls],
                [("15m", start_ms), ("1m", missing_ms), ("15m", start_ms + day_ms), ("15m", start_ms + day_ms)],
            )

    def test_sync_config_failure_writes_fail_report(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            config = project / "config/market_data.json"
            config.parent.mkdir()
            config.write_text("{not-json", encoding="utf-8")

            with (
                patch("nautilus_quant.cli.PROJECT_ROOT", project),
                redirect_stdout(StringIO()),
                redirect_stderr(StringIO()),
            ):
                result = main(["sync", "--config", str(config)])

            self.assertEqual(result, 1)
            reports = list((project / "var/runs").glob("sync-*.json"))
            self.assertEqual(len(reports), 1)
            report = json.loads(reports[0].read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "FAIL")
            self.assertEqual(report["error_type"], "JSONDecodeError")

    def test_sync_rejects_config_outside_project_and_reports_inside_project(self):
        with TemporaryDirectory() as project_tmp, TemporaryDirectory() as external_tmp:
            project = Path(project_tmp)
            external = Path(external_tmp)
            config = external / "config/market_data.json"
            config.parent.mkdir()
            config.write_text("{}", encoding="utf-8")

            with (
                patch("nautilus_quant.cli.PROJECT_ROOT", project),
                redirect_stdout(StringIO()),
                redirect_stderr(StringIO()),
            ):
                result = main(["sync", "--config", str(config)])

            self.assertEqual(result, 1)
            reports = list((project / "var/runs").glob("sync-*.json"))
            self.assertEqual(len(reports), 1)
            report = json.loads(reports[0].read_text(encoding="utf-8"))
            self.assertEqual(report["error_type"], "ValueError")
            self.assertIn("config path must stay within the project root", report["error"])
            self.assertFalse((external / "var/runs").exists())

    def test_status_ignores_report_for_different_config_scope_same_paths(self):
        for field, value in (
            ("start", "2022-08-01T00:00:00Z"),
            ("symbols", ["ETHUSDT"]),
            ("intervals", ["15m"]),
            ("datasets", ["trade", "funding"]),
        ):
            with self.subTest(field=field), TemporaryDirectory() as tmp:
                project = Path(tmp)
                config = write_valid_config(project)

                with (
                    patch("nautilus_quant.cli.PROJECT_ROOT", project),
                    patch("nautilus_quant.cli.run_sync", side_effect=RuntimeError("old scope failed")),
                    redirect_stdout(StringIO()),
                    redirect_stderr(StringIO()),
                ):
                    self.assertEqual(main(["sync", "--config", str(config)]), 1)

                raw = json.loads(config.read_text(encoding="utf-8"))
                raw[field] = value
                config.write_text(json.dumps(raw), encoding="utf-8")
                output = StringIO()

                with (
                    patch("nautilus_quant.cli.PROJECT_ROOT", project),
                    patch(
                        "nautilus_quant.cli.read_funding_status",
                        return_value={"status": "READY", "generation": "a" * 64},
                    ),
                    redirect_stdout(output),
                ):
                    result = main(["status", "--config", str(config)])

                event = json.loads(output.getvalue())
                self.assertEqual(result, 0)
                self.assertEqual(event["status"], "NO_RUN")
                self.assertIsNone(event["latest_report"])

    def test_status_ignores_historical_report_with_retired_reference_scope(self):
        for dataset in ("premium", "mark", "index"):
            with self.subTest(dataset=dataset), TemporaryDirectory() as tmp:
                project = Path(tmp)
                config = write_valid_config(project)
                run_dir = project / "var/runs"
                run_dir.mkdir(parents=True)
                (run_dir / "sync-20260811T000000.000000Z.json").write_text(
                    json.dumps({
                        "status": "FAIL",
                        "catalog_path": str((project / "data/catalog").resolve()),
                        "funding_path": str((project / "data/funding").resolve()),
                        "config_scope": {
                            "base_url": "https://fapi.binance.com",
                            "start": "2021-01-01T00:00:00+00:00",
                            "symbols": ["BTCUSDT"],
                            "intervals": ["5m"],
                            "datasets": ["trade", dataset],
                        },
                    }),
                    encoding="utf-8",
                )
                output = StringIO()

                with patch("nautilus_quant.cli.PROJECT_ROOT", project), redirect_stdout(output):
                    result = main(["status", "--config", str(config)])

                event = json.loads(output.getvalue())
                self.assertEqual(result, 0)
                self.assertEqual(event["status"], "NO_RUN")
                self.assertIsNone(event["latest_report"])

    def test_sync_success_report_persists_scope_and_is_visible_to_status(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            config = write_valid_config(project)
            expected_scope = {
                "base_url": "https://fapi.binance.com",
                "start": "2021-01-01T00:00:00+00:00",
                "symbols": ["BTCUSDT"],
                "intervals": ["5m"],
                "datasets": ["trade"],
            }
            report = {
                "status": "PASS",
                "catalog_path": str((project / "data/catalog").resolve()),
                "funding_path": str((project / "data/funding").resolve()),
            }

            with (
                patch("nautilus_quant.cli.PROJECT_ROOT", project),
                patch("nautilus_quant.cli.run_sync", return_value=report),
                redirect_stdout(StringIO()),
            ):
                self.assertEqual(main(["sync", "--config", str(config)]), 0)

            output = StringIO()
            with patch("nautilus_quant.cli.PROJECT_ROOT", project), redirect_stdout(output):
                self.assertEqual(main(["status", "--config", str(config)]), 0)

            event = json.loads(output.getvalue())
            self.assertEqual(event["status"], "PASS")
            self.assertEqual(event["latest_report"]["config_scope"], expected_scope)

    def test_status_binds_matching_report_to_ready_funding_generation(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            config = write_valid_config(project)
            raw = json.loads(config.read_text(encoding="utf-8"))
            raw["datasets"] = ["trade", "funding"]
            config.write_text(json.dumps(raw), encoding="utf-8")
            report = {
                "status": "PASS",
                "catalog_path": str((project / "data/catalog").resolve()),
                "funding_path": str((project / "data/funding").resolve()),
                "funding_generation": "a" * 64,
            }
            funding_status = {"status": "READY", "generation": "a" * 64}

            with (
                patch("nautilus_quant.cli.PROJECT_ROOT", project),
                patch("nautilus_quant.cli.run_sync", return_value=report),
                redirect_stdout(StringIO()),
            ):
                self.assertEqual(main(["sync", "--config", str(config)]), 0)

            output = StringIO()
            with (
                patch("nautilus_quant.cli.PROJECT_ROOT", project),
                patch("nautilus_quant.cli.read_funding_status", return_value=funding_status) as read_status,
                redirect_stdout(output),
            ):
                self.assertEqual(main(["status", "--config", str(config)]), 0)

            event = json.loads(output.getvalue())
            read_status.assert_called_once_with((project / "data/funding").resolve(), symbols=("BTCUSDT",))
            self.assertEqual(event["funding_canonical"], funding_status)

            output = StringIO()
            errors = StringIO()
            with (
                patch("nautilus_quant.cli.PROJECT_ROOT", project),
                patch(
                    "nautilus_quant.cli.read_funding_status",
                    return_value={"status": "READY", "generation": "b" * 64},
                ),
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                self.assertEqual(main(["status", "--config", str(config)]), 1)
            self.assertEqual(output.getvalue(), "")
            self.assertIn("generation mismatch", json.loads(errors.getvalue().splitlines()[0])["error"])

    def test_status_rejects_pre_cutover_pass_report_without_funding_generation(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            config = write_valid_config(project)
            raw = json.loads(config.read_text(encoding="utf-8"))
            raw["datasets"] = ["trade", "funding"]
            config.write_text(json.dumps(raw), encoding="utf-8")
            start_ms = 1_609_459_200_000

            class FundingClient:
                def funding(
                    self,
                    symbol: str,
                    start_ms: int,
                    end_ms: int,
                ) -> list[dict[str, object]]:
                    return [
                        {
                            "symbol": symbol,
                            "fundingTime": timestamp,
                            "fundingRate": "0.0001",
                            "markPrice": "1000",
                        }
                        for timestamp in (start_ms, start_ms + 8 * 60 * 60_000, start_ms + 16 * 60 * 60_000)
                        if start_ms <= timestamp < end_ms
                    ]

            migrate_funding_observations(
                client=FundingClient(),
                funding_path=project / "data/funding",
                symbols=("BTCUSDT",),
                start_ms=start_ms,
                end_ms=start_ms + 24 * 60 * 60_000,
            )
            run_dir = project / "var/runs"
            run_dir.mkdir(parents=True)
            (run_dir / "sync-20260813T021534.612879Z.json").write_text(json.dumps({
                "status": "PASS",
                "catalog_path": str((project / "data/catalog").resolve()),
                "funding_path": str((project / "data/funding").resolve()),
                "config_scope": {
                    "base_url": "https://fapi.binance.com",
                    "start": "2021-01-01T00:00:00+00:00",
                    "symbols": ["BTCUSDT"],
                    "intervals": ["5m"],
                    "datasets": ["trade", "funding"],
                },
            }), encoding="utf-8")

            output = StringIO()
            errors = StringIO()
            with (
                patch("nautilus_quant.cli.PROJECT_ROOT", project),
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                result = main(["status", "--config", str(config)])

            self.assertEqual(result, 1)
            self.assertEqual(output.getvalue(), "")
            self.assertIn("generation", json.loads(errors.getvalue().splitlines()[0])["error"])

    def test_status_ignores_latest_report_for_different_data_paths(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            config = write_valid_config(project)
            run_dir = project / "var/runs"
            run_dir.mkdir(parents=True)
            (run_dir / "sync-20260811T000000.000000Z.json").write_text(
                json.dumps({
                    "status": "PASS",
                    "catalog_path": str(project / ".local/catalog"),
                    "funding_path": str(project / ".local/funding"),
                }),
                encoding="utf-8",
            )
            output = StringIO()

            with patch("nautilus_quant.cli.PROJECT_ROOT", project), redirect_stdout(output):
                result = main(["status", "--config", str(config)])

            event = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(event["status"], "NO_RUN")
            self.assertIsNone(event["latest_report"])

    def test_status_fails_closed_on_malformed_report(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            config = write_valid_config(project)
            run_dir = project / "var/runs"
            run_dir.mkdir(parents=True)
            (run_dir / "sync-20260811T000000.000000Z.json").write_text("{broken", encoding="utf-8")
            output = StringIO()
            errors = StringIO()

            with (
                patch("nautilus_quant.cli.PROJECT_ROOT", project),
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                result = main(["status", "--config", str(config)])

            event = json.loads(errors.getvalue().splitlines()[0])
            self.assertEqual(result, 1)
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(event["status"], "FAIL")
            self.assertEqual(event["error_type"], "JSONDecodeError")

    def test_status_fails_closed_on_structurally_invalid_report(self):
        for invalid in (
            {"status": "FAIL"},
            {
                "status": "UNKNOWN",
                "catalog_path": "data/catalog",
                "funding_path": "data/funding",
            },
            {
                "status": "FAIL",
                "catalog_path": "data/catalog",
                "funding_path": "data/funding",
                "config_scope": None,
            },
            {
                "status": "FAIL",
                "catalog_path": "data/catalog",
                "funding_path": "data/funding",
                "config_scope": {},
            },
            {
                "status": "FAIL",
                "catalog_path": "data/catalog",
                "funding_path": "data/funding",
                "config_scope": {
                    "base_url": "https://fapi.binance.com",
                    "start": "not-a-timestamp",
                    "symbols": ["BTCUSDT"],
                    "intervals": ["5m"],
                    "datasets": ["trade"],
                },
            },
            {
                "status": "FAIL",
                "catalog_path": "data/catalog",
                "funding_path": "data/funding",
                "config_scope": {
                    "base_url": "https://fapi.binance.com",
                    "start": "2022-08-01T00:00:00+00:00",
                    "symbols": ["BTCUSDT"],
                    "intervals": ["5m"],
                    "datasets": ["bogus"],
                },
            },
            {
                "status": "FAIL",
                "catalog_path": "data/catalog",
                "funding_path": "data/funding",
                "config_scope": {
                    "base_url": "https://fapi.binance.com",
                    "start": "2022-08-01T00:00:00+00:00",
                    "symbols": ["USDT"],
                    "intervals": ["5m"],
                    "datasets": ["trade"],
                },
            },
        ):
            with self.subTest(invalid=invalid), TemporaryDirectory() as tmp:
                project = Path(tmp)
                config = write_valid_config(project)
                run_dir = project / "var/runs"
                run_dir.mkdir(parents=True)
                (run_dir / "sync-20260811T000000.000000Z.json").write_text(
                    json.dumps({
                        "status": "PASS",
                        "catalog_path": str((project / "data/catalog").resolve()),
                        "funding_path": str((project / "data/funding").resolve()),
                    }),
                    encoding="utf-8",
                )
                report = {
                    key: str((project / value).resolve()) if key.endswith("_path") else value
                    for key, value in invalid.items()
                }
                (run_dir / "sync-20260811T010000.000000Z.json").write_text(
                    json.dumps(report),
                    encoding="utf-8",
                )
                output = StringIO()
                errors = StringIO()

                with (
                    patch("nautilus_quant.cli.PROJECT_ROOT", project),
                    redirect_stdout(output),
                    redirect_stderr(errors),
                ):
                    result = main(["status", "--config", str(config)])

                event = json.loads(errors.getvalue().splitlines()[0])
                self.assertEqual(result, 1)
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(event["status"], "FAIL")
                self.assertEqual(event["error_type"], "ValueError")

    def test_sync_failure_report_is_visible_to_matching_status(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            config = write_valid_config(project)

            with (
                patch("nautilus_quant.cli.PROJECT_ROOT", project),
                patch("nautilus_quant.cli.run_sync", side_effect=RuntimeError("sync failed")),
                redirect_stdout(StringIO()),
                redirect_stderr(StringIO()),
            ):
                sync_result = main(["sync", "--config", str(config)])

            output = StringIO()
            with patch("nautilus_quant.cli.PROJECT_ROOT", project), redirect_stdout(output):
                status_result = main(["status", "--config", str(config)])

            event = json.loads(output.getvalue())
            self.assertEqual(sync_result, 1)
            self.assertEqual(status_result, 0)
            self.assertEqual(event["status"], "FAIL")
            self.assertEqual(event["latest_report"]["catalog_path"], str((project / "data/catalog").resolve()))
            self.assertEqual(event["latest_report"]["funding_path"], str((project / "data/funding").resolve()))


if __name__ == "__main__":
    unittest.main()
