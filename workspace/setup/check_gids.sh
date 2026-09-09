#!/usr/bin/env bash
# Read-only RoCE GID discovery; no tools to install or network access needed.
# Optional argument: HCA device name. By default scan every visible HCA.
set -euo pipefail
shopt -s nullglob

if (( $# > 1 )); then
  echo "Usage: bash workspace/setup/check_gids.sh [HCA]" >&2
  exit 2
fi

devices=(/sys/class/infiniband/*)
if (( $# == 1 )); then
  if [[ "$1" == */* || "$1" == . || "$1" == .. ]]; then
    echo "GID:INVALID_DEVICE" >&2
    exit 2
  fi
  devices=("/sys/class/infiniband/$1")
fi

found=0
for device in "${devices[@]}"; do
  for port in "$device"/ports/*; do
    state=$(<"$port/state")
    [[ "$state" == *ACTIVE* ]] || continue
    echo "HCA:${device##*/} PORT:${port##*/} ACTIVE"
    for file in "$port"/gids/*; do
      gid=$(<"$file")
      [[ "$gid" != 0000:0000:0000:0000:0000:0000:0000:0000 ]] || continue
      index=${file##*/}
      type="?"
      netdev="?"
      [[ ! -r "$port/gid_attrs/types/$index" ]] || type=$(<"$port/gid_attrs/types/$index")
      [[ ! -r "$port/gid_attrs/ndevs/$index" ]] || netdev=$(<"$port/gid_attrs/ndevs/$index")
      address=$gid
      # Render IPv4-mapped GIDs as IPv4, avoiding manual hex conversion.
      if [[ "$gid" =~ ^0000:0000:0000:0000:0000:ffff:([0-9a-fA-F]{4}):([0-9a-fA-F]{4})$ ]]; then
        high=${BASH_REMATCH[1]}
        low=${BASH_REMATCH[2]}
        printf -v address '%d.%d.%d.%d' "$((16#$high >> 8))" "$((16#$high & 255))" "$((16#$low >> 8))" "$((16#$low & 255))"
      fi
      echo "GID:$index $type $address $netdev"
      found=1
    done
  done
done
if (( ! found )); then
  echo "GID:NONE_ACTIVE"
  exit 1
fi
