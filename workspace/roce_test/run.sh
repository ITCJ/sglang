#!/usr/bin/env bash
# Host-only perftest wrapper. No NPU access or content validation.
set -uo pipefail
usage() {
  echo 'Usage: bash run.sh server|client HCA IB_PORT GID_INDEX smoke|bw [SERVER_ROCE_IP]'
  echo 'Optional env: ROCE_PORT=18515 ROCE_TIMEOUT=90 ROCE_LOG_DIR=/tmp/...'
}
fail() { echo "ROCE:FAIL $*"; exit 1; }
[[ $# -ge 5 && $# -le 6 ]] || { usage; exit 2; }
role=$1; dev=$2; ibport=$3; gid=$4; mode=$5; peer=${6:-}
[[ $role == server || $role == client ]] || fail ROLE
[[ $mode == smoke || $mode == bw ]] || fail MODE
[[ $ibport =~ ^[1-9][0-9]*$ && $gid =~ ^[0-9]+$ ]] || fail PORT_OR_GID
[[ $role != client || -n $peer && $peer != -* ]] || fail PEER_REQUIRED
[[ $role != server || -z $peer ]] || fail SERVER_TAKES_NO_PEER
port=${ROCE_PORT:-18515}
limit=${ROCE_TIMEOUT:-90}
[[ $port =~ ^[0-9]+$ && $limit =~ ^[1-9][0-9]*$ ]] || fail OPTIONS
for tool in ib_write_bw timeout awk tail; do
  command -v "$tool" >/dev/null 2>&1 || fail "MISSING=$tool"
done
base=/sys/class/infiniband/$dev/ports/$ibport
[[ -r $base/state ]] || fail NO_DEVICE_PORT
[[ $(<"$base/state") == *ACTIVE* ]] || fail PORT_NOT_ACTIVE
[[ -r $base/gids/$gid ]] || fail NO_GID
[[ -r $base/gid_attrs/types/$gid ]] || fail NO_GID_TYPE
[[ $(<"$base/gid_attrs/types/$gid") == *'RoCE v2'* ]] || fail NOT_ROCE_V2
logdir=${ROCE_LOG_DIR:-/tmp/roce-$(date +%Y%m%d-%H%M%S)-$$}
mkdir -p "$logdir" || fail LOG_DIR
log=$logdir/perftest.log
args=(-d "$dev" -i "$ibport" -x "$gid" -p "$port" -s 4096 -q 1 -t 8)
if [[ $mode == smoke ]]; then
  args+=(-n 100)
else
  args+=(-D 3)
fi
[[ $role != client ]] || args+=("$peer")
{
  date -Is
  ib_write_bw --version
  printf 'COMMAND: '; printf '%q ' ib_write_bw "${args[@]}"; printf '\n'
  printf 'GID: %s\n' "$(<"$base/gids/$gid")"
} >"$logdir/meta.log" 2>&1
# Timeout also bounds a server waiting for a client. Disable crash dumps.
ulimit -c 0
echo "ROCE:START role=$role mode=$mode log=$logdir"
timeout -k 3 "$limit" ib_write_bw "${args[@]}" >"$log" 2>&1
rc=$?
if [[ $rc -ne 0 ]]; then
  echo "ROCE:FAIL exit=$rc (124=timeout) log=$logdir"
  # One short hint only; full diagnostics remain on the remote host.
  awk 'tolower($0) ~ /error|failed|failure|unable|segmentation|could not/ {print "HINT: " substr($0,1,180); exit}' "$log"
  exit "$rc"
fi
row=$(awk '$1 == 4096 && NF >= 4 {line=$0} END {print line}' "$log")
[[ -n $row ]] || fail "NO_RESULT log=$logdir"
echo "ROCE:PASS role=$role mode=$mode validation=off"
if [[ $mode == bw ]]; then
  awk '/#bytes/ {print; exit}' "$log"
  echo "$row"
fi
