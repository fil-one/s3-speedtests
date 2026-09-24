import importlib.util
import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "build_summary_report.py"
NETWORK_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "network_speedtest_ookla.sh"
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


class TestStartTimestampTest(unittest.TestCase):
    def test_formats_timestamp_in_requested_utc_style(self) -> None:
        self.assertEqual(
            REPORT.format_tests_started_at_utc("20260901T015410Z"),
            "01:54:10 (UTC) on September 1, 2026",
        )

    def test_infers_start_from_latest_access_check_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "s3_access_check_20260920T230000Z.jsonl").write_text("", encoding="utf-8")
            (data_dir / "s3_access_check_20260921T015410Z.jsonl").write_text("", encoding="utf-8")
            (data_dir / "s3_provider_traceroutes_20260921T020000Z.jsonl").write_text("", encoding="utf-8")
            (data_dir / "network_speedtest_ookla_runs.jsonl").write_text(
                '{"saved_at_utc":"2026-09-21T01:58:00Z"}\n',
                encoding="utf-8",
            )

            self.assertEqual(REPORT.infer_tests_started_at_utc(data_dir), "20260921T015410Z")

    def test_falls_back_to_latest_data_file_modification_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            older = data_dir / "older.jsonl"
            newer = data_dir / "newer.log"
            older.write_text("", encoding="utf-8")
            newer.write_text("", encoding="utf-8")
            os.utime(older, (1_795_000_000, 1_795_000_000))
            os.utime(newer, (1_795_000_100, 1_795_000_100))

            expected = REPORT.dt.datetime.fromtimestamp(1_795_000_100, tz=REPORT.dt.UTC).strftime("%Y%m%dT%H%M%SZ")
            self.assertEqual(REPORT.infer_tests_started_at_utc(data_dir), expected)


class ReportNodeSpecsTest(unittest.TestCase):
    def test_omits_hostname_and_network_from_report_output(self) -> None:
        args = Namespace(
            tests_started_display="12:00:00 (UTC) on September 24, 2026",
            source_provider="Example provider",
            source_location="Paris, France",
            node_hostname="private-hostname",
            node_network="private-network",
            node_compute="16 vCPU",
            node_memory="126 GB RAM",
        )

        specs = REPORT.report_node_specs(args)

        self.assertEqual(
            specs,
            [
                ("Tests started", args.tests_started_display),
                ("Source provider", args.source_provider),
                ("Source location", args.source_location),
                ("Compute", args.node_compute),
                ("Memory", args.node_memory),
            ],
        )
        self.assertNotIn("private-hostname", str(specs))
        self.assertNotIn("private-network", str(specs))


class TracerouteEndpointTest(unittest.TestCase):
    def test_parses_explicit_endpoint_port_separately_from_hostname(self) -> None:
        self.assertEqual(
            REPORT.endpoint_target("https://s3.rustfs.staging.filonecontent.com:8010"),
            (
                "s3.rustfs.staging.filonecontent.com:8010",
                "s3.rustfs.staging.filonecontent.com",
                8010,
            ),
        )

    def test_traceroute_fallback_uses_explicit_port_and_bare_hostname(self) -> None:
        record = {"endpoint": "s3.rustfs.staging.filonecontent.com:8010"}

        self.assertEqual(
            REPORT.traceroute_command(record),
            "traceroute -T -p 8010 -n -w 3 -q 3 -m 30 s3.rustfs.staging.filonecontent.com",
        )

    def test_filter_accepts_current_and_legacy_traceroute_records(self) -> None:
        records = [
            {"test_type": "tcp_traceroute", "endpoint": "new.example:8010"},
            {"test_type": "tcp_traceroute_443", "endpoint": "old.example"},
            {"test_type": "something_else", "endpoint": "ignored.example"},
        ]

        self.assertEqual(REPORT.filter_traceroute_records(records, set()), records[:2])


class NetworkTargetTextTest(unittest.TestCase):
    def test_distinguishes_automatic_and_explicit_amsterdam_targets(self) -> None:
        automatic = {
            "target_label": "auto_nearest",
            "target_server_name": "Server A",
            "target_city": "Amsterdam",
            "target_country": "Netherlands",
        }
        explicit = {
            "target_label": "amsterdam_netherlands",
            "target_server_name": "Server B",
            "target_city": "Amsterdam",
            "target_country": "Netherlands",
        }

        self.assertEqual(
            REPORT.network_target_text(automatic),
            "Automatic nearest\nServer: Server A (Amsterdam, Netherlands)",
        )
        self.assertEqual(
            REPORT.network_target_text(explicit),
            "Amsterdam Netherlands\nServer: Server B (Amsterdam, Netherlands)",
        )

    def test_omits_auto_nearest_when_explicit_target_has_same_location(self) -> None:
        automatic = {
            "record_type": "network_speedtest_summary",
            "target_label": "auto_nearest",
            "target_server_id": "111",
            "target_city": "Amsterdam",
            "target_country": "Netherlands",
        }
        explicit = {
            "record_type": "network_speedtest_summary",
            "target_label": "amsterdam_netherlands",
            "target_server_id": "52365",
            "target_city": "Amsterdam",
            "target_country": "Netherlands",
        }

        self.assertEqual(REPORT.network_summary_rows([automatic, explicit]), [explicit])

    def test_keeps_auto_nearest_when_location_is_unique(self) -> None:
        automatic = {
            "record_type": "network_speedtest_summary",
            "target_label": "auto_nearest",
            "target_server_id": "111",
            "target_city": "Brussels",
            "target_country": "Belgium",
        }
        explicit = {
            "record_type": "network_speedtest_summary",
            "target_label": "amsterdam_netherlands",
            "target_server_id": "52365",
            "target_city": "Amsterdam",
            "target_country": "Netherlands",
        }

        self.assertEqual(REPORT.network_summary_rows([automatic, explicit]), [automatic, explicit])


class NetworkScriptConfigurationTest(unittest.TestCase):
    def test_world_mode_has_one_server_list_and_does_not_add_auto_nearest(self) -> None:
        script = NETWORK_SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertEqual(script.count("WORLD_SERVERS=("), 1)
        self.assertEqual(script.count("\n  world)"), 1)
        world_case = script.split("\n  world)", 1)[1].split("\n    ;;", 1)[0]
        self.assertNotIn("auto_nearest", world_case)


if __name__ == "__main__":
    unittest.main()
