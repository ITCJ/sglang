"""Execute the scatter kernel's address arithmetic with a CPU Triton boundary."""

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace

import torch
from test_mempool_native_prefix import native_methods


class Pointer:
    def __init__(self, tensor, offset=0):
        self.tensor, self.offset = tensor.reshape(-1), torch.as_tensor(offset)

    def __add__(self, offset):
        return Pointer(self.tensor, self.offset + offset)


def load(pointer, mask=None, other=0):
    if mask is None:
        return pointer.tensor[pointer.offset]
    safe = torch.where(mask, pointer.offset, 0)
    return torch.where(mask, pointer.tensor[safe], other)


def store(pointer, values, mask):
    pointer.tensor[pointer.offset[mask]] = values[mask]


class TestRestoreKernel(unittest.TestCase):
    def test_single_scatter_handles_flat_nz_split_and_index_without_padding_rows(self):
        source = (
            Path(__file__).resolve().parents[3]
            / "python/sglang/srt/hardware_backend/npu/mempool/restore_kernel.py"
        )
        definition = next(
            n
            for n in ast.parse(source.read_text()).body
            if isinstance(n, ast.FunctionDef) and n.name == "_scatter_rows"
        )
        definition.decorator_list = []
        tl = SimpleNamespace(
            load=load,
            store=store,
            arange=torch.arange,
            int64=torch.int64,
            constexpr=int,
        )
        namespace = dict(tl=tl)
        exec(
            compile(
                ast.Module(body=[definition], type_ignores=[]), str(source), "exec"
            ),
            namespace,
        )
        scatter = namespace["_scatter_rows"]
        # Slot rows 2/3 are padding and contain NaNs; the table has no entries for them.
        table = torch.tensor([[0, 1, 4, 5], [256, 257, 128, 129]])
        for nz in (False, True):
            with self.subTest(nz=nz):
                key = torch.full((4, 128, 1, 512), -9, dtype=torch.bfloat16)
                rope = torch.full((4, 128, 1, 64), -9, dtype=torch.bfloat16)
                index = torch.full((512, 1, 128), -9, dtype=torch.bfloat16)
                pool = native_methods(
                    SimpleNamespace(
                        start_layer=0,
                        layer_num=1,
                        kv_lora_rank=512,
                        qk_rope_head_dim=64,
                        page_size=128,
                        use_fia_nz=nz,
                        get_key_buffer=lambda _: key,
                        get_value_buffer=lambda _: rope,
                    )
                )
                for width, k, v, dim, tail, tiled in (
                    (576, key, rope, 512, 64, nz),
                    (128, index, index, 128, 0, False),
                ):
                    values = (
                        torch.arange(6 * width).reshape(6, 1, width).to(torch.bfloat16)
                    )
                    values[2:4] = float("nan")
                    for row in range(4):
                        tl.program_id = lambda axis, row=row: row
                        scatter(
                            Pointer(values),
                            Pointer(table),
                            Pointer(k),
                            Pointer(v),
                            4,
                            dim,
                            tail,
                            128,
                            tiled,
                            1024 if tail else 128,
                        )
                    actual = (
                        torch.cat(pool.get_mla_kv_rows(0, table[1]), -1)
                        if tail
                        else index[table[1]]
                    )
                    self.assertTrue(torch.equal(actual, values[table[0]]))
                    self.assertTrue(torch.all(k.reshape(-1, dim)[:128] == -9))


if __name__ == "__main__":
    unittest.main()
