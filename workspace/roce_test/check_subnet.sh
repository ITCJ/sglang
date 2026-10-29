#!/usr/bin/env bash
# Send the local read-only GID check over SSH; no remote installation needed.
set -uo pipefail
if [[ $# -lt 3 || $# -gt 4 ]]; then
  echo 'Usage: bash check_subnet.sh PREFIX START END [SSH_USER]'
  echo 'Example: bash check_subnet.sh 192.0.2 10 19 user'
  exit 2
fi
prefix=${1%.}; first=$2; last=$3; user=${4:-}
bad() { echo "SCAN:FAIL $*"; exit 2; }
[[ $prefix =~ ^([0-9]{1,3}\.){2}[0-9]{1,3}$ ]] || bad PREFIX
IFS=. read -r a b c <<< "$prefix"
for octet in "$a" "$b" "$c" "$first" "$last"; do
  [[ $octet =~ ^[0-9]{1,3}$ ]] || bad OCTET
  (( 10#$octet <= 255 )) || bad OCTET
done
first=$((10#$first)); last=$((10#$last))
(( first <= last )) || bad RANGE
[[ -z $user || $user =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.-]*$ ]] || bad SSH_USER
prefix="$((10#$a)).$((10#$b)).$((10#$c))"
for tool in ssh timeout awk mktemp; do
  command -v "$tool" >/dev/null 2>&1 || bad "MISSING=$tool"
done
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd) || exit 2
check=$script_dir/../setup/check_gid.sh
[[ -r $check ]] || bad CHECK_SCRIPT_MISSING
logdir=$(mktemp -d "${TMPDIR:-/tmp}/roce-scan.XXXXXX") || exit 2
ok=0; no_gid=0; no_active=0; failed=0
echo "SCAN:START range=$prefix.$first-$last logs=$logdir"
for ((host=first; host<=last; host++)); do
  ip=$prefix.$host
  target=$ip
  [[ -z $user ]] || target=$user@$ip
  # Accept new hosts automatically, but reject changed host keys.
  timeout -k 2 15 ssh -T -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
    -o ConnectTimeout=5 -o ConnectionAttempts=1 \
    -o ServerAliveInterval=5 -o ServerAliveCountMax=1 \
    "$target" 'bash -s' <"$check" >"$logdir/$ip.out" 2>"$logdir/$ip.err"
  rc=$?
  if (( rc != 0 )); then
    ((failed+=1))
    reason=$(awk '
      /Host key verification failed|REMOTE HOST IDENTIFICATION HAS CHANGED/ {print "HOST_KEY"; found=1; exit}
      /Permission denied/ {print "AUTH"; found=1; exit}
      /Connection refused/ {print "REFUSED"; found=1; exit}
      /No route to host|Network is unreachable/ {print "UNREACHABLE"; found=1; exit}
      /timed out/ {print "TIMEOUT"; found=1; exit}
      END {if (!found) print "SSH_OR_CHECK"}' "$logdir/$ip.err")
    [[ $rc != 124 ]] || reason=TIMEOUT
    echo "$ip FAIL=$reason exit=$rc"
    continue
  fi
  counts=$(awk '/^HCA:/ {h++} /^GID:[0-9]+ / {g++} END {print h+0, g+0}' "$logdir/$ip.out")
  read -r h g <<< "$counts"
  if (( h == 0 )); then
    ((no_active+=1))
    echo "$ip NO_ACTIVE_HCA"
  elif (( g == 0 )); then
    ((no_gid+=1))
    echo "$ip NO_IPV4_GID active_ports=$h"
  else
    ((ok+=1))
    awk -v ip="$ip" -v h="$h" -v g="$g" '
      BEGIN {printf "%s GID_FOUND active_ports=%s gids=%s", ip,h,g}
      /^HCA:/ {dev=$1; sub(/^HCA:/,"",dev)}
      /^GID:[0-9]+ / {printf " | %s %s %s %s %s %s",dev,$1,$2,$3,$4,$5}
      END {print ""}' "$logdir/$ip.out"
  fi
done
echo "SUMMARY total=$((last-first+1)) gid_found=$ok no_ipv4_gid=$no_gid no_active_hca=$no_active failed=$failed"
echo "LOGS=$logdir"
(( failed == 0 && no_gid == 0 && no_active == 0 ))
