from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from nautilus_quant import strategy_lab


_WRITER_TRY = r'''
import fcntl, sys
p = open(sys.argv[1], "a+b")
try:
    fcntl.flock(p.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("BLOCKED")
    raise SystemExit(3)
print("ACQUIRED")
'''

_WRITER_HOLD = r'''
import fcntl, pathlib, sys, time
lock, ready, release = map(pathlib.Path, sys.argv[1:])
p = lock.open("a+b")
fcntl.flock(p.fileno(), fcntl.LOCK_EX)
ready.write_text("ready")
for _ in range(500):
    if release.exists():
        break
    time.sleep(0.01)
fcntl.flock(p.fileno(), fcntl.LOCK_UN)
'''


class DataSnapshotLockTests(unittest.TestCase):
    def _writer_try(self, lock: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", _WRITER_TRY, str(lock)],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_writer_is_busy_while_reader_holds_shared_lock(self) -> None:
        with TemporaryDirectory() as temporary:
            lock = Path(temporary) / "data-sync.lock"
            with strategy_lab._hold_data_snapshot_shared(lock):
                result = self._writer_try(lock)
                self.assertEqual(result.returncode, 3)
                self.assertEqual(result.stdout.strip(), "BLOCKED")
            result = self._writer_try(lock)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), "ACQUIRED")

    def test_reader_waits_for_existing_writer_then_acquires(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = root / "data-sync.lock"
            ready = root / "ready"
            release = root / "release"
            child = subprocess.Popen(
                [sys.executable, "-c", _WRITER_HOLD, str(lock), str(ready), str(release)],
            )
            self.addCleanup(lambda: child.poll() is None and child.kill())
            for _ in range(200):
                if ready.exists():
                    break
                time.sleep(0.01)
            self.assertTrue(ready.exists())
            acquired = Event()

            def reader() -> None:
                with strategy_lab._hold_data_snapshot_shared(lock):
                    acquired.set()

            thread = Thread(target=reader)
            thread.start()
            time.sleep(0.1)
            self.assertFalse(acquired.is_set())
            release.touch()
            thread.join(timeout=5)
            child.wait(timeout=5)
            self.assertTrue(acquired.is_set())

    def test_nested_shared_lock_does_not_release_outer_reader(self) -> None:
        with TemporaryDirectory() as temporary:
            lock = Path(temporary) / "data-sync.lock"
            with strategy_lab._hold_data_snapshot_shared(lock):
                with strategy_lab._hold_data_snapshot_shared(lock):
                    self.assertEqual(self._writer_try(lock).returncode, 3)
                self.assertEqual(self._writer_try(lock).returncode, 3)
            self.assertEqual(self._writer_try(lock).returncode, 0)

    def test_exception_releases_reader_lock(self) -> None:
        with TemporaryDirectory() as temporary:
            lock = Path(temporary) / "data-sync.lock"
            with self.assertRaisesRegex(RuntimeError, "boom"):
                with strategy_lab._hold_data_snapshot_shared(lock):
                    raise RuntimeError("boom")
            self.assertEqual(self._writer_try(lock).returncode, 0)

    def test_standalone_strategy_loop_holds_shared_lock_for_execution(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = root / "data-sync.lock"
            paths = strategy_lab.StrategyLoopPaths(
                root / "market.json",
                root / "policy.json",
                root / "catalog",
                root / "funding",
                root / "state",
            )
            prepared = SimpleNamespace(identity=object())

            def execute(*_args: object, **_kwargs: object) -> dict[str, object]:
                self.assertEqual(self._writer_try(lock).returncode, 3)
                return {"status": "EVALUATED"}

            with (
                patch.object(strategy_lab, "_DATA_SYNC_LOCK", lock),
                patch.object(strategy_lab, "load_strategy_hypothesis", return_value=object()),
                patch.object(strategy_lab, "_prepare_execution", return_value=prepared),
                patch.object(strategy_lab, "_experiment_id", return_value="e" * 64),
                patch.object(strategy_lab, "_run_strategy_loop_locked", side_effect=execute),
            ):
                result = strategy_lab.run_strategy_loop(root / "hypothesis.json", paths)
            self.assertEqual(result["status"], "EVALUATED")
            self.assertEqual(self._writer_try(lock).returncode, 0)

    def test_campaign_holds_one_outer_shared_lock_for_cohort(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = root / "data-sync.lock"
            paths = strategy_lab.StrategyLoopPaths(
                root / "market.json",
                root / "policy.json",
                root / "catalog",
                root / "funding",
                root / "state",
            )
            spec = SimpleNamespace(screen_policy_id="0" * 64, data_as_of_ns=0)

            def run_campaign_stub(*_args: object, **_kwargs: object) -> dict[str, object]:
                self.assertEqual(self._writer_try(lock).returncode, 3)
                return {"status": "COMPLETE"}

            with (
                patch.object(strategy_lab, "_DATA_SYNC_LOCK", lock),
                patch.object(strategy_lab, "expand_campaign", return_value=[]),
                patch.object(strategy_lab, "run_campaign", side_effect=run_campaign_stub),
            ):
                result = strategy_lab.run_strategy_campaign(spec, paths)  # type: ignore[arg-type]
            self.assertEqual(result["status"], "COMPLETE")
            self.assertEqual(self._writer_try(lock).returncode, 0)


if __name__ == "__main__":
    unittest.main()
