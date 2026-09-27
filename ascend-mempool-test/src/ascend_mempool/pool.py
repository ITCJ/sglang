"""Compatibility imports for the ticket 01 standalone gate."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sglang.srt.hardware_backend.npu.mempool.manager import (
        BMHandle,
        MempoolKVManager,
        MempoolKVView,
    )
else:
    from .manager import BMHandle, MempoolKVManager, MempoolKVView

__all__ = ["BMHandle", "MempoolKVManager", "MempoolKVView"]
