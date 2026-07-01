#!/usr/bin/env bash
set -euo pipefail

OUTPUT_DIR="/dataoutput"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"

TXT_OUT="${OUTPUT_DIR}/s3_provider_traceroutes_${RUN_ID}.txt"
JSONL_OUT="${OUTPUT_DIR}/s3_provider_traceroutes_${RUN_ID}.jsonl"
LATEST_TXT="${OUTPUT_DIR}/s3_provider_traceroutes.txt"
LATEST_JSONL="${OUTPUT_DIR}/s3_provider_traceroutes.jsonl"

mkdir -p "$OUTPUT_DIR"

PROVIDERS=(
  "Fil.one::eu-west-1 | Albi, France::eu-west-1.s3.fil.one"
  "AWS::eu-south-2 | Aragon, Spain::s3.eu-south-2.amazonaws.com"
  "Wasabi::eu-west-2 | Paris, France::s3.eu-west-2.wasabisys.com"
  "Backblaze::eu-central-003 | Amsterdam, Netherlands::s3.eu-central-003.backblazeb2.com"
)

if ! command -v traceroute >/dev/null 2>&1; then
  echo "ERROR: traceroute is not installed. Install with: apt update && apt install -y traceroute"
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "ERROR: jq is not installed. Install with: apt update && apt install -y jq"
  exit 1
fi

echo "S3 Provider Traceroute Test" | tee "$TXT_OUT"
echo "run_id=$RUN_ID" | tee -a "$TXT_OUT"
echo "source_node=Cubepath VM Barcelona" | tee -a "$TXT_OUT"
echo "timestamp_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)" | tee -a "$TXT_OUT"
echo | tee -a "$TXT_OUT"

for item in "${PROVIDERS[@]}"; do
  provider="${item%%::*}"
  rest="${item#*::}"
  region_location="${rest%%::*}"
  endpoint="${rest##*::}"

  echo "============================================================" | tee -a "$TXT_OUT"
  echo "Provider: $provider" | tee -a "$TXT_OUT"
  echo "Region/Location: $region_location" | tee -a "$TXT_OUT"
  echo "Endpoint: $endpoint" | tee -a "$TXT_OUT"
  echo "Timestamp UTC: $(date -u +%Y-%m-%dT%H:%M:%SZ)" | tee -a "$TXT_OUT"
  echo | tee -a "$TXT_OUT"

  resolved_ips="$(getent ahostsv4 "$endpoint" | awk '{print $1}' | sort -u | paste -sd ',' - || true)"

  echo "Resolved IPv4 addresses: ${resolved_ips:-none}" | tee -a "$TXT_OUT"
  echo | tee -a "$TXT_OUT"

  echo "TCP traceroute to HTTPS port 443:" | tee -a "$TXT_OUT"

  trace_start="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  safe_provider="$(echo "$provider" | tr ' /.' '___')"
  trace_tmp="/tmp/traceroute_${safe_provider}_${RUN_ID}.txt"

  if traceroute -T -p 443 -n -w 3 -q 3 -m 30 "$endpoint" > "$trace_tmp" 2>&1; then
    status="success"
  else
    status="failed"
  fi

  cat "$trace_tmp" | tee -a "$TXT_OUT"

  hop_count="$(grep -E '^[[:space:]]*[0-9]+' "$trace_tmp" | wc -l | awk '{print $1}')"
  last_hop="$(grep -E '^[[:space:]]*[0-9]+' "$trace_tmp" | tail -1 || true)"

  jq -nc \
    --arg run_id "$RUN_ID" \
    --arg timestamp_utc "$trace_start" \
    --arg provider "$provider" \
    --arg region_location "$region_location" \
    --arg endpoint "$endpoint" \
    --arg resolved_ips "$resolved_ips" \
    --arg status "$status" \
    --argjson hop_count "$hop_count" \
    --arg last_hop "$last_hop" \
    '{
      schema_version: "traceroute_v1",
      test_type: "tcp_traceroute_443",
      source_node: "Cubepath VM Barcelona",
      run_id: $run_id,
      timestamp_utc: $timestamp_utc,
      provider: $provider,
      region_location: $region_location,
      endpoint: $endpoint,
      resolved_ipv4_addresses: ($resolved_ips | split(",") | map(select(length > 0))),
      status: $status,
      hop_count: $hop_count,
      last_hop: $last_hop
    }' >> "$JSONL_OUT"

  rm -f "$trace_tmp"
  echo | tee -a "$TXT_OUT"
done

cp "$TXT_OUT" "$LATEST_TXT"
cp "$JSONL_OUT" "$LATEST_JSONL"

echo "============================================================" | tee -a "$TXT_OUT"
echo "Done." | tee -a "$TXT_OUT"
echo "Text output: $TXT_OUT" | tee -a "$TXT_OUT"
echo "JSONL output: $JSONL_OUT" | tee -a "$TXT_OUT"
echo "Latest text alias: $LATEST_TXT" | tee -a "$TXT_OUT"
echo "Latest JSONL alias: $LATEST_JSONL" | tee -a "$TXT_OUT"
