import unittest

import torch
from test_pool import FakeBM

from ascend_mempool.copy import SparseCopyInputs, SparseKVCopy
from ascend_mempool.layout import KVLayout, PoolLayout
from ascend_mempool.pool import MempoolKVManager
from ascend_mempool.verification import (
    SENTINEL,
    expected_output,
    kv_pattern,
    make_cases,
)


class TestSparseKVCopy(unittest.TestCase):
    def setUp(self):
        layout = PoolLayout(
            KVLayout(layers=1, slots=16, tokens=8, heads=1, dim=8),
            KVLayout(layers=1, slots=16, tokens=16, heads=1, dim=8),
        )
        self.manager = MempoolKVManager.create(layout, rank=1, bm_module=FakeBM(1))
        self.manager.join(timeout=0.1)
        self.inputs = SparseCopyInputs(batch_rows=16, topk=6, device="cpu")
        self.copy = SparseKVCopy(
            self.manager.view(0, 0), self.manager.view(1, 0), self.inputs
        )

    def tearDown(self):
        self.manager.close(drain=lambda: None)

    def test_prompt_decode_boundary_written_length_and_padding(self):
        inputs = self.inputs
        inputs.p_slots[0] = 2
        inputs.d_slots[0] = 3
        inputs.prompt_lengths[0] = 4
        inputs.decode_lengths[0] = 3
        inputs.positions[0] = torch.tensor([0, 3, 4, 6, 7, -1])
        inputs.valid[0] = True
        inputs.active[0] = True
        prompt, decode = self.copy.routes()
        self.assertEqual(prompt.src_index[:6].tolist(), [16, 19, 0, 0, 0, 0])
        self.assertEqual(
            prompt.valid[:6].tolist(), [True, True, False, False, False, False]
        )
        self.assertEqual(decode.src_index[:6].tolist(), [0, 0, 48, 50, 0, 0])
        self.assertEqual(
            decode.valid[:6].tolist(), [False, False, True, True, False, False]
        )
        self.assertEqual(prompt.dst_index[:6].tolist(), [0, 1, 2, 3, 4, 5])
        self.assertFalse(prompt.valid[6:].any().item())
        self.assertFalse(decode.valid[6:].any().item())

    def test_two_sources_write_only_selected_destination_rows(self):
        p_source = torch.zeros((16, 8, 1, 8), dtype=torch.bfloat16)
        d_source = torch.zeros((16, 16, 1, 8), dtype=torch.bfloat16)
        p_source[2, 0] = 101
        p_source[2, 3] = 103
        d_source[3, 0] = 201
        d_source[3, 2] = 203
        sources = {0x5000000000: p_source, 0x7000000000: d_source}

        def cpu_kernel(
            src,
            dst,
            src_index,
            dst_index,
            valid,
            src_rows,
            dst_rows,
            row_bytes,
            max_copy,
            block_dim,
            src_ptr,
            dst_ptr,
        ):
            source = sources[src_ptr].reshape(src_rows, row_bytes // 2)
            target = dst.reshape(dst_rows, row_bytes // 2)
            target[dst_index[valid]] = source[src_index[valid]]

        copy = SparseKVCopy(
            self.manager.view(0, 0),
            self.manager.view(1, 0),
            self.inputs,
            kernel=cpu_kernel,
        )
        self.inputs.p_slots[0] = 2
        self.inputs.d_slots[0] = 3
        self.inputs.prompt_lengths[0] = 4
        self.inputs.decode_lengths[0] = 3
        self.inputs.positions[0] = torch.tensor([0, 3, 4, 6, 7, -1])
        self.inputs.valid[0] = True
        self.inputs.active[0] = True
        copy.output.fill_(-1)
        actual = copy.gather()
        self.assertEqual(actual[0, :, 0, 0].tolist(), [101, 103, 201, 203, -1, -1])
        self.assertTrue((actual[1:] == -1).all().item())

        for slot in range(16):
            p_source[slot] = kv_pattern(0, 0, slot, 0, 8, 1, 8)
            d_source[slot] = kv_pattern(1, 0, slot, 0, 16, 1, 8)
        for cycle in range(2):
            for case in make_cases(self.manager.layout, 16, 6, 3, cycle):
                with self.subTest(cycle=cycle, case=case.name):
                    case.load(self.inputs)
                    copy.output.fill_(SENTINEL)
                    actual = copy.gather()
                    reference = expected_output(case, self.manager.layout, layer=0)
                    self.assertTrue(torch.equal(actual, reference))

    def test_cases_change_bindings_without_replacing_device_inputs(self):
        cases = make_cases(self.manager.layout, batch_rows=16, topk=6, active_rows=2)
        tensors = (
            self.inputs.p_slots,
            self.inputs.d_slots,
            self.inputs.prompt_lengths,
            self.inputs.decode_lengths,
            self.inputs.positions,
            self.inputs.valid,
            self.inputs.active,
        )
        pointers = [tensor.data_ptr() for tensor in tensors]
        cases[0].load(self.inputs)
        prompt, decode = self.copy.routes()
        self.assertTrue(prompt.valid.any().item())
        self.assertFalse(decode.valid.any().item())
        cases[1].load(self.inputs)
        prompt, decode = self.copy.routes()
        self.assertFalse(prompt.valid.any().item())
        self.assertTrue(decode.valid.any().item())
        self.assertEqual([tensor.data_ptr() for tensor in tensors], pointers)


if __name__ == "__main__":
    unittest.main()
