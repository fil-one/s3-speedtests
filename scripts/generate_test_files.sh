#!/usr/bin/env bash
# Generate the random_*.bin payload set the speed tests expect, on macOS or
# Linux, without root. setup_vm.sh calls this with the "full" set as part of
# the Ubuntu VM setup; call it directly when the test scripts run from a
# developer machine.
#
#   scripts/generate_test_files.sh <dir> <set>
#
# Sets match the --file-set names of the upload/download scripts:
#   quick     one 1 MiB + one 100 MiB file                       (101 MiB)
#   standard  100 x 1 MiB, 5 x 100 MiB, 1 x 1 GiB                (~1.6 GiB)
#   large     1 x 25 GiB, 1 x 50 GiB                             (75 GiB)
#   full      standard + large
#
# Existing files of the right size are kept; FORCE_FILES=1 regenerates them.
set -euo pipefail

usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; }

if [ $# -ne 2 ]; then usage >&2; exit 2; fi
dir="$1"
set_name="$2"
FORCE_FILES="${FORCE_FILES:-0}"

case "$(uname -s)" in
  Darwin) file_size() { stat -f '%z' "$1"; }; dd_opts=(bs=1m status=progress) ;;
  # GNU dd only: fullblock avoids short reads from /dev/urandom truncating the file.
  *)      file_size() { stat -c '%s' "$1"; }; dd_opts=(bs=1M iflag=fullblock status=progress) ;;
esac

generate_file() {
  local path="$1" mib="$2"
  local expected=$((mib * 1024 * 1024)) current=0
  [ -f "$path" ] && current="$(file_size "$path")"
  if [ "$FORCE_FILES" -eq 0 ] && [ "$current" -eq "$expected" ]; then
    echo "exists size_ok $path"
    return
  fi
  echo "generating $path ($mib MiB)"
  local tmp="$path.partial"
  rm -f "$tmp"
  dd if=/dev/urandom of="$tmp" count="$mib" "${dd_opts[@]}"
  [ "$(file_size "$tmp")" -eq "$expected" ] || { echo "ERROR: $tmp has the wrong size" >&2; rm -f "$tmp"; exit 1; }
  mv "$tmp" "$path"
}

quick() {
  generate_file "$dir/random_001_1mib.bin" 1
  generate_file "$dir/random_1_100mib.bin" 100
}

standard() {
  local i
  for i in $(seq -w 1 100); do generate_file "$dir/random_${i}_1mib.bin" 1; done
  for i in $(seq 1 5); do generate_file "$dir/random_${i}_100mib.bin" 100; done
  generate_file "$dir/random_001_1gib.bin" 1024
}

large() {
  generate_file "$dir/random_001_25gib.bin" 25600
  generate_file "$dir/random_001_50gib.bin" 51200
}

mkdir -p "$dir"
case "$set_name" in
  quick)    quick ;;
  standard) standard ;;
  large)    large ;;
  full)     standard; large ;;
  *) echo "ERROR: unknown set '$set_name' (quick | standard | large | full)" >&2; exit 2 ;;
esac
