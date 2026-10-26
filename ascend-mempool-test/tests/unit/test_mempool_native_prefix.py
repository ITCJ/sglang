"""Read/write actual NPU MLA pool layouts through a CPU scatter primitive."""

import unittest
from types import SimpleNamespace

import torch
from test_native_release import load_functions, load_methods


def native_methods(pool):
    namespace = dict(
        torch=torch,
        torch_npu=SimpleNamespace(
            npu_scatter_nd_update_=lambda dst, idx, src: dst.index_copy_(
                0, idx.flatten().long(), src
            )
        ),
    )
    path = "hardware_backend/npu/memory_pool_npu.py"
    load_functions(path, {"_mla_fia_nz_scatter_indices"}, namespace)
    cls = load_methods(
        path,
        "NPUMLATokenToKVPool",
        {"set_mla_kv_rows", "get_mla_kv_rows", "_set_fia_nz_kv_buffer"},
        namespace,
        standalone=True,
    )
    result = cls()
    result.__dict__.update(vars(pool))
    result.use_fia_nz = getattr(pool, "use_fia_nz", False)
    result.k_buffer = [
        pool.get_key_buffer(i + pool.start_layer) for i in range(pool.layer_num)
    ]
    result.v_buffer = [
        pool.get_value_buffer(i + pool.start_layer) for i in range(pool.layer_num)
    ]
    return result


class TestNativePrefixLayout(unittest.TestCase):
    def test_flat_and_nz_use_real_noncontiguous_token_addresses(self):
        for nz in (False, True):
            with self.subTest(nz=nz):
                k = torch.full((4, 128, 1, 512), -9, dtype=torch.bfloat16)
                rope = torch.full((4, 128, 1, 64), -9, dtype=torch.bfloat16)
                pool = native_methods(
                    SimpleNamespace(
                        start_layer=0,
                        layer_num=1,
                        kv_lora_rank=512,
                        qk_rope_head_dim=64,
                        page_size=128,
                        use_fia_nz=nz,
                        get_key_buffer=lambda _: k,
                        get_value_buffer=lambda _: rope,
                    )
                )
                loc = torch.tensor([256, 257, 128, 129])
                values = torch.arange(4 * 576).view(4, 1, 576).to(torch.bfloat16)
                pool.set_mla_kv_rows(0, loc, values[..., :512], values[..., 512:])
                a, b = pool.get_mla_kv_rows(0, loc)
                self.assertTrue(torch.equal(torch.cat((a, b), -1), values))
                physical = (
                    k.view(4, 32, 128, 16)[2, :, 0, :].flatten() if nz else k[2, 0, 0]
                )
                self.assertTrue(torch.equal(physical, values[0, 0, :512]))
                self.assertTrue(torch.all(k[0] == -9))


if __name__ == "__main__":
    unittest.main()
