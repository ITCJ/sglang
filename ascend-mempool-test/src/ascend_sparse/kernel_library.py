"""Report the installed package and operator interfaces, without building kernels."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import inspect
import json
from pathlib import Path
from typing import Any


def kernel_identity(*, require_lru: bool = True) -> dict[str, Any]:
    import torch

    package = importlib.import_module("sgl_kernel_npu")
    ops = importlib.import_module("sgl_kernel_npu.sparsity_driven_kv_offload")
    names = ["unidex_copy_multibase"]
    if require_lru:
        names += [
            "fused_timestamp_lru_metadata_update_with_probation",
            "parallel_lru_metadata_write",
            "slot_map_lookup",
        ]
        if "pos_mask_size" not in inspect.signature(ops.slot_map_lookup).parameters:
            raise RuntimeError("Installed slot_map_lookup lacks pos_mask_size")
    schemas = {}
    for name in names:
        if not callable(getattr(ops, name)):
            raise RuntimeError(f"Installed kernel export is not callable: {name}")
        schemas[name] = str(getattr(torch.ops.npu, name).default._schema)
    distribution = importlib.metadata.distribution("sgl-kernel-npu")
    assert package.__file__ is not None
    root = Path(package.__file__).resolve().parent
    binaries = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*.so"))
    }
    return dict(
        version=distribution.version,
        package_path=str(root),
        binaries_sha256=binaries,
        schemas=schemas,
        direct_url=json.loads(distribution.read_text("direct_url.json") or "null"),
    )
