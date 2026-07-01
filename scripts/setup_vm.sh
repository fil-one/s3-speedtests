#!/usr/bin/env bash
set -euo pipefail

TESTFILES_DIR="${TESTFILES_DIR:-/testfiles}"
OUTPUT_DIR="${OUTPUT_DIR:-/dataoutput}"
DOWNLOADS_DIR="${DOWNLOADS_DIR:-/downloads}"
TARGETS_FILE="${TARGETS_FILE:-${TESTFILES_DIR}/s3_targets.ini}"
FORCE_FILES=0
SKIP_FILES=0

usage() {
  cat <<'USAGE'
Usage: scripts/setup_vm.sh [--force-files] [--skip-files]

Installs benchmark dependencies and prepares:
  /testfiles    random upload payloads and local target config
  /dataoutput   JSONL/log/report output
  /downloads    temporary download test location

Environment overrides:
  TESTFILES_DIR=/testfiles OUTPUT_DIR=/dataoutput DOWNLOADS_DIR=/downloads
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

echo "Installing Ookla speedtest CLI..."
"${SUDO[@]}" apt-get remove -y speedtest-cli >/dev/null 2>&1 || true
hash -r
curl -fsSL https://packagecloud.io/install/repositories/ookla/speedtest-cli/script.deb.sh | "${SUDO[@]}" bash
"${SUDO[@]}" apt-get install -y speedtest
hash -r

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

echo "Creating benchmark directories..."
"${SUDO[@]}" mkdir -p "$TESTFILES_DIR" "$OUTPUT_DIR" "$DOWNLOADS_DIR"
"${SUDO[@]}" chmod 755 "$TESTFILES_DIR" "$OUTPUT_DIR" "$DOWNLOADS_DIR"
if [[ "$(id -u)" -ne 0 ]]; then
  "${SUDO[@]}" chown "$(id -u):$(id -g)" "$TESTFILES_DIR" "$OUTPUT_DIR" "$DOWNLOADS_DIR"
fi

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ ! -f "$TARGETS_FILE" && -f "${repo_root}/config/s3_targets.example.ini" ]]; then
  echo "Seeding target config: $TARGETS_FILE"
  "${SUDO[@]}" cp "${repo_root}/config/s3_targets.example.ini" "$TARGETS_FILE"
  "${SUDO[@]}" chmod 600 "$TARGETS_FILE"
  if [[ "$(id -u)" -ne 0 ]]; then
    "${SUDO[@]}" chown "$(id -u):$(id -g)" "$TARGETS_FILE"
  fi
fi

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

if [[ "$SKIP_FILES" -eq 0 ]]; then
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
fi

cat > "${OUTPUT_DIR}/setup_manifest.jsonl" <<EOF
{"record_type":"setup_manifest","testfiles_dir":"${TESTFILES_DIR}","output_dir":"${OUTPUT_DIR}","downloads_dir":"${DOWNLOADS_DIR}","targets_file":"${TARGETS_FILE}","saved_at_utc":"$(date -u +%Y-%m-%dT%H:%M:%SZ)"}
EOF

echo
echo "Setup complete."
echo "testfiles_dir=$TESTFILES_DIR"
echo "output_dir=$OUTPUT_DIR"
echo "downloads_dir=$DOWNLOADS_DIR"
echo "targets_file=$TARGETS_FILE"
echo "aws=$(command -v aws || true)"
echo "speedtest=$(command -v speedtest || true)"
