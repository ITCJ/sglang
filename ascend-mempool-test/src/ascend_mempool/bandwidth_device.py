"""Real BM L2G writes and deployed UniDexCopy reads for the standalone suite."""

from __future__ import annotations

import math
import random
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from .bandwidth import PRODUCERS, BenchmarkConfig, Case, write_pages
from .multibase_fixture import BenchmarkCopy

if TYPE_CHECKING:
    from sglang.srt.hardware_backend.npu.mempool.layout import KVLayout, PoolLayout
    from sglang.srt.hardware_backend.npu.mempool.manager import (
        MempoolKVView,
    )
    from sglang.srt.hardware_backend.npu.mempool.sharing_plan import (
        HostTopology,
        MempoolNumaConfig,
    )
else:
    from .layout import KVLayout, PoolLayout
    from .manager import MempoolKVView
    from .sharing_plan import HostTopology, MempoolNumaConfig


def pool_plan(c: BenchmarkConfig, side: int, host: str, port: int, run_id: str) -> Any:
    common: dict[str, Any] = dict(
        slots=2, tokens=c.capacity, heads=1, page_size=c.page_size
    )
    layout = PoolLayout(
        KVLayout(layers=c.layers, dim=576, **common),
        KVLayout(layers=c.layers, dim=576, **common),
        prompt_index=KVLayout(layers=len(PRODUCERS), dim=128, **common),
        prefix_main=KVLayout(
            c.layers,
            c.capacity // c.page_size,
            c.page_size,
            1,
            576,
            page_size=c.page_size,
        ),
        prefix_index=KVLayout(
            len(PRODUCERS),
            c.capacity // c.page_size,
            c.page_size,
            1,
            128,
            page_size=c.page_size,
        ),
    )
    return MempoolNumaConfig(False, ",".join(map(str, c.nodes))).plan(
        hosts=(
            HostTopology(host, tuple(range(c.tp_size)), ",".join(map(str, c.nodes))),
        ),
        tp_size=c.tp_size,
        role=("prefill", "decode")[side],
        instance_id=run_id,
        kv_identity="ticket14-glm52",
        layout=layout,
        base_port=port + 2,
        nic_port=port + 64,
        pool_id=114,
        prefill_shared=True,
        decode_shared=True,
        prefill_index_shared=True,
        prefix_shared=True,
    )


class Device:
    def __init__(
        self,
        pool: Any,
        torch: Any,
        bm: Any,
        config: BenchmarkConfig,
        side: int,
        rank: int,
        device_id: int,
    ) -> None:
        self.pool, self.torch, self.bm, self.config = pool, torch, bm, config
        self.side, self.rank, self.device_id = side, rank, device_id
        self.handle = pool._handle
        self.placeholder = torch.empty(1, dtype=torch.bfloat16)
        self.kernel = BenchmarkCopy()

    def view(
        self, side: int, segment: int, layer: int = 0, region: str = "alive_main"
    ) -> Any:
        owner = self.pool.session.region_owner(side, region, segment)
        offset, layout = self.pool.session.regions(owner)[region]
        return MempoolKVView(self.pool, side, layer, layout, offset, owner, region)

    def transfer(self, blocks: list[tuple[int, int, int]]) -> None:
        # Submit and wait on the same thread. Every caller retains source tensors.
        for start in range(0, len(blocks), 256):
            batch = blocks[start : start + 256]
            ret = self.handle.copy_data_batch(
                [b[0] for b in batch],
                [b[1] for b in batch],
                [b[2] for b in batch],
                len(batch),
                self.bm.BmCopyType.L2G,
                flags=1,
                stream=0,
            )
            if ret != 0:
                raise RuntimeError(f"L2G batch failed: {ret}")
            ret = self.handle.wait()
            if ret != 0:
                raise RuntimeError(f"L2G completion unknown: {ret}")

    def raw_values(self, offsets: list[int], size: int, salt: int) -> Any:
        t = self.torch
        indices = t.tensor(offsets, dtype=t.int64)[:, None] // 2
        values = (indices + t.arange(size // 2)[None, :] + salt) % 251
        return values.to(t.bfloat16).contiguous()

    def initialize_read(self, case: Case) -> None:
        c, t = self.config, self.torch
        source_side = 1 - case.side if case.location == "remote" else case.side
        segments = (
            range(c.groups) if case.striped else (1 if case.location == "other" else 0,)
        )
        offsets = read_offsets(c, case)
        size = case.block_bytes(c)
        for segment in segments:
            if (
                self.side != source_side
                or self.rank != self.pool.session.main_groups[segment][0]
            ):
                continue
            view = self.view(source_side, segment)
            salt = source_side * 17 + segment
            for start in range(0, len(offsets), 64):
                selection = offsets[start : start + 64]
                values = self.raw_values(selection, size, salt).to("npu")
                t.npu.current_stream().synchronize()
                self.transfer(
                    [
                        (values.data_ptr() + i * size, view.gva_base + offset, size)
                        for i, offset in enumerate(selection)
                    ]
                )


def read_offsets(c: BenchmarkConfig, case: Case) -> list[int]:
    rng = random.Random(c.seed)
    blocks = case.read_blocks(c)
    arena = c.capacity * c.layers * c.row_bytes  # slot 1, disjoint from writes
    if case.grain == "token" and not case.slice_bytes:
        rows = rng.sample(range(c.capacity), blocks)
        return [
            arena
            + (r // c.page_size * c.layers * c.page_size + r % c.page_size)
            * c.row_bytes
            for r in rows
        ]
    pages = c.capacity // c.page_size
    # Small reads may revisit pages, but every destination slice remains distinct.
    selected = [rng.randrange(pages) for _ in range(blocks)]
    return [arena + p * c.page_bytes for p in selected]


class ReadWork:
    def __init__(self, device: Device, case: Case) -> None:
        self.d, self.case = device, case
        c, t = device.config, device.torch
        self.side = 1 - case.side if case.location == "remote" else case.side
        self.segment = (
            device.rank // (c.tp_size // c.groups)
            if case.striped
            else (1 if case.location == "other" else 0)
        )
        self.view = device.view(self.side, self.segment)
        self.offsets = read_offsets(c, case)
        self.size = case.block_bytes(c)
        self.tile = self.size if self.size <= 16384 else math.gcd(self.size, 16384)
        self.nbytes = len(self.offsets) * self.size
        self.output = t.empty(self.nbytes // 2, dtype=t.bfloat16, device="npu")
        indices = t.tensor(self.offsets, dtype=t.int64)[:, None] // self.tile
        indices = (indices + t.arange(self.size // self.tile)[None, :]).flatten()
        self.source = indices.to("npu")
        self.target = t.arange(indices.numel(), dtype=t.int64, device="npu")
        self.valid = t.ones_like(self.target, dtype=t.bool)
        self.stream = t.npu.Stream()
        t.npu.current_stream().synchronize()
        self.segments = {f"{self.side}/{c.nodes[self.segment]}": self.nbytes}

    def launch(self) -> None:
        d = self.d
        d.kernel(
            d.placeholder,
            self.output,
            self.source,
            self.target,
            self.valid,
            self.view.layout.total_bytes // self.tile,
            self.nbytes // self.tile,
            self.tile,
            self.source.numel(),
            48,
            self.view.device_base,
            None,
        )

    def run(self) -> None:
        with self.d.torch.npu.stream(self.stream):
            self.launch()
        self.stream.synchronize()

    def verify(self) -> None:
        t = self.d.torch
        actual = self.output.cpu().reshape(len(self.offsets), -1)
        for start in range(0, len(self.offsets), 64):
            expected = self.d.raw_values(
                self.offsets[start : start + 64],
                self.size,
                self.side * 17 + self.segment,
            )
            if not t.equal(actual[start : start + 64], expected):
                raise AssertionError(f"{self.case.name}: device read content differs")


class WriteWork:
    """One logical copy at D segment 0; P prefix pages interleave all segments."""

    def __init__(
        self, device: Device, case: Case, layer: int = 0, variant: int = 0
    ) -> None:
        self.d, self.case, self.layer, self.variant = device, case, layer, variant
        c, t = device.config, device.torch
        pages = write_pages(c, case, device.rank)
        self.pages = pages
        self.sources: list[Any] = []
        self.blocks: list[tuple[int, int, int]] = []
        self.checks: list[tuple[Any, int, Any]] = []
        self.segments: dict[str, int] = defaultdict(int)
        self.nbytes = 0
        self.prepare()
        t.npu.current_stream().synchronize()

    def prepare(self) -> None:
        d, case, c, t = self.d, self.case, self.d.config, self.d.torch
        prefix = d.pool.session.prefix_layout
        # Same interleaved allocation policy as service prefix candidates.
        allocated = prefix.select(
            set(range(prefix.pages)), math.ceil(case.tokens / c.page_size), 0
        )
        for page in self.pages:
            count = min(c.page_size, case.tokens - page * c.page_size)
            tokens = t.arange(page * c.page_size, page * c.page_size + count)
            # BF16-exact independent logical coordinates, not destination addresses.
            cols = t.arange(576)[None, :]
            values = (
                (tokens[:, None] + cols * 3 + self.layer * 7 + self.variant * 19) % 251
            ).to(t.bfloat16)
            source = values.to("npu")
            self.sources.append(source)
            targets = []
            if case.group == "locations":
                side = 1 - case.side if case.location == "remote" else case.side
                segment = 1 if case.location == "other" else 0
                targets.append((d.view(side, segment, self.layer), page, segment, side))
            else:
                if case.targets in ("remote", "dual"):
                    # Scatter alive pages as after allocator reuse; no contiguous shortcut.
                    physical = page * 17 % (c.capacity // c.page_size)
                    targets.append((d.view(1, 0, self.layer), physical, 0, 1))
                if case.targets in ("local", "dual"):
                    segment, physical = prefix.decode(allocated[page])
                    targets.append(
                        (
                            d.view(0, segment, self.layer, "prefix_main"),
                            physical,
                            segment,
                            0,
                        )
                    )
            for view, physical, segment, side in targets:
                offset = physical * view.layout.page_bytes
                nbytes = count * c.row_bytes
                self.blocks.append((source.data_ptr(), view.gva_base + offset, nbytes))
                self.checks.append((view, offset, values))
                self.segments[f"{side}/{c.nodes[segment]}"] += nbytes
                self.nbytes += nbytes
            if (
                case.group != "locations"
                and case.targets in ("local", "dual")
                and self.layer in PRODUCERS
            ):
                segment, physical = prefix.decode(allocated[page])
                view = d.view(0, segment, PRODUCERS.index(self.layer), "prefix_index")
                values = (
                    (
                        tokens[:, None]
                        + t.arange(128)[None, :] * 5
                        + self.layer
                        + self.variant * 19
                    )
                    % 251
                ).to(t.bfloat16)
                source = values.to("npu")
                self.sources.append(source)
                offset = physical * view.layout.page_bytes
                nbytes = count * 256
                self.blocks.append((source.data_ptr(), view.gva_base + offset, nbytes))
                self.checks.append((view, offset, values))
                self.segments[f"0/{c.nodes[segment]}"] += nbytes
                self.nbytes += nbytes

    def run(self) -> None:
        self.d.transfer(self.blocks)

    def verify(self) -> None:
        # Normal NPU readback, never direct CPU loads from a BM host mapping.
        d, t = self.d, self.d.torch
        for view, offset, expected in self.checks:
            rows = expected.shape[0]
            width = expected.shape[1] * 2
            idx = t.arange(rows, dtype=t.int64, device="npu")
            result = t.empty_like(expected, device="npu")
            d.kernel(
                d.placeholder,
                result,
                idx,
                idx,
                t.ones_like(idx, dtype=t.bool),
                rows,
                rows,
                width,
                rows,
                48,
                view.device_base + offset,
                None,
            )
            t.npu.current_stream().synchronize()
            if not t.equal(result.cpu(), expected):
                raise AssertionError(
                    f"{self.case.name}: write content differs at layer {self.layer}"
                )


def pipeline(device: Device, case: Case, variant: int = 0) -> dict[str, Any]:
    """Queue each layer once; source buffers remain alive through the final drain."""
    c, t = device.config, device.torch
    from .multibase_write import MultibaseWriteWork

    writer = MultibaseWriteWork if c.copy_backend == "multibase" else WriteWork
    work = [writer(device, case, layer, variant) for layer in range(c.layers)]
    timings: list[dict[str, float]] = []

    def setup() -> None:
        t.npu.set_device(device.device_id)

    def execute(w: Any, queued: float) -> None:
        started = time.perf_counter()
        w.run()
        timings.append(
            dict(
                queue_ms=(started - queued) * 1000,
                copy_ms=(time.perf_counter() - started) * 1000,
            )
        )

    start = time.perf_counter()
    with ThreadPoolExecutor(1, initializer=setup) as executor:
        futures = [executor.submit(execute, w, time.perf_counter()) for w in work]
        last_submit = time.perf_counter()
        for future in futures:
            future.result()
    done = time.perf_counter()
    return dict(
        layers=c.layers,
        total_ms=(done - start) * 1000,
        tail_drain_ms=(done - last_submit) * 1000,
        retained_source_bytes=sum(
            s.numel() * s.element_size() for w in work for s in w.sources
        ),
        timings=timings,
        mode="unpaced_transfer_only_not_model_overlap",
    )
