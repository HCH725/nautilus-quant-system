from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path

from .timebound import UTC, interval_millis

_ALLOWED_DATASETS = {"trade", "funding"}


def valid_binance_usdt_symbol(value: str) -> bool:
    return (
        len(value) > len("USDT")
        and value.isascii()
        and value.isupper()
        and value.isalnum()
        and value.endswith("USDT")
    )


def _string_array(raw: dict[str, object], field: str) -> tuple[str, ...]:
    value = raw.get(field)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be an array of strings")
    return tuple(value)


@dataclass(frozen=True)
class MarketDataConfig:
    base_url: str
    start: datetime
    symbols: tuple[str, ...]
    intervals: tuple[str, ...]
    datasets: tuple[str, ...]
    chunk_days: int
    catalog_path: Path
    funding_path: Path
    backtest_start: datetime | None = None


def load_config(path: Path, project_root: Path) -> MarketDataConfig:
    root = Path(project_root).resolve()
    path = Path(path).resolve()
    if not path.is_relative_to(root):
        raise ValueError("config path must stay within the project root")
    raw = json.loads(path.read_text(encoding="utf-8"))
    datasets = _string_array(raw, "datasets")
    if not datasets or len(datasets) != len(set(datasets)):
        raise ValueError("datasets must be non-empty and unique")
    unknown = set(datasets) - _ALLOWED_DATASETS
    if unknown:
        raise ValueError(f"unsupported dataset(s): {sorted(unknown)}")
    intervals = _string_array(raw, "intervals")
    if not intervals or len(intervals) != len(set(intervals)):
        raise ValueError("intervals must be non-empty and unique")
    for interval in intervals:
        interval_millis(interval)
    symbols = _string_array(raw, "symbols")
    if not symbols or len(symbols) != len(set(symbols)) or any(not valid_binance_usdt_symbol(s) for s in symbols):
        raise ValueError("symbols must be non-empty, unique Binance USDT symbols")
    parsed_start = datetime.fromisoformat(raw["start"].replace("Z", "+00:00"))
    if parsed_start.tzinfo is None:
        raise ValueError("start must include a timezone")
    start = parsed_start.astimezone(UTC)
    backtest_start = None
    if raw.get("backtest_start") is not None:
        parsed_backtest_start = datetime.fromisoformat(raw["backtest_start"].replace("Z", "+00:00"))
        if parsed_backtest_start.tzinfo is None:
            raise ValueError("backtest_start must include a timezone")
        backtest_start = parsed_backtest_start.astimezone(UTC)
        if backtest_start < start:
            raise ValueError("backtest_start cannot be before download start")
    raw_chunk_days = raw.get("chunk_days")
    if isinstance(raw_chunk_days, bool) or not isinstance(raw_chunk_days, int):
        raise ValueError("chunk_days must be an integer")
    chunk_days = raw_chunk_days
    if chunk_days < 1:
        raise ValueError("chunk_days must be positive")
    base_url = str(raw["base_url"]).rstrip("/")
    if base_url != "https://fapi.binance.com":
        raise ValueError("base_url must be the Binance USD-M public API")
    catalog_path = (root / raw["catalog_path"]).resolve()
    funding_path = (root / raw["funding_path"]).resolve()
    if not catalog_path.is_relative_to(root) or not funding_path.is_relative_to(root):
        raise ValueError("data paths must stay within the project root")
    return MarketDataConfig(
        base_url=base_url,
        start=start,
        symbols=symbols,
        intervals=intervals,
        datasets=datasets,
        chunk_days=chunk_days,
        catalog_path=catalog_path,
        funding_path=funding_path,
        backtest_start=backtest_start,
    )
