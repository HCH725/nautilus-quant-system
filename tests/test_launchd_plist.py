from pathlib import Path
import plistlib
import unittest


ROOT = Path(__file__).resolve().parents[1]
PLIST = ROOT / "ops/ai.nautilus.quant.data-sync.plist"
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


if __name__ == "__main__":
    unittest.main()
