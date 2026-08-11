from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from nautilus_quant.config import load_config


def valid_config() -> dict[str, object]:
    return {
        "base_url": "https://fapi.binance.com",
        "start": "2021-01-01T00:00:00Z",
        "symbols": ["BTCUSDT"],
        "intervals": ["5m"],
        "datasets": ["trade"],
        "chunk_days": 30,
        "catalog_path": "data/catalog",
        "funding_path": "data/funding",
    }


class ConfigTests(unittest.TestCase):
    def write_config(self, root: Path, changes: dict[str, object]) -> Path:
        data = {**valid_config(), **changes}
        path = root / "config.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_rejects_unknown_dataset(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self.write_config(root, {"datasets": ["trade", "open_interest"]})
            with self.assertRaisesRegex(ValueError, "dataset"):
                load_config(path, root)

    def test_rejects_retired_premium_dataset(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self.write_config(root, {"datasets": ["trade", "premium"]})
            with self.assertRaisesRegex(ValueError, "dataset"):
                load_config(path, root)

    def test_rejects_naive_start(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self.write_config(root, {"start": "2021-01-01T00:00:00"})
            with self.assertRaisesRegex(ValueError, "timezone"):
                load_config(path, root)

    def test_rejects_non_ascii_lowercase_or_bare_usdt_symbols(self):
        for symbol in ("比特USDT", "btcUSDT", "USDT"):
            with self.subTest(symbol=symbol), TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = self.write_config(root, {"symbols": [symbol]})
                with self.assertRaisesRegex(ValueError, "symbols"):
                    load_config(path, root)

    def test_rejects_config_path_outside_project(self):
        with TemporaryDirectory() as project_tmp, TemporaryDirectory() as outside_tmp:
            root = Path(project_tmp)
            path = self.write_config(Path(outside_tmp), {})
            with self.assertRaisesRegex(ValueError, "config path"):
                load_config(path, root)

    def test_rejects_data_path_outside_project(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self.write_config(root, {"catalog_path": "../outside"})
            with self.assertRaisesRegex(ValueError, "project root"):
                load_config(path, root)


if __name__ == "__main__":
    unittest.main()
