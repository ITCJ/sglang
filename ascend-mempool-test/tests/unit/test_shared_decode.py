"""Shared decode data through the runtime and external copy boundary."""

import unittest
from types import SimpleNamespace

import torch
from test_offload import CPUWriteKernel
from test_pool import FakeBM
from test_runtime import ManualEvent, prefill_batch

from ascend_mempool.layout import KVLayout, PoolLayout
from ascend_mempool.manager import MempoolKVManager
from ascend_mempool.runtime import KVWriteExpectation, MempoolRuntime


class TestSharedDecode(unittest.TestCase):
    def test_split_writers_supply_current_locally_and_read_complete_previous_rows(self):
        from ascend_mempool.decode_write import DecodeWriter

        for assignment in ("features", "layers"):
            layout = KVLayout(2, 2, 4, 1, 4)
            memory = [
                torch.full(layout.shape, -1, dtype=torch.bfloat16) for _ in range(2)
            ]
            runtimes, bindings = [], []
            for member in (0, 1):
                manager = MempoolKVManager.create(
                    PoolLayout(layout, layout), 1, bm_module=FakeBM(1)
                )
                manager.join(0.1)
                self.addCleanup(manager.close, drain=lambda: None)
                sources = {
                    manager.view(1, layer).device_base: memory[layer]
                    for layer in range(2)
                }
                sources.update(
                    {
                        manager.view(0, layer).device_base: torch.zeros(
                            layout.shape, dtype=torch.bfloat16
                        )
                        for layer in range(2)
                    }
                )

                def read(
                    src,
                    dst,
                    si,
                    di,
                    valid,
                    sr,
                    dr,
                    rb,
                    count,
                    block,
                    sp,
                    dp,
                    sources=sources,
                ):
                    source = sources[sp] if sp is not None else src
                    dst.reshape(dr, rb // 2)[di[valid]] = source.reshape(sr, rb // 2)[
                        si[valid]
                    ]

                def event():
                    value = ManualEvent()
                    value.done = True
                    return value

                runtime = MempoolRuntime(
                    manager,
                    req_pool_rows=3,
                    max_context_len=8,
                    start_layer=0,
                    device="cpu",
                    fetch_enabled=True,
                    fetch_kernel=read,
                    kernels=[
                        CPUWriteKernel(
                            memory[layer], manager.view(1, layer).device_base
                        )
                        for layer in range(2)
                    ],
                    event_factory=event,
                    decode_writer=DecodeWriter(member, 2, assignment),
                )
                runtimes.append(runtime)
                bindings.append(runtime.bind(1, slot=0, prompt_tokens=2, prompt_slot=1))
            for step in range(2):
                for member, runtime in enumerate(runtimes):
                    batch = prefill_batch((1, 1), (0, 0), (1, 0))
                    batch.forward_mode = SimpleNamespace(is_decode=lambda: True)
                    batch.seq_lens = torch.tensor([3 + step, 0])
                    runtime.begin_forward([KVWriteExpectation(1, 2 + step, 1)])
                    for layer in range(2):
                        values = (
                            torch.tensor(
                                [[11, 12, 13, 14], [0, 0, 0, 0]], dtype=torch.bfloat16
                            )
                            + 20 * step
                            + 100 * layer
                        )
                        runtime.write_layer(layer, values[:, :2], values[:, 2:], batch)
                        positions = torch.tensor(
                            [[2 + step, 2 if step else -1], [2, 3]]
                        )
                        mask = runtime.selected_kv_valid(
                            layer, batch.req_pool_indices, positions
                        )
                        output = torch.full((2, 2, 1, 4), -7, dtype=torch.bfloat16)
                        runtime.fetch_selected_kv(
                            layer, batch.req_pool_indices, positions, mask, output
                        )
                        self.assertEqual(output[0, 0, 0].tolist(), values[0].tolist())
                        self.assertTrue((output[1] == -7).all())
                        if step:
                            self.assertEqual(
                                output[0, 1, 0].tolist(),
                                [
                                    11 + 100 * layer,
                                    12 + 100 * layer,
                                    13 + 100 * layer,
                                    14 + 100 * layer,
                                ],
                            )
                    runtime.end_forward()
                    runtime.poll_completed()
                for layer in range(2):
                    self.assertEqual(
                        memory[layer][0, step, 0].tolist(),
                        [
                            11 + 20 * step + 100 * layer,
                            12 + 20 * step + 100 * layer,
                            13 + 20 * step + 100 * layer,
                            14 + 20 * step + 100 * layer,
                        ],
                    )
            for runtime, binding in zip(runtimes, bindings):
                self.assertEqual(runtime.completion_report(binding)["written_kv"], 2)


if __name__ == "__main__":
    unittest.main()
