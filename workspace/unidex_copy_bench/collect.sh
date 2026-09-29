#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 3 ]]; then
    printf 'Usage: bash collect.sh <NEW_ARCHIVE.tar.gz> <INSTALL_LOG_DIR> <RUN_OR_CHECK_DIR> [...]\n' >&2
    exit 2
fi
ARCHIVE=$1
shift
[[ ! -e "$ARCHIVE" ]] || { printf 'Archive exists; choose a new name.\n' >&2; exit 2; }
ARCHIVE_PARENT=$(cd "$(dirname "$ARCHIVE")" && pwd)
ARCHIVE="$ARCHIVE_PARENT/$(basename "$ARCHIVE")"
ENTRIES=()
for directory in "$@"; do
    [[ -d "$directory" ]] || { printf 'Missing log/result directory: %s\n' "$directory" >&2; exit 1; }
    absolute=$(cd "$directory" && pwd)
    case "$ARCHIVE" in "$absolute"/*) printf 'Archive must be outside input directories.\n' >&2; exit 2;; esac
    ENTRIES+=("${absolute#/}")
done
# Preserve directory identity and every raw JSON/CSV/log, including failed runs.
tar -C / -czf "$ARCHIVE" -- "${ENTRIES[@]}"
sha256sum "$ARCHIVE"
printf 'COLLECT_OK archive=%s; input files preserved\n' "$ARCHIVE"
