#!/usr/bin/env bash
# Read-only: show active RDMA ports and IPv4-mapped GIDs, without downloads.
set -euo pipefail
shopt -s nullglob
found=0
for port in /sys/class/infiniband/*/ports/*; do
  [[ -r "$port/state" ]] || continue
  state=$(<"$port/state")
  [[ "$state" == *ACTIVE* ]] || continue
  device=${port%/ports/*}
  device=${device##*/}
  number=${port##*/}
  echo "HCA:$device/$number ACTIVE"
  for entry in "$port"/gids/*; do
    gid=$(<"$entry")
    # IPv4-mapped GIDs are the candidates for the IPv4 RoCE test.
    [[ "$gid" == 0000:0000:0000:0000:0000:ffff:* ]] || continue
    index=${entry##*/}
    kind=unknown
    netdev=unknown
    [[ ! -r "$port/gid_attrs/types/$index" ]] || kind=$(<"$port/gid_attrs/types/$index")
    [[ ! -r "$port/gid_attrs/ndevs/$index" ]] || netdev=$(<"$port/gid_attrs/ndevs/$index")
    high=${gid%:*}
    high=${high##*:}
    low=${gid##*:}
    printf 'GID:%s IP:%d.%d.%d.%d %s %s\n' "$index" \
      "$((16#$high >> 8))" "$((16#$high & 255))" \
      "$((16#$low >> 8))" "$((16#$low & 255))" "$kind" "$netdev"
    found=1
  done
done
if [[ "$found" == 0 ]]; then
  echo 'GID:NONE (no IPv4 GID on active RDMA ports)'
fi
