import unittest
from types import SimpleNamespace

from ascend_mempool.layout import KVLayout, PoolLayout
from ascend_mempool.pool import MempoolKVManager


class FakeBM:
    """A BM SDK boundary with distinct GVA and process-local device mappings."""

    BmMemType = SimpleNamespace(HOST="host", LOCAL_DEVICE="device")
    BmDataOpType = SimpleNamespace(SDMA=1)

    def __init__(self, rank):
        self.rank = rank
        self.handle = None

    def create2(self, **options):
        self.handle = FakeHandle(self.rank, options)
        return self.handle


class FakeHandle:
    def __init__(self, rank, options):
        self.rank = rank
        self.options = options
        self.destroyed = False
        self.mapping_available = True
        self.stride_error = 0
        self.discontinuous_mapping = False

    def join(self):
        return 0

    def leave(self):
        if self.destroyed:
            raise RuntimeError("handle already destroyed")
        return 0

    def destroy(self):
        self.destroyed = True

    def local_mem_size(self, mem_type):
        return self.options["local_dram_size"]

    def peer_rank_ptr(self, rank, mem_type):
        return 0x1000000000 + rank * (self.options["max_dram_size"] + self.stride_error)

    def gva_to_va(self, gva, mem_type):
        if not self.mapping_available:
            return 0
        stride = self.options["max_dram_size"]
        rank, offset = divmod(gva - 0x1000000000, stride)
        if self.discontinuous_mapping and offset > 0:
            offset += 32
        return (0x5000000000, 0x7000000000)[rank] + offset


class TestMempoolKVManager(unittest.TestCase):
    def setUp(self):
        self.layout = PoolLayout(
            KVLayout(layers=2, slots=16, tokens=16384, heads=1, dim=576),
            KVLayout(layers=2, slots=16, tokens=32768, heads=1, dim=576),
        )

    def test_views_resolve_logical_elements_using_local_device_mapping(self):
        manager = MempoolKVManager.create(self.layout, rank=1, bm_module=FakeBM(1))
        manager.join(timeout=0.1)
        prompt = manager.view(rank=0, layer=1)
        decode = manager.view(rank=1, layer=0)
        self.assertEqual(prompt.shape, (16, 16384, 1, 576))
        self.assertEqual(prompt.dtype, "bfloat16")
        self.assertEqual(
            prompt.element_gva(slot=2, token=3, column=7), 0x1000000000 + 339742094
        )
        self.assertEqual(
            prompt.element_device_ptr(slot=2, token=3, column=7),
            0x5000000000 + 339742094,
        )
        self.assertEqual(decode.gva_base, 0x1080000000)
        self.assertEqual(decode.device_base, 0x7000000000)
        manager.close(drain=lambda: None)

    def test_failed_drain_keeps_views_alive_and_close_invalidates_them(self):
        manager = MempoolKVManager.create(self.layout, rank=1, bm_module=FakeBM(1))
        manager.join(timeout=0.1)
        view = manager.view(rank=0, layer=0)

        def pending_work():
            raise RuntimeError("pending NPU work")

        with self.assertRaisesRegex(RuntimeError, "pending NPU work"):
            manager.close(drain=pending_work)
        self.assertEqual(view.device_base, 0x5000000000)
        manager.close(drain=lambda: None)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            view.element_device_ptr(slot=0, token=0)
        manager.close(drain=lambda: None)

    def test_join_only_exposes_verified_mappings(self):
        for defect, error in (
            ("mapping_available", TimeoutError),
            ("stride_error", RuntimeError),
            ("discontinuous_mapping", RuntimeError),
        ):
            with self.subTest(defect=defect):
                sdk = FakeBM(1)
                manager = MempoolKVManager.create(self.layout, rank=1, bm_module=sdk)
                setattr(
                    sdk.handle, defect, False if defect == "mapping_available" else 32
                )
                with self.assertRaises(error):
                    manager.join(timeout=0.01)
                with self.assertRaisesRegex(RuntimeError, "not ready"):
                    manager.view(rank=0, layer=0).device_base
                manager.close(drain=lambda: None)


if __name__ == "__main__":
    unittest.main()
