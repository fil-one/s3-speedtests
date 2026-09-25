import importlib.util
import subprocess
import threading
import unittest
from argparse import Namespace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


UPLOAD = load_script("s3_upload_speedtest")
DOWNLOAD = load_script("s3_download_speedtest")


class ParallelJobRunnerTest(unittest.TestCase):
    def assert_jobs_overlap(self, module) -> None:
        barrier = threading.Barrier(2, timeout=2)

        def worker(value: int) -> int:
            barrier.wait()
            return value * 2

        self.assertCountEqual(module.run_jobs([1, 2], worker, 2), [2, 4])

    def test_upload_jobs_overlap(self) -> None:
        self.assert_jobs_overlap(UPLOAD)

    def test_download_jobs_overlap(self) -> None:
        self.assert_jobs_overlap(DOWNLOAD)

    def test_parallel_mode_defaults_to_four_workers(self) -> None:
        args = Namespace(parallel=True, parallel_workers=4)
        self.assertEqual(UPLOAD.worker_count(args), 4)
        self.assertEqual(DOWNLOAD.worker_count(args), 4)

    def test_serial_mode_uses_one_worker(self) -> None:
        args = Namespace(parallel=False, parallel_workers=99)
        self.assertEqual(UPLOAD.worker_count(args), 1)
        self.assertEqual(DOWNLOAD.worker_count(args), 1)


class ParallelCliTest(unittest.TestCase):
    def test_transfer_commands_expose_parallel_flags(self) -> None:
        for script in ("s3_upload_speedtest.py", "s3_download_speedtest.py"):
            proc = subprocess.run(
                [str(ROOT / "scripts" / script), "--help"],
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertIn("--parallel", proc.stdout)
            self.assertIn("--parallel-workers N", proc.stdout)

    def test_run_all_exposes_parallel_flags(self) -> None:
        proc = subprocess.run(
            [str(ROOT / "scripts" / "run_all"), "--help"],
            check=True,
            text=True,
            capture_output=True,
        )
        self.assertIn("--parallel", proc.stdout)
        self.assertIn("--parallel-workers N", proc.stdout)


if __name__ == "__main__":
    unittest.main()
