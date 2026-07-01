#!/usr/bin/env bash
set -uo pipefail

OUTPUT_DIR="/dataoutput"
RUNS="${RUNS:-3}"
SPEEDTEST_TIMEOUT="${SPEEDTEST_TIMEOUT:-240}"

RUNS_FILE="${OUTPUT_DIR}/network_speedtest_ookla_runs.jsonl"
SUMMARY_FILE="${OUTPUT_DIR}/network_speedtest_ookla_summary.jsonl"
COMBINED_FILE="${OUTPUT_DIR}/network_speedtest_ookla.jsonl"

mkdir -p "$OUTPUT_DIR"
rm -f "$RUNS_FILE" "$SUMMARY_FILE" "$COMBINED_FILE"

if ! command -v speedtest >/dev/null 2>&1; then
  echo "ERROR: speedtest CLI not found" >&2
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "ERROR: jq not found. Install with: apt install -y jq" >&2
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "ERROR: curl not found. Install with: apt install -y curl" >&2
  exit 1
fi

FIXED_SERVERS=(
  "barcelona_spain_jazztel|63419"
  "madrid_spain_melbicom|37061"
  "paris_france_scaleway|61933"
)

# Format: label|lat|lon
GEO_LOCATIONS=(
  "frankfurt_germany|50.1109|8.6821"
  "london_uk|51.5074|-0.1278"
  "new_york_usa|40.7128|-74.0060"
  "austin_texas_usa|30.2672|-97.7431"
  "los_angeles_california_usa|34.0522|-118.2437"
  "singapore|1.3521|103.8198"
  "hong_kong|22.3193|114.1694"
  "tokyo_japan|35.6762|139.6503"
)

echo "Starting Ookla network baseline"
echo "runs_per_server=$RUNS"
echo "speedtest_timeout_seconds=$SPEEDTEST_TIMEOUT"
echo "runs_file=$RUNS_FILE"
echo "summary_file=$SUMMARY_FILE"
echo "combined_file=$COMBINED_FILE"
echo

find_server_id_by_geo() {
  local lat="$1"
  local lon="$2"

  curl -fsSLG "https://www.speedtest.net/api/js/servers" \
    --data-urlencode "engine=js" \
    --data-urlencode "https_functional=true" \
    --data-urlencode "limit=10" \
    --data-urlencode "lat=$lat" \
    --data-urlencode "lon=$lon" \
    | jq -r '.[0].id // empty'
}

run_speedtest() {
  local label="$1"
  local server_id="$2"
  local run_index="$3"
  local tmp_json="/tmp/ookla_${label}_${server_id}_${run_index}.json"

  echo "Running label=$label server_id=$server_id run=$run_index"

  if ! timeout "$SPEEDTEST_TIMEOUT" speedtest --server-id "$server_id" --format=json --accept-license --accept-gdpr > "$tmp_json"; then
    echo "WARNING: speedtest failed or timed out for label=$label server_id=$server_id run=$run_index" >&2
    rm -f "$tmp_json"
    return 0
  fi

  jq -c \
    --arg label "$label" \
    --arg server_id "$server_id" \
    --argjson run_index "$run_index" '
    {
      schema_version: "perf_test_v1",
      test_suite: "vm_network_baseline",
      record_type: "network_speedtest_run",
      target_service: "ookla_speedtest",
      tool: "speedtest",
      tool_vendor: "Ookla",
      target_label: $label,
      run_index: $run_index,
      target_server_id: $server_id,
      target_server_name: .server.name,
      target_city: .server.location,
      target_country: .server.country,
      ping_ms: .ping.latency,
      jitter_ms: .ping.jitter,
      packet_loss_percent: (.packetLoss // null),
      download_mbps: ((.download.bandwidth * 8) / 1000000),
      upload_mbps: ((.upload.bandwidth * 8) / 1000000),
      download_unit: "Mbps",
      upload_unit: "Mbps",
      latency_unit: "ms",
      vm_public_ip: .interface.externalIp,
      vm_asn: .isp,
      result_url: (.result.url // null),
      saved_at_utc: (now | todateiso8601)
    }
  ' "$tmp_json" | tee -a "$RUNS_FILE" "$COMBINED_FILE" >/dev/null

  rm -f "$tmp_json"
}

echo "Fixed servers:"
ALL_SERVERS=()

for item in "${FIXED_SERVERS[@]}"; do
  label="${item%%|*}"
  server_id="${item##*|}"
  echo "  $label -> $server_id"
  ALL_SERVERS+=("${label}|${server_id}")
done

echo
echo "Searching geo servers:"

for item in "${GEO_LOCATIONS[@]}"; do
  label="$(echo "$item" | cut -d'|' -f1)"
  lat="$(echo "$item" | cut -d'|' -f2)"
  lon="$(echo "$item" | cut -d'|' -f3)"

  server_id="$(find_server_id_by_geo "$lat" "$lon" || true)"

  if [[ -z "$server_id" ]]; then
    echo "  WARNING: no server found for $label lat=$lat lon=$lon; skipping"
    continue
  fi

  echo "  $label lat=$lat lon=$lon -> server_id=$server_id"
  ALL_SERVERS+=("${label}|${server_id}")
done

echo
echo "Running speedtests..."

for item in "${ALL_SERVERS[@]}"; do
  label="${item%%|*}"
  server_id="${item##*|}"

  for run in $(seq 1 "$RUNS"); do
    run_speedtest "$label" "$server_id" "$run"
    sleep "${SLEEP_BETWEEN_TESTS:-90}"
  done
done

if [[ ! -s "$RUNS_FILE" ]]; then
  echo "ERROR: no successful speedtest runs were written to $RUNS_FILE" >&2
  exit 1
fi

jq -s -c '
  def median:
    sort as $s |
    if ($s | length) == 0 then null
    elif ($s | length) % 2 == 1 then $s[(($s | length) / 2 | floor)]
    else
      (($s[(($s | length) / 2) - 1] + $s[(($s | length) / 2)]) / 2)
    end;

  group_by(.target_label)[] |
  {
    schema_version: "perf_test_v1",
    test_suite: "vm_network_baseline",
    record_type: "network_speedtest_summary",
    target_service: "ookla_speedtest",
    tool: "speedtest",
    tool_vendor: "Ookla",
    target_label: .[0].target_label,
    target_server_id: .[0].target_server_id,
    target_server_name: .[0].target_server_name,
    target_city: .[0].target_city,
    target_country: .[0].target_country,
    run_count: length,
    sample_run_indices: map(.run_index),
    avg_download_mbps: (((map(.download_mbps) | add) / length) | . * 100 | round / 100),
    avg_upload_mbps: (((map(.upload_mbps) | add) / length) | . * 100 | round / 100),
    avg_ping_ms: (((map(.ping_ms) | add) / length) | . * 100 | round / 100),
    median_download_mbps: ((map(.download_mbps) | median) | . * 100 | round / 100),
    median_upload_mbps: ((map(.upload_mbps) | median) | . * 100 | round / 100),
    median_ping_ms: ((map(.ping_ms) | median) | . * 100 | round / 100),
    min_download_mbps: ((map(.download_mbps) | min) | . * 100 | round / 100),
    max_download_mbps: ((map(.download_mbps) | max) | . * 100 | round / 100),
    min_upload_mbps: ((map(.upload_mbps) | min) | . * 100 | round / 100),
    max_upload_mbps: ((map(.upload_mbps) | max) | . * 100 | round / 100),
    max_packet_loss_percent: (map(.packet_loss_percent // 0) | max),
    download_unit: "Mbps",
    upload_unit: "Mbps",
    latency_unit: "ms",
    saved_at_utc: (now | todateiso8601)
  }
' "$RUNS_FILE" | tee "$SUMMARY_FILE" >> "$COMBINED_FILE"

echo
echo "Completed Ookla network baseline"
echo "runs_file=$RUNS_FILE"
echo "summary_file=$SUMMARY_FILE"
echo "combined_file=$COMBINED_FILE"
echo
echo "Summary:"
cat "$SUMMARY_FILE" | jq .
