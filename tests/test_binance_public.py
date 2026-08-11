from io import BytesIO
from urllib.error import HTTPError
import unittest

from nautilus_quant.binance_public import (
    BinancePublicClient,
    Kline,
    endpoint_request,
    normalize_kline,
    paginate_klines,
    validate_contiguous,
)


class BinancePublicTests(unittest.TestCase):
    def test_index_uses_pair_and_other_klines_use_symbol(self):
        path, params = endpoint_request("index", "BTCUSDT", "5m", 1, 2)
        self.assertEqual(path, "/fapi/v1/indexPriceKlines")
        self.assertEqual(params["pair"], "BTCUSDT")
        self.assertNotIn("symbol", params)
        _, trade_params = endpoint_request("trade", "BTCUSDT", "5m", 1, 2)
        self.assertEqual(trade_params["symbol"], "BTCUSDT")

    def test_pagination_advances_from_last_open(self):
        pages = [
            [[0, "1", "2", "0.5", "1.5", "3", 299_999]],
            [[300_000, "1.5", "2", "1", "1.8", "4", 599_999]],
            [],
        ]
        calls = []
        def fetch(start_ms):
            calls.append(start_ms)
            return pages.pop(0)
        rows = list(paginate_klines(fetch, 0, 900_000, 300_000))
        self.assertEqual([row[0] for row in rows], [0, 300_000])
        self.assertEqual(calls, [0, 300_000, 600_000])

    def test_non_trade_volume_is_zero(self):
        row = [0, "1", "2", "0.5", "1.5", "99", 299_999]
        self.assertEqual(normalize_kline("mark", row, 300_000).volume, "0")
        self.assertEqual(normalize_kline("trade", row, 300_000).volume, "99")

    def test_retry_falls_back_when_retry_after_is_malformed_and_reads_weight(self):
        calls = 0
        sleeps: list[float] = []

        class Response:
            headers = {"X-MBX-USED-WEIGHT-1M": "123"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def read() -> bytes:
                return b"[]"

        def opener(_request, timeout):
            nonlocal calls
            self.assertEqual(timeout, 30.0)
            calls += 1
            if calls == 1:
                raise HTTPError("https://example.invalid", 429, "rate", {"Retry-After": "bad"}, BytesIO())
            return Response()

        client = BinancePublicClient(
            retries=1,
            request_delay=0,
            opener=opener,
            sleeper=sleeps.append,
        )
        self.assertEqual(client.request_json("/fapi/v1/klines", {}), [])
        self.assertEqual(calls, 2)
        self.assertEqual(sleeps, [1.0])
        self.assertEqual(client.last_used_weight, 123)

    def test_retry_after_is_capped(self):
        self.assertEqual(
            BinancePublicClient._retry_after({"Retry-After": "9999"}, 1.0),
            60.0,
        )

    def test_continuity_rejects_gap(self):
        rows = [
            Kline(0, 300_000, "1", "1", "1", "1", "1"),
            Kline(600_000, 900_000, "1", "1", "1", "1", "1"),
        ]
        with self.assertRaisesRegex(ValueError, "gap"):
            validate_contiguous(rows, 300_000)


if __name__ == "__main__":
    unittest.main()
