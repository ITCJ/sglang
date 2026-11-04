"""Load the Ascend PD control modules without importing the SGLang server."""

from pathlib import Path

__path__.append(
    str(Path(__file__).resolve().parents[3] / "python/sglang/srt/disaggregation/ascend")
)

# The storage allocator is also used by the control-plane CPU harness.
import sys

from ascend_mempool import alive_pages

sys.modules.setdefault(
    "sglang.srt.hardware_backend.npu.mempool.alive_pages", alive_pages
)
