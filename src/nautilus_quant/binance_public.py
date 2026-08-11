from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import json
import time
from typing import Callable, Iterable, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

_ENDPOINTS = {
    "trade": "/fapi/v1/klines",
    "mark": "/fapi/v1/markPriceKlines",
    "index": "/fapi/v1/indexPriceKlines",
}
# ponytail: one day bounds REST fallback cost and prevents hiding long
# outages. Add an official archive adapter before raising this ceiling.
_MAX_RECONSTRUCTION_MINUTES = 1_440


@dataclass(frozen=True)
class Kline:
    open_ms: int
    close_ms: int
    open: str
    high: str
    low: str
    close: str
    volume: str
    source_interval: str | None = None


def endpoint_request(dataset: str, symbol: str, interval: str, start_ms: int, end_ms: int) -> tuple[str, dict[str, object]]:
    try:
        path = _ENDPOINTS[dataset]
    except KeyError as exc:
        raise ValueError(f"unsupported kline dataset: {dataset}") from exc
    params: dict[str, object] = {
        "interval": interval,
        "startTime": start_ms,
        "endTime": end_ms - 1,
        "limit": 1500,
    }
    params["pair" if dataset == "index" else "symbol"] = symbol
    return path, params


def normalize_kline(dataset: str, row: list[object], interval_ms: int) -> Kline:
    if len(row) < 7:
        raise ValueError("short Binance kline row")
    open_ms = int(row[0])
    return Kline(
        open_ms=open_ms,
        close_ms=open_ms + interval_ms,
        open=str(row[1]),
        high=str(row[2]),
        low=str(row[3]),
        close=str(row[4]),
        volume=str(row[5]) if dataset == "trade" else "0",
    )


def paginate_klines(
    fetch_page: Callable[[int], list[list[object]]],
    start_ms: int,
    end_ms: int,
    interval_ms: int,
) -> Iterator[list[object]]:
    cursor = start_ms
    while cursor < end_ms:
        page = fetch_page(cursor)
        if not page:
            return
        accepted = [row for row in page if cursor <= int(row[0]) < end_ms]
        if not accepted:
            return
        accepted.sort(key=lambda row: int(row[0]))
        for row in accepted:
            yield row
        next_cursor = int(accepted[-1][0]) + interval_ms
        if next_cursor <= cursor:
            raise ValueError("Binance pagination did not advance")
        cursor = next_cursor


def validate_contiguous(rows: Iterable[Kline], interval_ms: int) -> None:
    previous: Kline | None = None
    for row in rows:
        if row.close_ms != row.open_ms + interval_ms:
            raise ValueError(f"invalid close boundary at {row.open_ms}")
        if previous is not None:
            if row.open_ms == previous.open_ms:
                raise ValueError(f"duplicate timestamp: {row.open_ms}")
            if row.open_ms != previous.open_ms + interval_ms:
                raise ValueError(f"kline gap: {previous.open_ms} -> {row.open_ms}")
        previous = row


def _internal_gap_starts(rows: list[Kline], interval_ms: int) -> list[int]:
    missing: list[int] = []
    for previous, row in zip(rows, rows[1:], strict=False):
        for open_ms in range(previous.open_ms + interval_ms, row.open_ms, interval_ms):
            if (len(missing) + 1) * interval_ms // 60_000 > _MAX_RECONSTRUCTION_MINUTES:
                raise ValueError(
                    f"reconstruction window too large: "
                    f"{(len(missing) + 1) * interval_ms // 60_000} minutes",
                )
            missing.append(open_ms)
    return missing


def _aggregate_klines(dataset: str, rows: list[Kline], open_ms: int, interval_ms: int) -> Kline:
    expected_rows = interval_ms // 60_000
    if (
        interval_ms % 60_000
        or len(rows) != expected_rows
        or not rows
        or rows[0].open_ms != open_ms
        or rows[-1].close_ms != open_ms + interval_ms
    ):
        raise ValueError(f"incomplete one-minute reconstruction at {open_ms}")
    validate_contiguous(rows, 60_000)
    return Kline(
        open_ms=open_ms,
        close_ms=open_ms + interval_ms,
        open=rows[0].open,
        high=max(rows, key=lambda row: Decimal(row.high)).high,
        low=min(rows, key=lambda row: Decimal(row.low)).low,
        close=rows[-1].close,
        volume=str(sum((Decimal(row.volume) for row in rows), start=Decimal())) if dataset == "trade" else "0",
        source_interval="1m",
    )


class BinancePublicClient:
    def __init__(
        self,
        base_url: str = "https://fapi.binance.com",
        timeout: float = 30.0,
        retries: int = 6,
        request_delay: float = 0.25,
        opener: Callable = urlopen,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.request_delay = request_delay
        self.opener = opener
        self.sleeper = sleeper
        self.monotonic = monotonic
        self.last_request_at: float | None = None
        self.last_used_weight: int | None = None

    def _pace(self) -> None:
        now = self.monotonic()
        if self.last_request_at is not None:
            remaining = self.request_delay - (now - self.last_request_at)
            if remaining > 0:
                self.sleeper(remaining)
        self.last_request_at = self.monotonic()

    @staticmethod
    def _retry_after(headers, fallback: float) -> float:
        value = headers.get("Retry-After") if headers is not None else None
        try:
            return min(60.0, max(0.0, float(value))) if value is not None else fallback
        except (TypeError, ValueError):
            return fallback

    def request_json(self, path: str, params: dict[str, object]) -> list[object] | dict[str, object]:
        url = f"{self.base_url}{path}?{urlencode(params)}"
        for attempt in range(self.retries + 1):
            try:
                self._pace()
                request = Request(url, headers={"User-Agent": "nautilus-quant-system/0.1"})
                with self.opener(request, timeout=self.timeout) as response:  # noqa: S310 - fixed HTTPS host is validated by config
                    used_weight = response.headers.get("X-MBX-USED-WEIGHT-1M")
                    if used_weight is not None:
                        self.last_used_weight = int(used_weight)
                    return json.loads(response.read())
            except HTTPError as exc:
                retryable = exc.code in {418, 429} or 500 <= exc.code < 600
                if not retryable or attempt == self.retries:
                    raise
                fallback = min(60.0, 2.0**attempt)
                delay = self._retry_after(exc.headers, fallback)
            except (URLError, TimeoutError):
                if attempt == self.retries:
                    raise
                delay = min(60.0, 2.0**attempt)
            self.sleeper(delay)
        raise AssertionError("unreachable")

    def klines(self, dataset: str, symbol: str, interval: str, start_ms: int, end_ms: int, interval_ms: int) -> list[Kline]:
        def fetch(cursor: int) -> list[list[object]]:
            path, params = endpoint_request(dataset, symbol, interval, cursor, end_ms)
            payload = self.request_json(path, params)
            if not isinstance(payload, list):
                raise ValueError("Binance kline response is not a list")
            return payload

        rows = [normalize_kline(dataset, row, interval_ms) for row in paginate_klines(fetch, start_ms, end_ms, interval_ms)]
        if interval != "1m":
            gap_starts = _internal_gap_starts(rows, interval_ms)
            for open_ms in gap_starts:
                one_minute_rows = self.klines(dataset, symbol, "1m", open_ms, open_ms + interval_ms, 60_000)
                rows.append(_aggregate_klines(dataset, one_minute_rows, open_ms, interval_ms))
            rows.sort(key=lambda row: row.open_ms)
        validate_contiguous(rows, interval_ms)
        return rows

    def funding(self, symbol: str, start_ms: int, end_ms: int) -> list[dict[str, object]]:
        cursor = start_ms
        result: list[dict[str, object]] = []
        while cursor < end_ms:
            payload = self.request_json(
                "/fapi/v1/fundingRate",
                {"symbol": symbol, "startTime": cursor, "endTime": end_ms - 1, "limit": 1000},
            )
            if not isinstance(payload, list) or not payload:
                break
            page = [item for item in payload if cursor <= int(item["fundingTime"]) < end_ms]
            if not page:
                break
            page.sort(key=lambda item: int(item["fundingTime"]))
            result.extend(page)
            next_cursor = int(page[-1]["fundingTime"]) + 1
            if next_cursor <= cursor:
                raise ValueError("Binance funding pagination did not advance")
            cursor = next_cursor
        timestamps = [int(item["fundingTime"]) for item in result]
        if len(timestamps) != len(set(timestamps)):
            raise ValueError("duplicate funding timestamp")
        return result
