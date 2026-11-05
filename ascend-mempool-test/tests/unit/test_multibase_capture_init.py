"""Exercise cold and shape-changing descriptors without host tensor uploads."""

import tarfile
import unittest
from pathlib import Path
from unittest.mock import patch

import torch
from test_multibase import cpu_multibase

from ascend_mempool.multibase import IndexedCopy


class TestCaptureInitialization(unittest.TestCase):
    def test_first_call_and_shape_change_require_no_host_tensor_upload(self):
        root = Path(__file__).resolve().parents[3]
        with tarfile.open(
            root / ".scratch/k2/verification/ticket26-local/phase1-source.tar.gz"
        ) as bundle:
            code = bundle.extractfile(
                "python/sglang/srt/hardware_backend/npu/mempool/multibase.py"
            ).read()
        namespace = {"__name__": "phase1_multibase"}
        exec(compile(code, "phase1_multibase", "exec"), namespace)
        for phase, adapter in (
            ("phase1", namespace["IndexedCopy"]),
            ("phase2", IndexedCopy),
        ):
            with self.subTest(phase=phase):
                copier = adapter(kernel=cpu_multibase)
                for rows in (2, 3):
                    source = torch.arange(rows, dtype=torch.int64) + 19
                    target = torch.full_like(source, -1)
                    indices = torch.arange(rows)
                    mask = torch.ones(rows, dtype=torch.bool)
                    # torch.tensor(Python data, device=...) performs the H2D
                    # upload rejected by the real NPU capture in run 002.
                    with patch.object(
                        torch,
                        "tensor",
                        side_effect=AssertionError("host tensor upload during capture"),
                    ):
                        copier(
                            source,
                            target,
                            indices,
                            indices,
                            mask,
                            rows,
                            rows,
                            8,
                            rows,
                            8,
                        )
                    torch.testing.assert_close(target, source, rtol=0, atol=0)
