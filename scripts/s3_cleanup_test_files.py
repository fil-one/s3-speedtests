#!/usr/bin/env python3
from __future__ import annotations

import argparse
import configparser
import datetime as dt
import json
import os
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_AWS_PATH = "aws"
DEFAULT_TARGETS = Path("/testfiles/s3_targets.ini")
DEFAULT_OUTPUT_DIR = Path("/dataoutput")
SCHEMA_VERSION = "perf_test_v1"
TEST_SUITE = "s3_provider_transfer_test"


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def bool_value(value: str) -> bool:
    return value.strip().lower() in {"1", "yes", "true", "on", "enabled", "enable"}


def tail_text(value: str, limit: int = 4000) -> str:
    value = value or ""
    return value if len(value) <= limit else value[-limit:]


def write_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")


def provider_filter(value: str) -> set[str] | None:
    selected = {item.strip() for item in value.split(",") if item.strip()}
    return selected or None


def get_targets(path: Path, only: set[str] | None = None) -> list[dict[str, str]]:
    cfg = configparser.ConfigParser(interpolation=None)
    if not path.exists():
        raise SystemExit(f"Target config not found: {path}")
    cfg.read(path)
    targets: list[dict[str, str]] = []
    for section in cfg.sections():
        target = {k: cfg.get(section, k, fallback="").strip() for k in cfg[section]}
        target["section"] = section
        target["provider"] = target.get("provider") or section
        names = {section, target["provider"], target.get("profile", "")}
        if only and not (names & only):
            continue
        if not bool_value(target.get("enabled", "false")):
            continue
        bucket = target.get("bucket", "")
        if not bucket or bucket.startswith("REPLACE_"):
            print(f"skip {section}: bucket missing/placeholder", file=sys.stderr)
            continue
        targets.append(target)
    return targets


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


def env_for(target: dict[str, str]) -> dict[str, str]:
    env = os.environ.copy()
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    env["AWS_REQUEST_CHECKSUM_CALCULATION"] = target.get("request_checksum_calculation") or "when_required"
    env["AWS_RESPONSE_CHECKSUM_VALIDATION"] = target.get("response_checksum_validation") or "when_required"
    env.setdefault("AWS_MAX_ATTEMPTS", target.get("aws_max_attempts") or "11")
    region = target.get("region") or "us-east-1"
    env["AWS_DEFAULT_REGION"] = region
    env["AWS_REGION"] = region
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


def cleanup_prefix(target: dict[str, str], args: argparse.Namespace) -> str:
    if args.entire_bucket:
        return ""
    if args.prefix is not None:
        return args.prefix.strip("/")
    return (target.get("prefix") or "").strip("/")


def build_command(aws_path: str, target: dict[str, str], prefix: str, dry_run: bool) -> list[str]:
    uri = f"s3://{target['bucket']}"
    if prefix:
        uri += f"/{prefix}"
    cmd = aws_base(aws_path, target) + ["s3", "rm", uri, "--recursive"]
    if dry_run:
        cmd.append("--dryrun")
    extra_args = target.get("extra_args", "")
    if extra_args:
        cmd.extend(shlex.split(extra_args))
    return cmd


def run_cleanup(base: dict[str, Any], target: dict[str, str], cmd: list[str], env: dict[str, str], out_paths: dict[str, Path], dry_run: bool, prefix: str) -> dict[str, Any]:
    started = utc_now()
    t0 = time.monotonic()
    proc = subprocess.run(cmd, text=True, capture_output=True, env=env)
    elapsed = time.monotonic() - t0
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    line_count = len([line for line in stdout.splitlines() if line.strip()])
    record = dict(base)
    record.update({
        "record_type": "s3_cleanup_test_files",
        "provider": target["provider"],
        "target_section": target["section"],
        "bucket": target["bucket"],
        "prefix": prefix,
        "endpoint_url": target.get("endpoint_url", ""),
        "region": target.get("region", ""),
        "profile": target.get("profile", ""),
        "auth_mode": auth_mode_label(target),
        "dry_run": dry_run,
        "started_at_utc": started,
        "ended_at_utc": utc_now(),
        "elapsed_seconds": round(elapsed, 3),
        "return_code": proc.returncode,
        "success": proc.returncode == 0,
        "output_line_count": line_count,
        "stdout_tail": tail_text(stdout),
        "stderr_tail": tail_text(stderr),
        "command_redacted": " ".join(shlex.quote(part) for part in cmd),
    })
    write_jsonl(out_paths["run"], record)
    write_jsonl(out_paths["aggregate"], record)
    action = "would delete" if dry_run else "deleted"
    print(f"{target['section']} {action}: bucket={target['bucket']} prefix={prefix or '<entire bucket>'} lines={line_count} rc={proc.returncode} elapsed={record['elapsed_seconds']}s")
    if stdout.strip():
        print(stdout, end="")
    if stderr.strip():
        print(stderr, end="", file=sys.stderr)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description="Delete generated S3 speed-test objects from enabled provider buckets.")
    parser.add_argument("--targets", default=str(DEFAULT_TARGETS), help="INI file with bucket/provider targets")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for JSONL cleanup audit output")
    parser.add_argument("--aws-path", default=DEFAULT_AWS_PATH, help="Path to aws CLI")
    parser.add_argument("--providers", default="", help="Comma-separated target sections/providers/profiles to clean, empty means all enabled")
    parser.add_argument("--prefix", default=None, help="Override prefix to delete. Default uses each target's prefix.")
    parser.add_argument("--entire-bucket", action="store_true", help="Delete all objects in each selected bucket, ignoring configured prefixes")
    parser.add_argument("--execute", action="store_true", help="Actually delete objects. Default is dry-run.")
    args = parser.parse_args()

    if args.entire_bucket and args.prefix:
        raise SystemExit("Use either --prefix or --entire-bucket, not both.")

    only = provider_filter(args.providers)
    targets_path = Path(args.targets)
    output_dir = Path(args.output_dir)
    targets = get_targets(targets_path, only)
    if not targets:
        raise SystemExit("No enabled targets matched.")

    dry_run = not args.execute
    run_id = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    output_dir.mkdir(parents=True, exist_ok=True)
    out_paths = {
        "run": output_dir / f"s3_cleanup_test_files_{run_id}.jsonl",
        "aggregate": output_dir / "s3_cleanup_test_files.jsonl",
    }
    try:
        aws_version = subprocess.check_output([args.aws_path, "--version"], text=True, stderr=subprocess.STDOUT).strip()
    except Exception as exc:
        aws_version = f"unknown: {exc}"

    base = {
        "schema_version": SCHEMA_VERSION,
        "test_suite": TEST_SUITE,
        "run_id": run_id,
        "vm_hostname": socket.gethostname(),
        "aws_cli_path": args.aws_path,
        "aws_cli_version": aws_version,
        "targets_file": str(targets_path),
        "output_dir": str(output_dir),
        "saved_at_utc": utc_now(),
    }

    mode = "DRY RUN" if dry_run else "EXECUTE"
    print(f"run_id={run_id} mode={mode} targets={len(targets)} output={out_paths['run']}")
    if not dry_run:
        print("WARNING: deleting matching objects from selected buckets.")

    records = []
    for target in targets:
        prefix = cleanup_prefix(target, args)
        cmd = build_command(args.aws_path, target, prefix, dry_run)
        records.append(run_cleanup(base, target, cmd, env_for(target), out_paths, dry_run, prefix))

    failures = [record for record in records if not record["success"]]
    print(f"completed={len(records)} failures={len(failures)} aggregate={out_paths['aggregate']}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
