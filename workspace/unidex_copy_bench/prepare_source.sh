#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
    printf 'Usage: bash prepare_source.sh <NEW_OUTPUT_DIR>\n' >&2
    exit 2
fi
SOURCE_COMMIT=d9261669b0303a28369d07c0eea0bd1627235dd6
PACKAGE_DIR=$1
if [[ -e "$PACKAGE_DIR" ]]; then
    printf 'Output already exists; choose a new directory.\n' >&2
    exit 2
fi
mkdir -p "$PACKAGE_DIR"
PACKAGE_DIR=$(cd "$PACKAGE_DIR" && pwd)
exec > >(tee "$PACKAGE_DIR/prepare.log") 2>&1
stage=clone
trap 'rc=$?; printf "PREPARE_EXIT stage=%s code=%s\n" "$stage" "$rc"' EXIT
git clone --no-checkout https://github.com/sgl-project/sgl-kernel-npu.git "$PACKAGE_DIR/checkout"
git -C "$PACKAGE_DIR/checkout" checkout --detach "$SOURCE_COMMIT"
stage=submodules
git -C "$PACKAGE_DIR/checkout" submodule update --init --recursive
[[ $(git -C "$PACKAGE_DIR/checkout" rev-parse HEAD) == "$SOURCE_COMMIT" ]]
git -C "$PACKAGE_DIR/checkout" submodule status --recursive > "$PACKAGE_DIR/submodules.txt"
if grep -q '^[+U-]' "$PACKAGE_DIR/submodules.txt"; then
    printf 'Submodule status does not match fixed gitlinks.\n' >&2
    exit 1
fi
stage=manifest
mkdir "$PACKAGE_DIR/checkout/unidex-source-manifest"
printf '%s\n' "$SOURCE_COMMIT" > "$PACKAGE_DIR/checkout/unidex-source-manifest/source_commit.txt"
cp "$PACKAGE_DIR/submodules.txt" "$PACKAGE_DIR/checkout/unidex-source-manifest/submodules.txt"
(
    cd "$PACKAGE_DIR/checkout"
    find . -name .git -prune -o -type f ! -path './unidex-source-manifest/*' -print0 \
        | sort -z | xargs -0 sha256sum > unidex-source-manifest/source-files.sha256
)
stage=archive
tar --exclude=.git -C "$PACKAGE_DIR/checkout" -czf "$PACKAGE_DIR/source.tar.gz" .
(
    cd "$PACKAGE_DIR"
    sha256sum source.tar.gz > SHA256SUMS
)
printf 'SOURCE_READY commit=%s archive=%s/source.tar.gz\n' "$SOURCE_COMMIT" "$PACKAGE_DIR"
