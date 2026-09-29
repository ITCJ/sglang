"""Load the Ascend PD control modules without importing the SGLang server."""

from pathlib import Path

__path__.append(
    str(Path(__file__).resolve().parents[3] / "python/sglang/srt/disaggregation/ascend")
)
