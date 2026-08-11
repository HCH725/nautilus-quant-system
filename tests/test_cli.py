from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import unittest

from nautilus_quant.cli import main


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
