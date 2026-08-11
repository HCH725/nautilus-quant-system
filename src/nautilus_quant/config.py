from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path

from .timebound import UTC, interval_millis

_ALLOWED_DATASETS = {"trade", "mark", "index", "premium", "funding"}


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


def load_config(path: Path, project_root: Path) -> MarketDataConfig:
    root = Path(project_root).resolve()
    path = Path(path).resolve()
    if not path.is_relative_to(root):
        raise ValueError("config path must stay within the project root")
    raw = json.loads(path.read_text(encoding="utf-8"))
    datasets = tuple(raw["datasets"])
    if not datasets or len(datasets) != len(set(datasets)):
        raise ValueError("datasets must be non-empty and unique")
    unknown = set(datasets) - _ALLOWED_DATASETS
    if unknown:
        raise ValueError(f"unsupported dataset(s): {sorted(unknown)}")
    intervals = tuple(raw["intervals"])
    if not intervals or len(intervals) != len(set(intervals)):
        raise ValueError("intervals must be non-empty and unique")
    for interval in intervals:
        interval_millis(interval)
    symbols = tuple(raw["symbols"])
    if not symbols or len(symbols) != len(set(symbols)) or any(not s.endswith("USDT") or not s.isalnum() for s in symbols):
        raise ValueError("symbols must be non-empty, unique Binance USDT symbols")
    parsed_start = datetime.fromisoformat(raw["start"].replace("Z", "+00:00"))
    if parsed_start.tzinfo is None:
        raise ValueError("start must include a timezone")
    start = parsed_start.astimezone(UTC)
    chunk_days = int(raw["chunk_days"])
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
    )
