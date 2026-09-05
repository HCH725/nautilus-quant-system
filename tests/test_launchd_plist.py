from pathlib import Path
import plistlib
import unittest


ROOT = Path(__file__).resolve().parents[1]
PLIST = ROOT / "ops/ai.nautilus.quant.data-sync.plist"
PAPER_PLIST = ROOT / "ops/ai.nautilus.quant.paper.plist"
PYTHON = "/Users/hong/.local/share/uv/python/cpython-3.13-macos-aarch64-none/bin/python3.13"
PYTHONPATH = f"{ROOT / 'src'}:{ROOT / '.venv/lib/python3.13/site-packages'}"
LOG_ROOT = "/Users/hong/Library/Logs/NautilusQuant"


class LaunchdPlistTests(unittest.TestCase):
    def test_uses_resolved_python_module_entrypoint_for_external_volume(self):
        with PLIST.open("rb") as source:
            config = plistlib.load(source)

        self.assertEqual(
            config["ProgramArguments"],
            [
                "/usr/bin/env",
                PYTHON,
                "-s",
                "-P",
                "-m",
                "nautilus_quant",
                "sync",
                "--config",
                str(ROOT / "config/market_data.json"),
            ],
        )
        self.assertEqual(config["EnvironmentVariables"]["PYTHONPATH"], PYTHONPATH)
        self.assertTrue(config["RunAtLoad"])
        self.assertEqual(config["StartCalendarInterval"], {"Hour": 10, "Minute": 15})
        self.assertEqual(config["StandardOutPath"], f"{LOG_ROOT}/data-sync.log")
        self.assertEqual(config["StandardErrorPath"], f"{LOG_ROOT}/data-sync.error.log")

    def test_paper_runtime_is_native_bounded_and_independent_from_data_sync(self):
        with PAPER_PLIST.open("rb") as source:
            config = plistlib.load(source)

        self.assertEqual(
            config["ProgramArguments"],
            [
                "/usr/bin/env",
                PYTHON,
                "-s",
                "-P",
                "-m",
                "nautilus_quant.paper_runtime",
                "--risk-policy",
                str(ROOT / "config/strategy_risk_execution_policy.json"),
                "--completed-bars",
                "24",
                "--timeout-seconds",
                "90000",
                "--output-directory",
                str(ROOT / "var/strategy-paper/smokes"),
            ],
        )
        self.assertEqual(config["EnvironmentVariables"]["PYTHONPATH"], PYTHONPATH)
        self.assertEqual(config["WorkingDirectory"], str(ROOT))
        self.assertTrue(config["RunAtLoad"])
        self.assertNotIn("StartCalendarInterval", config)
        self.assertNotIn("KeepAlive", config)
        self.assertEqual(config["StandardOutPath"], f"{LOG_ROOT}/paper.log")
        self.assertEqual(config["StandardErrorPath"], f"{LOG_ROOT}/paper.error.log")


if __name__ == "__main__":
    unittest.main()
