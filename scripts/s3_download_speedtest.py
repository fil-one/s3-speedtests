#!/usr/bin/env python3
"""S3-compatible download speed test harness.

Reads /testfiles/s3_targets.ini, lists uploaded bucket objects under each target
prefix, downloads selected .bin objects into /downloads, records listing and
transfer speed metrics as JSONL under /dataoutput, then deletes downloaded .bin files
after each provider.
"""

from __future__ import annotations

import argparse
import configparser
import datetime as dt
import json
import os
import shlex
import shutil
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "perf_test_v1"
TEST_SUITE = "s3_provider_transfer_test"
DEFAULT_AWS_PATH = "aws"
DEFAULT_TARGETS = "/testfiles/s3_targets.ini"
DEFAULT_OUTPUT_DIR = "/dataoutput"
DEFAULT_DOWNLOADS_DIR = "/downloads"
DEFAULT_PUBLIC_IP = "194.26.100.186"


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_name(value: str) -> str:
    out = []
    for ch in value.lower():
        out.append(ch if ch.isalnum() or ch in "-_" else "-")
    return "".join(out).strip("-") or "unknown"


def bool_value(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "yes", "true", "on"}


def tail_text(text: str, limit: int = 2000) -> str:
    text = text or ""
    return text if len(text) <= limit else text[-limit:]


def write_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")


def load_targets(path: Path, only: set[str] | None = None) -> list[dict[str, str]]:
    parser = configparser.ConfigParser(interpolation=None)
    if not path.exists():
        raise SystemExit(f"Target config not found: {path}")
    parser.read(path)
    targets: list[dict[str, str]] = []
    for section in parser.sections():
        if only and section not in only:
            continue
        if not bool_value(parser.get(section, "enabled", fallback="false")):
            continue
        bucket = parser.get(section, "bucket", fallback="").strip()
        if not bucket or bucket.startswith("REPLACE_"):
            print(f"Skipping [{section}]: bucket missing/placeholder", file=sys.stderr)
            continue
        target = {key: parser.get(section, key, fallback="").strip() for key in parser[section]}
        target["section"] = section
        target["provider"] = target.get("provider") or section
        target["bucket"] = bucket
        targets.append(target)
    return targets


def aws_version(aws_path: str) -> str:
    try:
        return subprocess.check_output([aws_path, "--version"], text=True, stderr=subprocess.STDOUT).strip()
    except Exception as exc:
        return f"unknown: {exc}"


def use_inline_credentials(target: dict[str, str]) -> bool:
    auth_mode = (target.get("auth_mode") or "auto").strip().lower()
    has_profile = bool(target.get("profile"))
    if auth_mode == "inline":
        return True
    if auth_mode == "profile":
        return False
    return not has_profile


def auth_mode_label(target: dict[str, str]) -> str:
    return "inline_env" if use_inline_credentials(target) else "profile"


def build_env(target: dict[str, str]) -> dict[str, str]:
    env = os.environ.copy()
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    region = target.get("region") or "us-east-1"
    env["AWS_DEFAULT_REGION"] = region
    env["AWS_REGION"] = region
    env["AWS_REQUEST_CHECKSUM_CALCULATION"] = target.get("request_checksum_calculation") or "when_required"
    env["AWS_RESPONSE_CHECKSUM_VALIDATION"] = target.get("response_checksum_validation") or "when_required"
    if use_inline_credentials(target):
        if target.get("access_key_id") and not target["access_key_id"].startswith("REPLACE_"):
            env["AWS_ACCESS_KEY_ID"] = target["access_key_id"]
        if target.get("secret_access_key") and not target["secret_access_key"].startswith("REPLACE_"):
            env["AWS_SECRET_ACCESS_KEY"] = target["secret_access_key"]
        if target.get("session_token"):
            env["AWS_SESSION_TOKEN"] = target["session_token"]
    return env


def aws_base(aws_path: str, target: dict[str, str]) -> list[str]:
    cmd = [aws_path]
    if target.get("profile"):
        cmd.extend(["--profile", target["profile"]])
    if target.get("endpoint_url"):
        cmd.extend(["--endpoint-url", target["endpoint_url"]])
    if target.get("region"):
        cmd.extend(["--region", target["region"]])
    return cmd


def base_record(args: argparse.Namespace, run_id: str, aws_path: str, aws_version_text: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "test_suite": TEST_SUITE,
        "run_id": run_id,
        "vm_hostname": socket.gethostname(),
        "vm_public_ip": DEFAULT_PUBLIC_IP,
        "aws_cli_path": aws_path,
        "aws_cli_version": aws_version_text,
        "targets_file": str(Path(args.targets).resolve()),
        "downloads_dir": str(Path(args.downloads_dir).resolve()),
        "output_dir": str(Path(args.output_dir).resolve()),
        "file_set": args.file_set,
    }


def object_size_mib(size_bytes: int) -> float:
    return round(size_bytes / 1024 / 1024, 3)


def file_set_accept(size_bytes: int, file_set: str) -> bool:
    mib = 1024 * 1024
    gib = 1024 * mib
    if file_set == "quick":
        return size_bytes in {mib, 100 * mib}
    if file_set == "standard":
        return size_bytes <= gib
    if file_set == "large":
        return size_bytes >= gib
    if file_set == "full":
        return True
    raise SystemExit(f"Unknown file set: {file_set}")


def list_objects(aws_path: str, target: dict[str, str], prefix: str, env: dict[str, str], args: argparse.Namespace) -> tuple[list[dict[str, Any]], subprocess.CompletedProcess[str], float]:
    cmd = aws_base(aws_path, target) + [
        "s3api", "list-objects-v2",
        "--bucket", target["bucket"],
        "--prefix", prefix.rstrip("/") + "/",
        "--output", "json",
    ]
    started = time.monotonic()
    proc = subprocess.run(cmd, text=True, capture_output=True, env=env)
    elapsed = time.monotonic() - started
    if proc.returncode != 0:
        return [], proc, elapsed
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return [], proc, elapsed
    objects = []
    for item in payload.get("Contents", []) or []:
        key = item.get("Key", "")
        size = int(item.get("Size", 0) or 0)
        if not key.endswith(".bin"):
            continue
        if not file_set_accept(size, args.file_set):
            continue
        objects.append({"key": key, "size_bytes": size})
    objects.sort(key=lambda obj: (obj["size_bytes"], obj["key"]))
    if args.max_files and args.max_files > 0:
        objects = objects[:args.max_files]
    return objects, proc, elapsed


def download_one(aws_path: str, target: dict[str, str], key: str, dest: Path, env: dict[str, str], args: argparse.Namespace) -> tuple[subprocess.CompletedProcess[str], float]:
    cmd = aws_base(aws_path, target) + ["s3", "cp", f"s3://{target['bucket']}/{key}", str(dest)]
    if args.no_progress:
        cmd.append("--no-progress")
    if args.only_show_errors:
        cmd.append("--only-show-errors")
    extra_args = target.get("extra_args", "")
    if extra_args:
        cmd.extend(shlex.split(extra_args))
    t0 = time.monotonic()
    proc = subprocess.run(cmd, text=True, capture_output=True, env=env)
    elapsed = time.monotonic() - t0
    return proc, elapsed


def cleanup_downloads(provider_dir: Path) -> dict[str, Any]:
    deleted = 0
    bytes_deleted = 0
    provider_dir.mkdir(parents=True, exist_ok=True)
    for path in provider_dir.rglob("*.bin"):
        if path.is_file():
            try:
                bytes_deleted += path.stat().st_size
                path.unlink()
                deleted += 1
            except FileNotFoundError:
                pass
    for directory in sorted([p for p in provider_dir.rglob("*") if p.is_dir()], reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    return {"deleted_bin_files": deleted, "deleted_bin_bytes": bytes_deleted}


def summarize_downloads(records: list[dict[str, Any]], base: dict[str, Any], output_paths: dict[str, Path]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for record in records:
        key = (
            record["provider"],
            record["bucket"],
            record.get("endpoint_url", ""),
            record.get("region", ""),
            record["file_size_mib"],
        )
        groups.setdefault(key, []).append(record)
    summaries = []
    for (provider, bucket, endpoint_url, region, size_mib), group in sorted(groups.items()):
        successes = [r for r in group if r["success"]]
        speeds = [r["throughput_mbps"] for r in successes if r["throughput_mbps"] is not None]
        elapsed = [r["elapsed_seconds"] for r in successes]
        summary = dict(base)
        summary.update({
            "record_type": "s3_download_summary",
            "operation": "download",
            "provider": provider,
            "bucket": bucket,
            "endpoint_url": endpoint_url,
            "region": region,
            "file_size_mib": size_mib,
            "attempt_count": len(group),
            "success_count": len(successes),
            "failure_count": len(group) - len(successes),
            "total_downloaded_bytes": sum(r["file_size_bytes"] for r in successes),
            "avg_throughput_mbps": round(sum(speeds) / len(speeds), 2) if speeds else None,
            "median_throughput_mbps": round(statistics.median(speeds), 2) if speeds else None,
            "min_throughput_mbps": round(min(speeds), 2) if speeds else None,
            "max_throughput_mbps": round(max(speeds), 2) if speeds else None,
            "total_elapsed_seconds": round(sum(elapsed), 3) if elapsed else None,
            "avg_elapsed_seconds": round(sum(elapsed) / len(elapsed), 3) if elapsed else None,
            "median_elapsed_seconds": round(statistics.median(elapsed), 3) if elapsed else None,
            "throughput_unit": "Mbps",
            "elapsed_unit": "seconds",
            "saved_at_utc": utc_now(),
        })
        summaries.append(summary)
        for path_key in ("summary", "combined", "summary_aggregate", "combined_aggregate"):
            write_jsonl(output_paths[path_key], summary)
    return summaries


def main() -> int:
    parser = argparse.ArgumentParser(description="List S3-compatible buckets, download selected .bin objects to /downloads, write JSONL metrics to /dataoutput, then garbage-collect downloads per provider.")
    parser.add_argument("--targets", default=DEFAULT_TARGETS, help="INI file with bucket/provider targets")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="Directory for JSONL/log output")
    parser.add_argument("--downloads-dir", default=DEFAULT_DOWNLOADS_DIR, help="Temporary download directory")
    parser.add_argument("--aws-path", default=DEFAULT_AWS_PATH, help="Path to aws CLI")
    parser.add_argument("--file-set", choices=["quick", "standard", "large", "full"], default="standard", help="quick=1MiB+100MiB, standard<=1GiB, large>=1GiB, full=all .bin objects")
    parser.add_argument("--providers", default="", help="Comma-separated target section names to run, empty means all enabled")
    parser.add_argument("--runs", type=int, default=1, help="Repeat count for each listed object")
    parser.add_argument("--max-files", type=int, default=0, help="Optional cap per provider after file-set filtering")
    parser.add_argument("--dry-run", action="store_true", help="List planned objects without downloading")
    parser.add_argument("--progress", dest="no_progress", action="store_false", help="Show aws-cli progress output")
    parser.add_argument("--verbose-aws", dest="only_show_errors", action="store_false", help="Do not pass --only-show-errors to aws-cli")
    parser.set_defaults(no_progress=True, only_show_errors=True)
    args = parser.parse_args()

    aws_path = args.aws_path
    if not Path(aws_path).exists():
        resolved = shutil.which(aws_path)
        if not resolved:
            raise SystemExit(f"aws CLI not found: {aws_path}")
        aws_path = resolved

    only = {name.strip() for name in args.providers.split(",") if name.strip()} or None
    targets = load_targets(Path(args.targets), only)
    run_id = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    output_dir = Path(args.output_dir)
    downloads_dir = Path(args.downloads_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir.mkdir(parents=True, exist_ok=True)

    output_paths = {
        "list_runs": output_dir / f"s3_download_list_runs_{run_id}.jsonl",
        "runs": output_dir / f"s3_download_speedtest_runs_{run_id}.jsonl",
        "summary": output_dir / f"s3_download_speedtest_summary_{run_id}.jsonl",
        "combined": output_dir / f"s3_download_speedtest_{run_id}.jsonl",
        "cleanup": output_dir / f"s3_download_cleanup_{run_id}.jsonl",
        "list_runs_aggregate": output_dir / "s3_download_list_runs.jsonl",
        "runs_aggregate": output_dir / "s3_download_speedtest_runs.jsonl",
        "summary_aggregate": output_dir / "s3_download_speedtest_summary.jsonl",
        "combined_aggregate": output_dir / "s3_download_speedtest.jsonl",
        "cleanup_aggregate": output_dir / "s3_download_cleanup.jsonl",
        "log": output_dir / f"s3_download_speedtest_{run_id}.log",
    }

    aws_version_text = aws_version(aws_path)
    base = base_record(args, run_id, aws_path, aws_version_text)

    print(f"run_id={run_id}")
    print(f"targets={len(targets)} runs={args.runs} file_set={args.file_set} downloads_dir={downloads_dir}")
    print(f"log={output_paths['log']}")
    print(f"runs_jsonl={output_paths['runs']}")
    print(f"summary_jsonl={output_paths['summary']}")

    download_records: list[dict[str, Any]] = []
    failures = 0

    with output_paths["log"].open("a", encoding="utf-8") as log:
        log.write(f"# s3 download speedtest run_id={run_id} started={utc_now()}\n")
        log.write(f"# aws_cli={aws_version_text}\n")
        for target in targets:
            provider = target.get("provider") or target["section"]
            prefix = (target.get("prefix") or f"codex-s3-test/{safe_name(provider)}").strip("/")
            env = build_env(target)
            provider_dir = downloads_dir / safe_name(provider)
            cleanup_before = cleanup_downloads(provider_dir)
            cleanup_before_record = dict(base)
            cleanup_before_record.update({
                "record_type": "s3_download_cleanup",
                "cleanup_phase": "before_provider",
                "provider": provider,
                "target_section": target["section"],
                "downloads_provider_dir": str(provider_dir),
                "saved_at_utc": utc_now(),
                **cleanup_before,
            })
            for key in ("cleanup", "cleanup_aggregate", "combined", "combined_aggregate"):
                write_jsonl(output_paths[key], cleanup_before_record)

            objects, list_proc, list_elapsed = list_objects(aws_path, target, prefix, env, args)
            list_record = dict(base)
            list_record.update({
                "record_type": "s3_download_list_run",
                "operation": "list_objects",
                "provider": provider,
                "target_section": target["section"],
                "bucket": target["bucket"],
                "endpoint_url": target.get("endpoint_url", ""),
                "region": target.get("region", ""),
                "profile": target.get("profile", ""),
                "auth_mode": auth_mode_label(target),
                "prefix": prefix,
                "listed_object_count": len(objects),
                "listed_total_bytes": sum(obj["size_bytes"] for obj in objects),
                "elapsed_seconds": round(list_elapsed, 3),
                "return_code": list_proc.returncode,
                "success": list_proc.returncode == 0,
                "stdout_tail": tail_text(list_proc.stdout),
                "stderr_tail": tail_text(list_proc.stderr),
                "saved_at_utc": utc_now(),
            })
            for key in ("list_runs", "list_runs_aggregate", "combined", "combined_aggregate"):
                write_jsonl(output_paths[key], list_record)
            print(f"LIST provider={provider} bucket={target['bucket']} objects={len(objects)} success={list_record['success']} elapsed={list_record['elapsed_seconds']}s")
            if not list_record["success"]:
                failures += 1
                if list_record["stderr_tail"]:
                    print("  " + list_record["stderr_tail"].strip().splitlines()[-1])
                continue
            if args.dry_run:
                for obj in objects:
                    print(f"DRY provider={provider} key={obj['key']} size={obj['size_bytes']}")
                cleanup_after = cleanup_downloads(provider_dir)
                cleanup_after_record = dict(cleanup_before_record)
                cleanup_after_record.update({"cleanup_phase": "after_provider", "saved_at_utc": utc_now(), **cleanup_after})
                for key in ("cleanup", "cleanup_aggregate", "combined", "combined_aggregate"):
                    write_jsonl(output_paths[key], cleanup_after_record)
                continue

            for repeat in range(1, args.runs + 1):
                for obj in objects:
                    key = obj["key"]
                    size_bytes = int(obj["size_bytes"])
                    dest = provider_dir / key.replace("/", "__")
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    cmd_display = aws_base(aws_path, target) + ["s3", "cp", f"s3://{target['bucket']}/{key}", str(dest)]
                    if args.no_progress:
                        cmd_display.append("--no-progress")
                    if args.only_show_errors:
                        cmd_display.append("--only-show-errors")
                    started = utc_now()
                    print(f"DOWNLOAD provider={provider} file={Path(key).name} size_mib={object_size_mib(size_bytes)}")
                    log.write(f"\n## download started={started} provider={provider} bucket={target['bucket']} key={key}\n")
                    log.write("$ " + " ".join(shlex.quote(part) for part in cmd_display) + "\n")
                    log.flush()
                    proc, elapsed = download_one(aws_path, target, key, dest, env, args)
                    ended = utc_now()
                    actual_size = dest.stat().st_size if dest.exists() else 0
                    success = proc.returncode == 0 and actual_size == size_bytes
                    throughput = (size_bytes * 8 / elapsed / 1_000_000) if elapsed > 0 and success else None
                    log.write(proc.stdout)
                    if proc.stderr:
                        log.write(proc.stderr)
                    log.write(f"## download ended={ended} return_code={proc.returncode} elapsed_seconds={elapsed:.3f} actual_size={actual_size}\n")
                    log.flush()
                    record = dict(base)
                    record.update({
                        "record_type": "s3_download_run",
                        "operation": "download",
                        "provider": provider,
                        "target_section": target["section"],
                        "bucket": target["bucket"],
                        "endpoint_url": target.get("endpoint_url", ""),
                        "region": target.get("region", ""),
                        "profile": target.get("profile", ""),
                        "auth_mode": auth_mode_label(target),
                        "prefix": prefix,
                        "object_key": key,
                        "destination_path": str(dest),
                        "filename": Path(key).name,
                        "file_size_bytes": size_bytes,
                        "file_size_mib": object_size_mib(size_bytes),
                        "downloaded_size_bytes": actual_size,
                        "repeat_index": repeat,
                        "started_at_utc": started,
                        "ended_at_utc": ended,
                        "elapsed_seconds": round(elapsed, 3),
                        "throughput_mbps": round(throughput, 2) if throughput is not None else None,
                        "throughput_unit": "Mbps",
                        "return_code": proc.returncode,
                        "success": success,
                        "stdout_tail": tail_text(proc.stdout),
                        "stderr_tail": tail_text(proc.stderr),
                        "log_path": str(output_paths["log"]),
                        "saved_at_utc": utc_now(),
                    })
                    download_records.append(record)
                    for key_name in ("runs", "runs_aggregate", "combined", "combined_aggregate"):
                        write_jsonl(output_paths[key_name], record)
                    if not success:
                        failures += 1

            cleanup_after = cleanup_downloads(provider_dir)
            cleanup_after_record = dict(base)
            cleanup_after_record.update({
                "record_type": "s3_download_cleanup",
                "cleanup_phase": "after_provider",
                "provider": provider,
                "target_section": target["section"],
                "downloads_provider_dir": str(provider_dir),
                "saved_at_utc": utc_now(),
                **cleanup_after,
            })
            for key_name in ("cleanup", "cleanup_aggregate", "combined", "combined_aggregate"):
                write_jsonl(output_paths[key_name], cleanup_after_record)
            print(f"CLEANUP provider={provider} deleted_bin_files={cleanup_after['deleted_bin_files']} deleted_bin_bytes={cleanup_after['deleted_bin_bytes']}")

    summaries = summarize_downloads(download_records, base, output_paths)
    print(f"completed_download_records={len(download_records)} summaries={len(summaries)} failures={failures}")
    print(f"combined_jsonl={output_paths['combined']}")
    print(f"aggregate_combined_jsonl={output_paths['combined_aggregate']}")
    return 2 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
