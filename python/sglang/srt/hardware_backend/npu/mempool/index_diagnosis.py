"""Opt-in index_add_ boundaries during eager warmup; never used in capture."""

from __future__ import annotations

import json
import os
import traceback
from contextlib import contextmanager, nullcontext


def warmup_index_diagnosis(warmup: int):
    if os.environ.get("SGLANG_NPU_MEMPOOL_INDEX_DIAG") != "1":
        return nullcontext()
    return _diagnose(warmup)


@contextmanager
def _diagnose(warmup: int):
    import torch
    from torch.utils._python_dispatch import TorchDispatchMode

    context = {
        "pid": os.getpid(),
        "device": torch.npu.current_device(),
        "warmup": warmup,
    }

    def emit(phase, **fields):
        print(
            "[MEMPOOL_INDEX_DIAG] " + json.dumps(dict(context, phase=phase, **fields)),
            flush=True,
        )

    class IndexAdds(TorchDispatchMode):
        calls = 0

        def __torch_dispatch__(self, func, types, args=(), kwargs=None):
            kwargs = kwargs or {}
            if func != torch.ops.aten.index_add_.default:
                return func(*args, **kwargs)
            self.calls += 1
            target, dim, index, source = args[:4]
            sites = [
                f"{frame.filename}:{frame.lineno}:{frame.name}"
                for frame in traceback.extract_stack(limit=24)[:-1]
                if "/hardware_backend/npu/" in frame.filename
                and not frame.filename.endswith("/index_diagnosis.py")
            ]
            fields = {
                "call": self.calls,
                "sites": sites,
                "stream": torch.npu.current_stream().npu_stream,
                "target_shape": list(target.shape),
                "source_shape": list(source.shape),
                "dtype": str(target.dtype),
                "dim": dim,
            }
            stage = "before_sync"
            emit(stage, **fields)
            try:
                # A failure here belongs to previously submitted device work.
                torch.npu.synchronize()
                stage = "read_indices"
                rows = index.detach().cpu().tolist()
                emit("inputs", indices=rows, **fields)
                stage = "index_add"
                result = func(*args, **kwargs)
                stage = "after_sync"
                torch.npu.synchronize()
                emit("after", **fields)
                return result
            except Exception as error:
                emit("failed", stage=stage, error=repr(error), **fields)
                raise

    mode = IndexAdds()
    emit("warmup_begin")
    try:
        with mode:
            yield
        torch.npu.synchronize()
    except Exception as error:
        emit("warmup_failed", calls=mode.calls, error=repr(error))
        raise
    else:
        emit("warmup_complete", calls=mode.calls)
