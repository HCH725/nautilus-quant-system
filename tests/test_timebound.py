from datetime import datetime, timezone
import unittest

from nautilus_quant.timebound import align_start, interval_millis, target_end


class TimeBoundTests(unittest.TestCase):
    def test_interval_millis(self):
        self.assertEqual(interval_millis("5m"), 300_000)
        self.assertEqual(interval_millis("1w"), 604_800_000)

    def test_daily_target_is_previous_complete_utc_day(self):
        now = datetime(2026, 8, 11, 16, 30, tzinfo=timezone.utc)
        self.assertEqual(target_end("5m", now), datetime(2026, 8, 11, tzinfo=timezone.utc))

    def test_weekly_target_is_last_completed_monday_boundary(self):
        now = datetime(2026, 8, 11, 16, 30, tzinfo=timezone.utc)
        self.assertEqual(target_end("1w", now), datetime(2026, 8, 10, tzinfo=timezone.utc))

    def test_weekly_start_aligns_up_to_monday(self):
        start = datetime(2021, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(align_start("1w", start), datetime(2021, 1, 4, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
