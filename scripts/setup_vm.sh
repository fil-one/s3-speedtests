#!/usr/bin/env bash
set -euo pipefail

TESTFILES_DIR="${TESTFILES_DIR:-/testfiles}"
OUTPUT_DIR="${OUTPUT_DIR:-/dataoutput}"
DOWNLOADS_DIR="${DOWNLOADS_DIR:-/downloads}"
TARGETS_FILE="${TARGETS_FILE:-${TESTFILES_DIR}/s3_targets.ini}"
FORCE_FILES=0
SKIP_FILES=0
FILES_ONLY=0

usage() {
  cat <<'USAGE'
Usage: scripts/setup_vm.sh [--force-files] [--skip-files] [--files-only]

Installs benchmark dependencies and prepares:
  /testfiles    random upload payloads and local target config
  /dataoutput   JSONL/log/report output
  /downloads    temporary download test location

Environment overrides:
  TESTFILES_DIR=/testfiles OUTPUT_DIR=/dataoutput DOWNLOADS_DIR=/downloads

Options:
  --force-files  Regenerate payloads even when existing files have the right size
  --skip-files   Install dependencies and create directories, but do not generate payloads
  --files-only   Create directories/config and generate payloads without installing packages
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force-files)
      FORCE_FILES=1
      shift
      ;;
    --skip-files)
      SKIP_FILES=1
      shift
      ;;
    --files-only)
      FILES_ONLY=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$(id -u)" -eq 0 ]]; then
  SUDO=()
else
  SUDO=(sudo)
fi

if ! command -v apt-get >/dev/null 2>&1; then
  echo "ERROR: this setup script expects an apt-based Linux VM." >&2
  exit 1
fi

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SPEEDTEST_VERSION="${SPEEDTEST_VERSION:-1.2.0}"

prepare_directories() {
  echo "Creating benchmark directories..."
  "${SUDO[@]}" mkdir -p "$TESTFILES_DIR" "$OUTPUT_DIR" "$DOWNLOADS_DIR"
  "${SUDO[@]}" chmod 755 "$TESTFILES_DIR" "$OUTPUT_DIR" "$DOWNLOADS_DIR"
  if [[ "$(id -u)" -ne 0 ]]; then
    "${SUDO[@]}" chown "$(id -u):$(id -g)" "$TESTFILES_DIR" "$OUTPUT_DIR" "$DOWNLOADS_DIR"
  fi

  if [[ ! -f "$TARGETS_FILE" && -f "${repo_root}/config/s3_targets.example.ini" ]]; then
    echo "Seeding target config: $TARGETS_FILE"
    "${SUDO[@]}" cp "${repo_root}/config/s3_targets.example.ini" "$TARGETS_FILE"
    "${SUDO[@]}" chmod 600 "$TARGETS_FILE"
    if [[ "$(id -u)" -ne 0 ]]; then
      "${SUDO[@]}" chown "$(id -u):$(id -g)" "$TARGETS_FILE"
    fi
  fi
}

bytes_available() {
  df -Pk "$TESTFILES_DIR" | awk 'NR == 2 {print $4 * 1024}'
}

generate_file() {
  local path="$1"
  local mib="$2"
  local expected_bytes=$((mib * 1024 * 1024))
  local current_bytes=0
  if [[ -f "$path" ]]; then
    current_bytes="$(stat -c '%s' "$path")"
  fi
  if [[ "$FORCE_FILES" -eq 0 && "$current_bytes" -eq "$expected_bytes" ]]; then
    echo "exists size_ok ${path}"
    return
  fi

  echo "generating ${path} (${mib} MiB)"
  local tmp="${path}.partial"
  "${SUDO[@]}" rm -f "$tmp"
  set +o pipefail
  openssl enc -aes-256-ctr -pass "pass:filone-speedtest-${path}" -nosalt < /dev/zero \
    | "${SUDO[@]}" dd of="$tmp" bs=1M count="$mib" iflag=fullblock status=progress
  local dd_status="${PIPESTATUS[1]}"
  set -o pipefail
  if [[ "$dd_status" -ne 0 ]]; then
    echo "ERROR: failed generating ${path}" >&2
    "${SUDO[@]}" rm -f "$tmp"
    exit "$dd_status"
  fi
  "${SUDO[@]}" mv "$tmp" "$path"
  "${SUDO[@]}" chmod 644 "$path"
}

generate_test_files() {
  if [[ "$SKIP_FILES" -ne 0 ]]; then
    echo "Skipping test payload generation because --skip-files was provided."
    return
  fi

  for required in df awk stat openssl dd seq; do
    if ! command -v "$required" >/dev/null 2>&1; then
      echo "ERROR: required command not found for payload generation: $required" >&2
      exit 1
    fi
  done

  required_bytes=$((90 * 1024 * 1024 * 1024))
  available="$(bytes_available)"
  if [[ "$available" -lt "$required_bytes" ]]; then
    echo "WARNING: less than 90 GiB free under $TESTFILES_DIR; full payload generation may fail." >&2
  fi

  for i in $(seq -w 1 100); do
    generate_file "${TESTFILES_DIR}/random_${i}_1mib.bin" 1
  done
  for i in $(seq 1 5); do
    generate_file "${TESTFILES_DIR}/random_${i}_100mib.bin" 100
  done
  generate_file "${TESTFILES_DIR}/random_001_1gib.bin" 1024
  generate_file "${TESTFILES_DIR}/random_001_25gib.bin" 25600
  generate_file "${TESTFILES_DIR}/random_001_50gib.bin" 51200
}

write_setup_manifest() {
  cat > "${OUTPUT_DIR}/setup_manifest.jsonl" <<EOF
{"record_type":"setup_manifest","testfiles_dir":"${TESTFILES_DIR}","output_dir":"${OUTPUT_DIR}","downloads_dir":"${DOWNLOADS_DIR}","targets_file":"${TARGETS_FILE}","saved_at_utc":"$(date -u +%Y-%m-%dT%H:%M:%SZ)"}
EOF
}

remove_ookla_packagecloud_repo() {
  local list_file
  shopt -s nullglob
  for list_file in /etc/apt/sources.list.d/*ookla* /etc/apt/sources.list.d/*speedtest*; do
    if [[ -f "$list_file" ]] && grep -qi 'packagecloud.io/ookla/speedtest-cli' "$list_file"; then
      echo "Removing unsupported/stale Ookla apt source: $list_file"
      "${SUDO[@]}" rm -f "$list_file"
    fi
  done
  shopt -u nullglob
}

install_speedtest_from_tarball() {
  local arch
  arch="$(uname -m)"
  case "$arch" in
    x86_64|amd64) arch="x86_64" ;;
    aarch64|arm64) arch="aarch64" ;;
    *)
      echo "ERROR: unsupported Ookla speedtest tarball architecture: $arch" >&2
      return 1
      ;;
  esac

  local tmpdir
  local url
  tmpdir="$(mktemp -d)"
  url="${SPEEDTEST_TARBALL_URL:-https://install.speedtest.net/app/cli/ookla-speedtest-${SPEEDTEST_VERSION}-linux-${arch}.tgz}"
  echo "Installing Ookla speedtest CLI from tarball: $url"
  curl -fsSL "$url" -o "${tmpdir}/speedtest.tgz"
  tar -xzf "${tmpdir}/speedtest.tgz" -C "$tmpdir"
  "${SUDO[@]}" install -m 0755 "${tmpdir}/speedtest" /usr/local/bin/speedtest
  rm -rf "$tmpdir"
}

install_speedtest_cli() {
  echo "Installing Ookla speedtest CLI..."
  "${SUDO[@]}" apt-get remove -y speedtest-cli >/dev/null 2>&1 || true
  hash -r

  if command -v speedtest >/dev/null 2>&1 && speedtest --version 2>/dev/null | grep -qi 'ookla'; then
    speedtest --version
    return 0
  fi

  if curl -fsSL https://packagecloud.io/install/repositories/ookla/speedtest-cli/script.deb.sh | "${SUDO[@]}" bash \
    && "${SUDO[@]}" apt-get update \
    && "${SUDO[@]}" apt-get install -y speedtest; then
    hash -r
    speedtest --version
    return 0
  fi

  echo "WARNING: Ookla packagecloud install failed; falling back to standalone tarball." >&2
  remove_ookla_packagecloud_repo
  "${SUDO[@]}" apt-get update
  install_speedtest_from_tarball
  hash -r
  speedtest --version
}

prepare_directories

if [[ "$FILES_ONLY" -eq 1 ]]; then
  generate_test_files
  write_setup_manifest
  echo
  echo "File setup complete."
  echo "testfiles_dir=$TESTFILES_DIR"
  echo "output_dir=$OUTPUT_DIR"
  echo "downloads_dir=$DOWNLOADS_DIR"
  echo "targets_file=$TARGETS_FILE"
  exit 0
fi

remove_ookla_packagecloud_repo

echo "Installing base dependencies..."
"${SUDO[@]}" apt-get update
"${SUDO[@]}" apt-get install -y \
  ca-certificates \
  coreutils \
  curl \
  dnsutils \
  gnupg \
  iproute2 \
  iputils-ping \
  jq \
  less \
  openssl \
  python3 \
  python3-docx \
  python3-pip \
  python3-venv \
  traceroute \
  unzip

install_speedtest_cli

install_aws_cli_v2() {
  local arch
  arch="$(uname -m)"
  case "$arch" in
    x86_64|amd64) arch="x86_64" ;;
    aarch64|arm64) arch="aarch64" ;;
    *)
      echo "WARNING: unsupported AWS CLI v2 architecture '$arch'; installing distro awscli package instead." >&2
      "${SUDO[@]}" apt-get install -y awscli
      return
      ;;
  esac

  local tmpdir
  tmpdir="$(mktemp -d)"
  curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${arch}.zip" -o "${tmpdir}/awscliv2.zip"
  unzip -q "${tmpdir}/awscliv2.zip" -d "$tmpdir"
  if command -v aws >/dev/null 2>&1; then
    "${SUDO[@]}" "${tmpdir}/aws/install" --update
  else
    "${SUDO[@]}" "${tmpdir}/aws/install"
  fi
  rm -rf "$tmpdir"
}

echo "Installing AWS CLI..."
if ! command -v aws >/dev/null 2>&1; then
  install_aws_cli_v2
else
  aws --version
fi

generate_test_files
write_setup_manifest

echo
echo "Setup complete."
echo "testfiles_dir=$TESTFILES_DIR"
echo "output_dir=$OUTPUT_DIR"
echo "downloads_dir=$DOWNLOADS_DIR"
echo "targets_file=$TARGETS_FILE"
echo "aws=$(command -v aws || true)"
echo "speedtest=$(command -v speedtest || true)"
