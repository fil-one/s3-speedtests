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

The setup installs `traceroute`, `ping`, `jq`, `curl`, Python, `python3-docx`, Ookla `speedtest`, and AWS CLI. It also generates:

- `100 x 1 MiB` files
- `5 x 100 MiB` files
- `1 x 1 GiB` file
- `1 x 25 GiB` file
- `1 x 50 GiB` file

To install dependencies without generating the full payload set:

```bash
sudo ./scripts/setup_vm.sh --skip-files
```

## Provider Config

Setup seeds `/testfiles/s3_targets.ini` from `config/s3_targets.example.ini` if it does not already exist.

Edit `/testfiles/s3_targets.ini`, enable the providers you want, and add bucket names. For static-key providers, add access keys there and then write AWS CLI profiles:

```bash
./scripts/s3_write_profiles.py
```

For AWS SSO or temporary AWS credentials, configure the profile directly with AWS CLI and set `profile = aws` and `auth_mode = profile`.

## Common Runs

Check bucket access:

```bash
./scripts/s3_access_check.py
```

Network baseline:

```bash
RUNS=3 ./scripts/network_speedtest_ookla.sh
```

Provider traceroutes:

```bash
./scripts/s3_provider_traceroutes.sh
```

Upload tests:

```bash
./scripts/s3_upload_speedtest.sh --file-set standard
./scripts/s3_upload_speedtest.sh --file-set large
```

Download all uploaded files:

```bash
./scripts/s3_download_speedtest.sh --file-set full
```

Watch newest logs:

```bash
tail -f "$(ls -t /dataoutput/*.log | head -n 1)"
```

Build the DOCX summary report from `/dataoutput`:

```bash
./scripts/build_summary_report.py
```

The generated report is written under `/dataoutput/reports`.
