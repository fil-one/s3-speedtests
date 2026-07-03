#!/usr/bin/env bash
set -euo pipefail

OUTPUT_DIR="${OUTPUT_DIR:-/dataoutput}"
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
  "AWS Oregon::us-west-2 | Oregon, USA::s3.us-west-2.amazonaws.com"
)

SELECTED_PROVIDERS=""

usage() {
  cat <<'USAGE'
Usage: scripts/s3_provider_traceroutes.sh [--providers aws-us-west-2]

Runs TCP traceroutes to S3 provider endpoints. Use --providers with a comma-separated
list to run only matching provider names, regions, endpoints, or aliases.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --providers)
      SELECTED_PROVIDERS="${2:-}"
      if [[ -z "$SELECTED_PROVIDERS" ]]; then
        echo "ERROR: --providers requires a comma-separated value" >&2
        exit 2
      fi
      shift 2
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

provider_matches() {
  local provider_lc="${1,,}"
  local region_lc="${2,,}"
  local endpoint_lc="${3,,}"
  local selected_lc="${SELECTED_PROVIDERS,,}"
  local wanted

  [[ -z "$selected_lc" ]] && return 0

  IFS=',' read -ra wanted_items <<< "$selected_lc"
  for wanted in "${wanted_items[@]}"; do
    wanted="${wanted// /}"
    case "$wanted" in
      aws-us-west-2|us-west-2|oregon|aws-oregon)
        [[ "$region_lc" == us-west-2* || "$provider_lc" == *oregon* || "$endpoint_lc" == *us-west-2* ]] && return 0
        ;;
      *)
        [[ "$provider_lc" == *"$wanted"* || "$region_lc" == *"$wanted"* || "$endpoint_lc" == *"$wanted"* ]] && return 0
        ;;
    esac
  done

  return 1
}

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
if [[ -n "$SELECTED_PROVIDERS" ]]; then
  echo "selected_providers=$SELECTED_PROVIDERS" | tee -a "$TXT_OUT"
fi
echo | tee -a "$TXT_OUT"

matched=0

for item in "${PROVIDERS[@]}"; do
  provider="${item%%::*}"
  rest="${item#*::}"
  region_location="${rest%%::*}"
  endpoint="${rest##*::}"

  if ! provider_matches "$provider" "$region_location" "$endpoint"; then
    continue
  fi
  matched=$((matched + 1))

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
  trace_command="traceroute -T -p 443 -n -w 3 -q 3 -m 30 $endpoint"

  echo "$ $trace_command" | tee -a "$TXT_OUT"

  if traceroute -T -p 443 -n -w 3 -q 3 -m 30 "$endpoint" > "$trace_tmp" 2>&1; then
    status="success"
  else
    status="failed"
  fi

  cat "$trace_tmp" | tee -a "$TXT_OUT"

  hop_count="$(grep -E '^[[:space:]]*[0-9]+' "$trace_tmp" | wc -l | awk '{print $1}')"
  last_hop="$(grep -E '^[[:space:]]*[0-9]+' "$trace_tmp" | tail -1 || true)"
  trace_output="$(cat "$trace_tmp")"
  total_ms="$(awk '
    /^[[:space:]]*[0-9]+/ { line = $0 }
    END {
      if (line == "") {
        exit
      }
      n = split(line, parts, /[[:space:]]+/)
      sum = 0
      count = 0
      for (i = 1; i <= n; i++) {
        if (parts[i] == "ms" && i > 1 && parts[i - 1] ~ /^[0-9.]+$/) {
          sum += parts[i - 1]
          count += 1
        }
      }
      if (count > 0) {
        printf "%.3f", sum / count
      }
    }
  ' "$trace_tmp")"

  jq -nc \
    --arg run_id "$RUN_ID" \
    --arg timestamp_utc "$trace_start" \
    --arg provider "$provider" \
    --arg region_location "$region_location" \
    --arg endpoint "$endpoint" \
    --arg trace_command "$trace_command" \
    --arg resolved_ips "$resolved_ips" \
    --arg status "$status" \
    --argjson hop_count "$hop_count" \
    --arg last_hop "$last_hop" \
    --arg trace_output "$trace_output" \
    --arg total_ms "$total_ms" \
    '{
      schema_version: "traceroute_v1",
      test_type: "tcp_traceroute_443",
      source_node: "Cubepath VM Barcelona",
      run_id: $run_id,
      timestamp_utc: $timestamp_utc,
      provider: $provider,
      region_location: $region_location,
      endpoint: $endpoint,
      trace_command: $trace_command,
      resolved_ipv4_addresses: ($resolved_ips | split(",") | map(select(length > 0))),
      status: $status,
      hop_count: $hop_count,
      last_hop: $last_hop,
      total_ms: (if $total_ms == "" then null else ($total_ms | tonumber) end),
      final_hop_avg_ms: (if $total_ms == "" then null else ($total_ms | tonumber) end),
      trace_output: $trace_output
    }' >> "$JSONL_OUT"

  rm -f "$trace_tmp"
  echo | tee -a "$TXT_OUT"
done

if [[ "$matched" -eq 0 ]]; then
  echo "ERROR: no providers matched --providers '${SELECTED_PROVIDERS}'" | tee -a "$TXT_OUT" >&2
  exit 2
fi

cp "$TXT_OUT" "$LATEST_TXT"
cp "$JSONL_OUT" "$LATEST_JSONL"

echo "============================================================" | tee -a "$TXT_OUT"
echo "Done." | tee -a "$TXT_OUT"
echo "Text output: $TXT_OUT" | tee -a "$TXT_OUT"
echo "JSONL output: $JSONL_OUT" | tee -a "$TXT_OUT"
echo "Latest text alias: $LATEST_TXT" | tee -a "$TXT_OUT"
echo "Latest JSONL alias: $LATEST_JSONL" | tee -a "$TXT_OUT"
