from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import unittest

from nautilus_quant.cli import main


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


if __name__ == "__main__":
    unittest.main()
