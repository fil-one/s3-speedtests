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

DEFAULT_AWS_PATH = "aws"
DEFAULT_TARGETS = Path("/testfiles/s3_targets.ini")
DEFAULT_OUTPUT_DIR = Path("/dataoutput")
SCHEMA_VERSION = "perf_test_v1"
TEST_SUITE = "s3_provider_transfer_test"
VM_PUBLIC_IP = "194.26.100.186"


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def tail_text(value: str, limit: int = 2000) -> str:
    value = value or ""
    return value if len(value) <= limit else value[-limit:]


def bool_value(value: str) -> bool:
    return value.strip().lower() in {"1", "yes", "true", "on", "enabled", "enable"}


def provider_filter(*values: str) -> set[str] | None:
    selected = set()
    for value in values:
        selected.update(x.strip() for x in value.split(",") if x.strip())
    return selected or None


def safe_name(value: str) -> str:
    out = []
    for ch in value.lower():
        out.append(ch if ch.isalnum() or ch in "-_" else "-")
    return "".join(out).strip("-") or "unknown"


def write_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")


def get_targets(path: Path, only: set[str] | None = None) -> list[dict[str, str]]:
    cfg = configparser.ConfigParser(interpolation=None)
    if not path.exists():
        raise SystemExit(f"Target config not found: {path}")
    cfg.read(path)
    targets = []
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
    env.setdefault("AWS_MAX_ATTEMPTS", target.get("aws_max_attempts") or "6")
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


def aws_base(aws_path: str, target: dict[str, str], debug_aws: bool = False) -> list[str]:
    cmd = [aws_path]
    if debug_aws:
        cmd.append("--debug")
    if target.get("profile"):
        cmd.extend(["--profile", target["profile"]])
    if target.get("endpoint_url"):
        cmd.extend(["--endpoint-url", target["endpoint_url"]])
    if target.get("region"):
        cmd.extend(["--region", target["region"]])
    return cmd


def run_step(base: dict, target: dict[str, str], operation: str, cmd: list[str], env: dict[str, str], out_paths: dict[str, Path], verbose: bool = False) -> dict:
    started = utc_now()
    if verbose:
        print("$ " + " ".join(shlex.quote(part) for part in cmd))
    t0 = time.monotonic()
    proc = subprocess.run(cmd, text=True, capture_output=True, env=env)
    elapsed = time.monotonic() - t0
    record = dict(base)
    record.update({
        "record_type": "s3_access_check",
        "operation": operation,
        "provider": target["provider"],
        "target_section": target["section"],
        "bucket": target["bucket"],
        "endpoint_url": target.get("endpoint_url", ""),
        "region": target.get("region", ""),
        "profile": target.get("profile", ""),
        "auth_mode": auth_mode_label(target),
        "started_at_utc": started,
        "ended_at_utc": utc_now(),
        "elapsed_seconds": round(elapsed, 3),
        "return_code": proc.returncode,
        "success": proc.returncode == 0,
        "stdout_tail": tail_text(proc.stdout),
        "stderr_tail": tail_text(proc.stderr),
        "command_redacted": " ".join(shlex.quote(part) for part in cmd),
    })
    write_jsonl(out_paths["run"], record)
    write_jsonl(out_paths["aggregate"], record)
    print(f"{target['provider']} {operation}: {'ok' if record['success'] else 'FAIL'} ({record['elapsed_seconds']}s)")
    if verbose:
        if proc.stdout:
            print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n")
        if proc.stderr:
            print(proc.stderr, end="" if proc.stderr.endswith("\n") else "\n", file=sys.stderr)
    elif not record["success"] and record["stderr_tail"]:
        print("  ", record["stderr_tail"].strip().splitlines()[-1])
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description="Check S3-compatible provider bucket access and write JSONL output.")
    parser.add_argument("provider_names", nargs="?", default="", help="Optional comma-separated provider sections to test; kept for compatibility")
    parser.add_argument("--providers", default="", help="Comma-separated provider sections/provider names/profiles to test")
    parser.add_argument("--targets", default=str(DEFAULT_TARGETS), help="INI file with bucket/provider targets")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for JSONL output")
    parser.add_argument("--aws-path", default=DEFAULT_AWS_PATH, help="Path to aws CLI")
    parser.add_argument("--verbose", action="store_true", help="Print each AWS CLI command plus stdout/stderr")
    parser.add_argument("--debug-aws", action="store_true", help="Pass --debug to AWS CLI commands; very noisy")
    args = parser.parse_args()

    only = provider_filter(args.provider_names, args.providers)
    targets_path = Path(args.targets)
    output_dir = Path(args.output_dir)
    targets = get_targets(targets_path, only)
    run_id = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    output_dir.mkdir(parents=True, exist_ok=True)
    probe = output_dir / f"s3_access_probe_{run_id}.txt"
    probe.write_text(f"codex s3 access probe {run_id}\n", encoding="utf-8")
    out_paths = {
        "run": output_dir / f"s3_access_check_{run_id}.jsonl",
        "summary": output_dir / f"s3_access_check_summary_{run_id}.jsonl",
        "aggregate": output_dir / "s3_access_check.jsonl",
        "summary_aggregate": output_dir / "s3_access_check_summary.jsonl",
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
        "vm_public_ip": VM_PUBLIC_IP,
        "aws_cli_path": args.aws_path,
        "aws_cli_version": aws_version,
        "targets_file": str(targets_path),
        "saved_at_utc": utc_now(),
    }
    print(f"run_id={run_id} targets={len(targets)} output={out_paths['run']}")
    all_records = []
    for target in targets:
        env = env_for(target)
        prefix = (target.get("prefix") or f"codex-s3-test/{safe_name(target['provider'])}").strip("/")
        key = f"{prefix}/access-check/{run_id}-{safe_name(target['provider'])}.txt"
        base_cmd = aws_base(args.aws_path, target, debug_aws=args.debug_aws)
        steps = [
            ("head_bucket", base_cmd + ["s3api", "head-bucket", "--bucket", target["bucket"]]),
            ("put_object", base_cmd + ["s3api", "put-object", "--bucket", target["bucket"], "--key", key, "--body", str(probe)]),
            ("head_object", base_cmd + ["s3api", "head-object", "--bucket", target["bucket"], "--key", key]),
            ("delete_object", base_cmd + ["s3api", "delete-object", "--bucket", target["bucket"], "--key", key]),
        ]
        records = [run_step(base, target, op, cmd, env, out_paths, verbose=args.verbose or args.debug_aws) for op, cmd in steps]
        all_records.extend(records)
        summary = dict(base)
        summary.update({
            "record_type": "s3_access_check_summary",
            "provider": target["provider"],
            "target_section": target["section"],
            "bucket": target["bucket"],
            "endpoint_url": target.get("endpoint_url", ""),
            "region": target.get("region", ""),
            "profile": target.get("profile", ""),
            "auth_mode": auth_mode_label(target),
            "operation_count": len(records),
            "success_count": sum(1 for r in records if r["success"]),
            "failure_count": sum(1 for r in records if not r["success"]),
            "all_success": all(r["success"] for r in records),
            "saved_at_utc": utc_now(),
        })
        write_jsonl(out_paths["summary"], summary)
        write_jsonl(out_paths["summary_aggregate"], summary)
    print(f"summary={out_paths['summary']}")
    failures = [r for r in all_records if not r["success"]]
    return 1 if failures else 0

if __name__ == "__main__":
    raise SystemExit(main())
