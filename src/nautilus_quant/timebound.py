from __future__ import annotations

from datetime import datetime, timedelta, timezone

UTC = timezone.utc
_INTERVAL_MS = {
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
    "1w": 7 * 24 * 60 * 60_000,
}


def interval_millis(interval: str) -> int:
    try:
        return _INTERVAL_MS[interval]
    except KeyError as exc:
        raise ValueError(f"unsupported interval: {interval}") from exc


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def target_end(interval: str, now: datetime | None = None) -> datetime:
    """Return the exclusive boundary of complete data only."""
    now = _require_utc(now or datetime.now(UTC))
    day_end = datetime(now.year, now.month, now.day, tzinfo=UTC)
    if interval == "1w":
        return day_end - timedelta(days=day_end.weekday())
    interval_millis(interval)
    return day_end


def align_start(interval: str, start: datetime) -> datetime:
    start = _require_utc(start)
    interval_millis(interval)
    if interval != "1w":
        return start
    day = datetime(start.year, start.month, start.day, tzinfo=UTC)
    aligned = day - timedelta(days=day.weekday())
    return aligned if aligned == start else aligned + timedelta(days=7)


def to_millis(value: datetime) -> int:
    return int(_require_utc(value).timestamp() * 1000)
