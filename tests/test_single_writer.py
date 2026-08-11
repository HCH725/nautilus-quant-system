from pathlib import Path
import os
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from nautilus_quant.service import AlreadyRunning, single_writer


class SingleWriterTests(unittest.TestCase):
    def test_second_process_cannot_enter(self):
        with TemporaryDirectory() as tmp:
            lock = Path(tmp) / "data.lock"
            code = (
                "from pathlib import Path\n"
                "from nautilus_quant.service import single_writer\n"
                f"with single_writer(Path({str(lock)!r})):\n"
                " print('LOCKED', flush=True)\n"
                " input()\n"
            )
            env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
            child = subprocess.Popen(
                [sys.executable, "-c", code],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            try:
                self.assertEqual(child.stdout.readline().strip(), "LOCKED")
                with self.assertRaises(AlreadyRunning):
                    with single_writer(lock):
                        self.fail("second writer entered")
            finally:
                _, stderr = child.communicate("\n", timeout=10)
            self.assertEqual(child.returncode, 0, stderr)


if __name__ == "__main__":
    unittest.main()
