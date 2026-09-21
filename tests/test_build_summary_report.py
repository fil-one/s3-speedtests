import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "build_summary_report.py"
SPEC = importlib.util.spec_from_file_location("build_summary_report", MODULE_PATH)
REPORT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(REPORT)


def transfer_row(provider: str, total: float | None, failures: int = 0) -> dict:
    return {
        "provider": provider,
        "bucket": f"{provider}-bucket",
        "size_mib": 1.0,
        "attempts": 1,
        "successes": 0 if failures else 1,
        "failures": failures,
        "total_elapsed_seconds": total,
    }


class TotalTimeChartRowsTest(unittest.TestCase):
    def test_failed_provider_is_included_and_ranked_after_successful_providers(self) -> None:
        rows = [
            (
                1.0,
                [
                    transfer_row("aws", 1.0),
                    transfer_row("wasabi", 10.0),
                    transfer_row("backblaze", 20.0),
                ],
            ),
            (
                25_600.0,
                [
                    transfer_row("aws", None, failures=1),
                    transfer_row("wasabi", 30.0),
                    transfer_row("backblaze", 40.0),
                ],
            ),
        ]

        chart_rows = REPORT.total_time_chart_rows(rows, {})

        self.assertEqual([row["provider"] for row in chart_rows], ["wasabi", "backblaze", "aws"])
        self.assertFalse(chart_rows[0]["failed"])
        self.assertFalse(chart_rows[1]["failed"])
        self.assertTrue(chart_rows[2]["failed"])
        self.assertEqual(chart_rows[2]["total_seconds"], 1.0)

    def test_provider_with_only_failures_is_retained_at_the_bottom(self) -> None:
        rows = [
            (
                50_000.0,
                [
                    transfer_row("aws", None, failures=1),
                    transfer_row("wasabi", 30.0),
                ],
            )
        ]

        chart_rows = REPORT.total_time_chart_rows(rows, {})

        self.assertEqual([row["provider"] for row in chart_rows], ["wasabi", "aws"])
        self.assertTrue(chart_rows[-1]["failed"])
        self.assertEqual(chart_rows[-1]["total_seconds"], 0.0)


if __name__ == "__main__":
    unittest.main()
