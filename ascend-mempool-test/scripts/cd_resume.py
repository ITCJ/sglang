"""Narrow checkpoint compatibility for reviewed launch and teardown fixes.

Only the original C/D release and launch fix are admitted across versions. Its exact source
identity and this release's runtime digest must match. Tests are excluded from
that runtime digest; this module is the compatibility policy itself. Any other
runtime edit disables the exception until separately reviewed. Saved checkpoint
contracts retain the actual measuring version, never relabelled as new samples.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from typing import Any

from run_numa_sharing import REPO, code_identity

BASELINE = dict(
    git_sha="61e22c86039fd9155ca25fdf3b105249b4e16a96",
    source_sha256="dc8ccf18169d3ef8d7d592583a901388bbda31d11a24628b4bbb584e0bb92314",
)
LAUNCH_FIX = dict(
    git_sha="0d000b262a0d62819a49367bb236d040606b804d",
    source_sha256="c762336bf3c9fec4a7b720cfaae3785fac214dd7a88fba7293a090c07c07121d",
)
# Updated only after auditing the complete runtime diff against both sources.
REVIEWED_RUNTIME_SHA256 = (
    "fc22a2dcd8a1c15cf1fe1194cc079aefa1643f046bf48cbcd3d6e918d1a53beb"
)


def runtime_digest() -> str:
    digest = hashlib.sha256()
    for root in (
        REPO / "ascend-mempool-test",
        REPO / "python/sglang/srt/hardware_backend/npu",
    ):
        for path in sorted(root.rglob("*.py")):
            relative = path.relative_to(REPO).as_posix()
            if relative.startswith("ascend-mempool-test/tests/") or relative == (
                "ascend-mempool-test/scripts/cd_resume.py"
            ):
                continue
            digest.update(relative.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


@lru_cache(maxsize=4)
def _audited_current(git_sha: str, source_sha256: str) -> bool:
    return (
        code_identity() == dict(git_sha=git_sha, source_sha256=source_sha256)
        and runtime_digest() == REVIEWED_RUNTIME_SHA256
    )


def compatible_code(previous: Any, current: Any) -> bool:
    if previous == current:
        return True
    if previous not in (BASELINE, LAUNCH_FIX) or not isinstance(current, dict):
        return False
    if set(current) != {"git_sha", "source_sha256"} or not all(
        isinstance(value, str) for value in current.values()
    ):
        return False
    return _audited_current(current["git_sha"], current["source_sha256"])
