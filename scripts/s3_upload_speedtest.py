#!/usr/bin/env python3
"""S3-compatible upload speed test harness.

Reads bucket/provider targets from /testfiles/s3_targets.ini, uploads selected
/testfiles payloads with aws-cli, and writes JSONL/log output to /dataoutput.
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
DEFAULT_TESTFILES_DIR = "/testfiles"
DEFAULT_OUTPUT_DIR = "/dataoutput"
DEFAULT_PUBLIC_IP = "194.26.100.186"


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_name(value: str) -> str:
    cleaned = []
    for char in value.lower():
        if char.isalnum() or char in ("-", "_"):
            cleaned.append(char)
        else:
            cleaned.append("-")
    return "".join(cleaned).strip("-") or "unknown"


def bool_value(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "yes", "true", "on"}


def tail_text(text: str, limit: int = 2000) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[-limit:]


def write_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")


def load_targets(path: Path, only: set[str] | None = None) -> list[dict[str, str]]:
    parser = configparser.ConfigParser(interpolation=None)
    if not path.exists():
        raise SystemExit(f"Target config not found: {path}")
    parser.read(path)

    targets: list[dict[str, str]] = []
    for section in parser.sections():
        if only and section not in only:
            continue
        enabled = bool_value(parser.get(section, "enabled", fallback="false"))
        if not enabled:
            continue
        bucket = parser.get(section, "bucket", fallback="").strip()
        if not bucket or bucket.startswith("REPLACE_"):
            print(f"Skipping [{section}]: enabled but bucket is blank/placeholder", file=sys.stderr)
            continue
        target = {key: parser.get(section, key, fallback="").strip() for key in parser[section]}
        target.setdefault("provider", section)
        if not target.get("provider"):
            target["provider"] = section
        target["section"] = section
        target["bucket"] = bucket
        targets.append(target)
    return targets


def select_files(testfiles_dir: Path, file_set: str) -> list[Path]:
    files = sorted(testfiles_dir.glob("random_*.bin"), key=lambda p: (p.stat().st_size, p.name))
    if not files:
        raise SystemExit(f"No random_*.bin files found under {testfiles_dir}")

    mib = 1024 * 1024
    gib = 1024 * mib

    if file_set == "quick":
        one_mib = next((p for p in files if p.stat().st_size == mib), None)
        hundred_mib = next((p for p in files if p.stat().st_size == 100 * mib), None)
        selected = [p for p in (one_mib, hundred_mib) if p is not None]
    elif file_set == "standard":
        selected = [p for p in files if p.stat().st_size <= gib]
    elif file_set == "large":
        selected = [p for p in files if p.stat().st_size >= gib]
    elif file_set == "full":
        selected = files
    else:
        raise SystemExit(f"Unknown file set: {file_set}")

    if not selected:
        raise SystemExit(f"File set {file_set!r} selected no files")
    return selected


def aws_version(aws_path: str) -> str:
    try:
        return subprocess.check_output([aws_path, "--version"], text=True, stderr=subprocess.STDOUT).strip()
    except Exception as exc:  # pragma: no cover - diagnostic only
        return f"unknown: {exc}"


def base_record(args: argparse.Namespace, run_id: str, aws_path: str, aws_version_text: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "test_suite": TEST_SUITE,
        "run_id": run_id,
        "vm_hostname": socket.gethostname(),
        "vm_public_ip": DEFAULT_PUBLIC_IP,
        "aws_cli_path": aws_path,
        "aws_cli_version": aws_version_text,
        "testfiles_dir": str(Path(args.testfiles_dir).resolve()),
        "output_dir": str(Path(args.output_dir).resolve()),
        "file_set": args.file_set,
    }


def use_inline_credentials(target: dict[str, str]) -> bool:
    auth_mode = (target.get("auth_mode") or "auto").strip().lower()
    has_profile = bool(target.get("profile"))
    if auth_mode == "inline":
        return True
    if auth_mode == "profile":
        return False
    return not has_profile


def build_env(target: dict[str, str]) -> dict[str, str]:
    env = os.environ.copy()
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    region = target.get("region") or "us-east-1"
    env["AWS_DEFAULT_REGION"] = region
    env["AWS_REGION"] = region

    request_checksum = target.get("request_checksum_calculation") or "when_required"
    response_checksum = target.get("response_checksum_validation") or "when_required"
    env["AWS_REQUEST_CHECKSUM_CALCULATION"] = request_checksum
    env["AWS_RESPONSE_CHECKSUM_VALIDATION"] = response_checksum

    if use_inline_credentials(target):
        if target.get("access_key_id") and not target["access_key_id"].startswith("REPLACE_"):
            env["AWS_ACCESS_KEY_ID"] = target["access_key_id"]
        if target.get("secret_access_key") and not target["secret_access_key"].startswith("REPLACE_"):
            env["AWS_SECRET_ACCESS_KEY"] = target["secret_access_key"]
        if target.get("session_token"):
            env["AWS_SESSION_TOKEN"] = target["session_token"]
    return env


def build_upload_command(aws_path: str, target: dict[str, str], source: Path, key: str, args: argparse.Namespace) -> list[str]:
    cmd = [aws_path]
    profile = target.get("profile", "")
    endpoint_url = target.get("endpoint_url", "")
    region = target.get("region", "")
    if profile:
        cmd.extend(["--profile", profile])
    if endpoint_url:
        cmd.extend(["--endpoint-url", endpoint_url])
    if region:
        cmd.extend(["--region", region])
    cmd.extend(["s3", "cp", str(source), f"s3://{target['bucket']}/{key}"])
    if args.no_progress:
        cmd.append("--no-progress")
    if args.only_show_errors:
        cmd.append("--only-show-errors")
    extra_args = target.get("extra_args", "")
    if extra_args:
        cmd.extend(shlex.split(extra_args))
    return cmd


def build_delete_command(aws_path: str, target: dict[str, str], key: str) -> list[str]:
    cmd = [aws_path]
    if target.get("profile"):
        cmd.extend(["--profile", target["profile"]])
    if target.get("endpoint_url"):
        cmd.extend(["--endpoint-url", target["endpoint_url"]])
    if target.get("region"):
        cmd.extend(["--region", target["region"]])
    cmd.extend(["s3", "rm", f"s3://{target['bucket']}/{key}", "--only-show-errors"])
    return cmd


def summarize(records: list[dict[str, Any]], base: dict[str, Any], output_paths: dict[str, Path]) -> list[dict[str, Any]]:
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

    summaries: list[dict[str, Any]] = []
    for (provider, bucket, endpoint_url, region, file_size_mib), group in sorted(groups.items()):
        successes = [r for r in group if r["success"]]
        speeds = [r["throughput_mbps"] for r in successes]
        elapsed = [r["elapsed_seconds"] for r in successes]
        summary = dict(base)
        summary.update({
            "record_type": "s3_upload_summary",
            "operation": "upload",
            "provider": provider,
            "bucket": bucket,
            "endpoint_url": endpoint_url,
            "region": region,
            "file_size_mib": file_size_mib,
            "attempt_count": len(group),
            "success_count": len(successes),
            "failure_count": len(group) - len(successes),
            "total_uploaded_bytes": sum(r["file_size_bytes"] for r in successes),
            "avg_throughput_mbps": round(sum(speeds) / len(speeds), 2) if speeds else None,
            "median_throughput_mbps": round(statistics.median(speeds), 2) if speeds else None,
            "min_throughput_mbps": round(min(speeds), 2) if speeds else None,
            "max_throughput_mbps": round(max(speeds), 2) if speeds else None,
            "avg_elapsed_seconds": round(sum(elapsed) / len(elapsed), 3) if elapsed else None,
            "median_elapsed_seconds": round(statistics.median(elapsed), 3) if elapsed else None,
            "throughput_unit": "Mbps",
            "elapsed_unit": "seconds",
            "saved_at_utc": utc_now(),
        })
        summaries.append(summary)
        write_jsonl(output_paths["summary"], summary)
        write_jsonl(output_paths["combined"], summary)
        write_jsonl(output_paths["summary_aggregate"], summary)
        write_jsonl(output_paths["combined_aggregate"], summary)
    return summaries


def main() -> int:
    argp = argparse.ArgumentParser(description="Upload /testfiles payloads to S3-compatible targets and write JSONL output to /dataoutput.")
    argp.add_argument("--targets", default=DEFAULT_TARGETS, help="INI file with bucket/provider targets")
    argp.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="Directory for JSONL/log output")
    argp.add_argument("--testfiles-dir", default=DEFAULT_TESTFILES_DIR, help="Directory containing random_*.bin payloads")
    argp.add_argument("--aws-path", default=DEFAULT_AWS_PATH, help="Path to aws CLI")
    argp.add_argument("--file-set", choices=["quick", "standard", "large", "full"], default="standard", help="quick=1MiB+100MiB, standard<=1GiB, large>=1GiB, full=all files")
    argp.add_argument("--providers", default="", help="Comma-separated target section names to run, empty means all enabled")
    argp.add_argument("--runs", type=int, default=1, help="Repeat count per selected file")
    argp.add_argument("--delete-after-upload", action="store_true", help="Delete each object after upload measurement")
    argp.add_argument("--dry-run", action="store_true", help="Print planned uploads without uploading")
    argp.add_argument("--progress", dest="no_progress", action="store_false", help="Show aws-cli progress output")
    argp.add_argument("--verbose-aws", dest="only_show_errors", action="store_false", help="Do not pass --only-show-errors to aws-cli")
    argp.set_defaults(no_progress=True, only_show_errors=True)
    args = argp.parse_args()

    aws_path = args.aws_path
    if not Path(aws_path).exists():
        resolved = shutil.which(aws_path)
        if not resolved:
            raise SystemExit(f"aws CLI not found: {aws_path}")
        aws_path = resolved

    targets_path = Path(args.targets)
    only = {name.strip() for name in args.providers.split(",") if name.strip()} or None
    targets = load_targets(targets_path, only)
    files = select_files(Path(args.testfiles_dir), args.file_set)

    run_id = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_paths = {
        "runs": output_dir / f"s3_upload_speedtest_runs_{run_id}.jsonl",
        "summary": output_dir / f"s3_upload_speedtest_summary_{run_id}.jsonl",
        "combined": output_dir / f"s3_upload_speedtest_{run_id}.jsonl",
        "runs_aggregate": output_dir / "s3_upload_speedtest_runs.jsonl",
        "summary_aggregate": output_dir / "s3_upload_speedtest_summary.jsonl",
        "combined_aggregate": output_dir / "s3_upload_speedtest.jsonl",
        "uploaded_objects": output_dir / f"s3_uploaded_objects_{run_id}.jsonl",
        "log": output_dir / f"s3_upload_speedtest_{run_id}.log",
    }

    aws_version_text = aws_version(aws_path)
    base = base_record(args, run_id, aws_path, aws_version_text)

    print(f"run_id={run_id}")
    print(f"targets={len(targets)} files={len(files)} runs={args.runs} file_set={args.file_set}")
    print(f"log={output_paths['log']}")
    print(f"runs_jsonl={output_paths['runs']}")
    print(f"summary_jsonl={output_paths['summary']}")

    if args.dry_run:
        for target in targets:
            for path in files:
                print(f"DRY provider={target.get('provider')} bucket={target['bucket']} file={path.name} size={path.stat().st_size}")
        return 0

    run_records: list[dict[str, Any]] = []
    with output_paths["log"].open("a", encoding="utf-8") as log:
        log.write(f"# s3 upload speedtest run_id={run_id} started={utc_now()}\n")
        log.write(f"# aws_cli={aws_version_text}\n")
        for target in targets:
            provider = target.get("provider") or target["section"]
            prefix = (target.get("prefix") or f"codex-s3-test/{safe_name(provider)}").strip("/")
            env = build_env(target)
            for repeat in range(1, args.runs + 1):
                for source in files:
                    size_bytes = source.stat().st_size
                    size_mib = round(size_bytes / 1024 / 1024, 3)
                    key = f"{prefix}/{run_id}/{source.name}"
                    cmd = build_upload_command(aws_path, target, source, key, args)
                    safe_cmd = " ".join(shlex.quote(part) for part in cmd)
                    started = utc_now()
                    print(f"UPLOAD provider={provider} bucket={target['bucket']} file={source.name} size_mib={size_mib}")
                    log.write(f"\n## upload started={started} provider={provider} bucket={target['bucket']} key={key}\n")
                    log.write(f"$ {safe_cmd}\n")
                    log.flush()
                    t0 = time.monotonic()
                    proc = subprocess.run(cmd, text=True, capture_output=True, env=env)
                    elapsed_seconds = time.monotonic() - t0
                    ended = utc_now()
                    throughput_mbps = (size_bytes * 8 / elapsed_seconds / 1_000_000) if elapsed_seconds > 0 else None
                    log.write(proc.stdout)
                    if proc.stderr:
                        log.write(proc.stderr)
                    log.write(f"## upload ended={ended} return_code={proc.returncode} elapsed_seconds={elapsed_seconds:.3f}\n")
                    log.flush()

                    record = dict(base)
                    record.update({
                        "record_type": "s3_upload_run",
                        "operation": "upload",
                        "provider": provider,
                        "target_section": target["section"],
                        "bucket": target["bucket"],
                        "endpoint_url": target.get("endpoint_url", ""),
                        "region": target.get("region", ""),
                        "profile": target.get("profile", ""),
                        "prefix": prefix,
                        "object_key": key,
                        "source_path": str(source),
                        "filename": source.name,
                        "file_size_bytes": size_bytes,
                        "file_size_mib": size_mib,
                        "repeat_index": repeat,
                        "started_at_utc": started,
                        "ended_at_utc": ended,
                        "elapsed_seconds": round(elapsed_seconds, 3),
                        "throughput_mbps": round(throughput_mbps, 2) if throughput_mbps is not None else None,
                        "throughput_unit": "Mbps",
                        "return_code": proc.returncode,
                        "success": proc.returncode == 0,
                        "stdout_tail": tail_text(proc.stdout),
                        "stderr_tail": tail_text(proc.stderr),
                        "log_path": str(output_paths["log"]),
                        "saved_at_utc": utc_now(),
                    })
                    run_records.append(record)
                    write_jsonl(output_paths["runs"], record)
                    write_jsonl(output_paths["combined"], record)
                    write_jsonl(output_paths["runs_aggregate"], record)
                    write_jsonl(output_paths["combined_aggregate"], record)
                    if proc.returncode == 0:
                        object_record = {
                            **base,
                            "record_type": "s3_uploaded_object",
                            "provider": provider,
                            "bucket": target["bucket"],
                            "endpoint_url": target.get("endpoint_url", ""),
                            "region": target.get("region", ""),
                            "object_key": key,
                            "source_path": str(source),
                            "file_size_bytes": size_bytes,
                            "saved_at_utc": utc_now(),
                        }
                        write_jsonl(output_paths["uploaded_objects"], object_record)

                    if args.delete_after_upload and proc.returncode == 0:
                        delete_cmd = build_delete_command(aws_path, target, key)
                        log.write(f"$ {' '.join(shlex.quote(part) for part in delete_cmd)}\n")
                        delete_proc = subprocess.run(delete_cmd, text=True, capture_output=True, env=env)
                        log.write(delete_proc.stdout)
                        if delete_proc.stderr:
                            log.write(delete_proc.stderr)
                        log.write(f"## delete return_code={delete_proc.returncode}\n")
                        log.flush()

    summaries = summarize(run_records, base, output_paths)
    print(f"completed_records={len(run_records)} summaries={len(summaries)}")
    print(f"combined_jsonl={output_paths['combined']}")
    print(f"aggregate_combined_jsonl={output_paths['combined_aggregate']}")
    failures = [record for record in run_records if not record["success"]]
    if failures:
        print(f"failures={len(failures)}; inspect log={output_paths['log']}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
