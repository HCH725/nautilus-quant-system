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

    def test_omitted_backtest_start_remains_backward_compatible(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = load_config(self.write_config(root, {}), root)
            self.assertIsNone(config.backtest_start)

    def test_formal_config_pins_download_backtest_and_dataset_contract(self):
        root = Path(__file__).resolve().parents[1]
        config = load_config(root / "config/market_data.json", root)
        self.assertEqual(config.start.isoformat(), "2022-01-01T00:00:00+00:00")
        assert config.backtest_start is not None
        self.assertEqual(config.backtest_start.isoformat(), "2022-07-01T00:00:00+00:00")
        self.assertEqual(config.symbols, ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"))
        self.assertEqual(config.datasets, ("trade", "mark", "index", "funding"))

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

    def test_parses_explicit_backtest_start(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self.write_config(
                root,
                {
                    "start": "2022-01-01T00:00:00Z",
                    "backtest_start": "2022-07-01T00:00:00Z",
                },
            )
            config = load_config(path, root)
            self.assertEqual(config.start.isoformat(), "2022-01-01T00:00:00+00:00")
            assert config.backtest_start is not None
            self.assertEqual(config.backtest_start.isoformat(), "2022-07-01T00:00:00+00:00")

    def test_rejects_naive_or_pre_download_backtest_start(self):
        for backtest_start, error in (
            ("2022-07-01T00:00:00", "timezone"),
            ("2021-12-31T23:59:59Z", "before download start"),
        ):
            with self.subTest(backtest_start=backtest_start), TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = self.write_config(
                    root,
                    {
                        "start": "2022-01-01T00:00:00Z",
                        "backtest_start": backtest_start,
                    },
                )
                with self.assertRaisesRegex(ValueError, error):
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
