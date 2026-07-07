# Fil.one S3 Speed Tests

Reusable VM benchmark harness for comparing S3-compatible providers from a Linux test node.

The scripts write JSONL and logs to `/dataoutput`, use generated payloads in `/testfiles`, and use `/downloads` as disposable download scratch space.

## VM Setup

Run this on a fresh apt-based Linux VM:

```bash
git clone <your-repo-url> filone-speedtests
cd filone-speedtests
sudo ./scripts/setup_vm.sh
```

The setup installs `traceroute`, `ping`, `jq`, `curl`, Python, `python3-docx`, `python3-reportlab`, Ookla `speedtest`, and AWS CLI. It also generates:

- `100 x 1 MiB` files
- `5 x 100 MiB` files
- `1 x 1 GiB` file
- `1 x 25 GiB` file
- `1 x 50 GiB` file

To install dependencies without generating the full payload set:

```bash
sudo ./scripts/setup_vm.sh --skip-files
```

To create or repair only the `/testfiles` payload set after dependencies are already installed:

```bash
sudo ./scripts/setup_vm.sh --files-only
```

To regenerate files even when matching filenames already exist:

```bash
sudo ./scripts/setup_vm.sh --files-only --force-files
```

## Provider Config

Setup seeds `/testfiles/s3_targets.ini` from `config/s3_targets.example.ini` if it does not already exist.

Edit `/testfiles/s3_targets.ini`, enable the providers you want, and add bucket names. For static-key providers, add access keys there and then write AWS CLI profiles:

```bash
./scripts/s3_write_profiles.py
```

For AWS SSO or temporary AWS credentials, configure the profile directly with AWS CLI and set `profile = aws` and `auth_mode = profile`.

## Common Runs

### Run Everything

Warning: `run_all` executes the full benchmark workflow after setup. It writes AWS CLI profiles from `/testfiles/s3_targets.ini`, checks access, uploads the standard and large file sets, downloads all uploaded objects, runs network tests and traceroutes, and then builds the default DOCX report. This can move more than 75 GiB per enabled provider in each direction and may incur cloud egress, storage, API, or bandwidth costs.

Run the complete workflow:

```bash
./scripts/run_all
```

Run only selected providers:

```bash
./scripts/run_all --providers aws,wasabi
```

Show AWS CLI transfer progress in the tmux console during upload and download steps:

```bash
./scripts/run_all --progress
```

The same behavior can be enabled with `TRANSFER_PROGRESS=1`:

```bash
TRANSFER_PROGRESS=1 ./scripts/run_all --providers aws,wasabi
```

By default, `run_all` uses minimal waits: `PAUSE_SECONDS=5`, `NETWORK_RUNS=1`, and `NETWORK_SLEEP_SECONDS=5`. Override them when you want more samples:

```bash
NETWORK_RUNS=3 NETWORK_SLEEP_SECONDS=30 PAUSE_SECONDS=10 ./scripts/run_all
```

The orchestration log is written to `/dataoutput/run_all_<timestamp>.log`.

The final report step prompts for the source node provider/name and location unless those values are supplied through environment variables:

```bash
SOURCE_NODE_PROVIDER="AWS EC2" \
SOURCE_NODE_LOCATION="eu-west-3 | Paris, France" \
SOURCE_NODE_NETWORK="1 Gbit connection" \
./scripts/run_all --providers aws,wasabi
```

### Individual Commands

Check bucket access:

```bash
./scripts/s3_access_check.py
```

Network baseline:

```bash
RUNS=1 ./scripts/network_speedtest_ookla.sh
```

Provider traceroutes:

```bash
./scripts/s3_provider_traceroutes.sh
```

Upload standard and large file sets together:

```bash
./scripts/s3_upload_speedtest.sh --file-set full
```

Show upload progress and per-object elapsed time / throughput in the console:

```bash
./scripts/s3_upload_speedtest.sh --file-set full --progress
```

Run upload file sets separately when you want independent standard and large result files:

```bash
./scripts/s3_upload_speedtest.sh --file-set standard
./scripts/s3_upload_speedtest.sh --file-set large
```

Download all uploaded files:

```bash
./scripts/s3_download_speedtest.sh --file-set full
```

Show download progress and per-object elapsed time / throughput in the console:

```bash
./scripts/s3_download_speedtest.sh --file-set full --progress
```

With `--progress`, the AWS CLI progress display streams to the terminal. The scripts still write JSONL metrics and print a final `DONE ... elapsed=... throughput_mbps=...` line for each object. When progress is enabled through `run_all`, the orchestration log also captures the console stream because `run_all` uses `tee`.

Watch newest logs:

```bash
tail -f "$(ls -t /dataoutput/*.log | head -n 1)"
```

## Report Builder

Build a summary report from JSONL output in `/dataoutput`:

```bash
./scripts/build_summary_report.py
```

DOCX is the default format. Generated reports are written under `/dataoutput/reports`.

Choose a report format explicitly:

```bash
./scripts/build_summary_report.py --format docx
./scripts/build_summary_report.py --format pdf
./scripts/build_summary_report.py --format both
```

The report builder loads the latest available benchmark artifacts:

- `/dataoutput/network_speedtest_ookla_summary.jsonl`
- `/dataoutput/s3_upload_speedtest_summary.jsonl` or the latest `s3_upload_speedtest_summary_*.jsonl`
- `/dataoutput/s3_download_speedtest_summary.jsonl` or the latest `s3_download_speedtest_summary_*.jsonl`
- `/dataoutput/s3_provider_traceroutes.jsonl` or the latest `s3_provider_traceroutes_*.jsonl`

Upload and download ranking cells include median throughput, average throughput, total elapsed time, and median elapsed time when available. The traceroute section includes the provider summary table plus the full CLI traceroute command output with hop lines.

Provider names and regions in the upload/download result tables are read from `/testfiles/s3_targets.ini`, so changing a bucket target region there changes the report label on the next report build. Use `--targets` to point at a different target config.

When run interactively, the report builder prompts for the source node provider/name and source node location. It auto-detects hostname, vCPU count, and RAM from the VM.

Prompted run:

```bash
./scripts/build_summary_report.py
```

Expected prompts:

```text
Source node provider/name:
Source node location:
```

For non-interactive runs:

```bash
./scripts/build_summary_report.py \
  --format docx \
  --source-provider "AWS EC2" \
  --source-location "eu-west-3 | Paris, France" \
  --node-network "1 Gbit connection" \
  --no-prompt
```

You can also use environment variables:

```bash
SOURCE_NODE_PROVIDER="AWS EC2" \
SOURCE_NODE_LOCATION="eu-west-3 | Paris, France" \
SOURCE_NODE_NETWORK="1 Gbit connection" \
./scripts/build_summary_report.py --format pdf --no-prompt
```

Use explicit input and output directories when rebuilding a report from archived results:

```bash
./scripts/build_summary_report.py \
  --format both \
  --targets /testfiles/s3_targets.ini \
  --data-dir /dataoutput \
  --output-dir /dataoutput/reports
```
