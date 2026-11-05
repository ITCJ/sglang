"""Dual-host graph gate using the actual registered pool mappings."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sglang.srt.hardware_backend.npu.mempool.alive_pages import PageLease
    from sglang.srt.hardware_backend.npu.mempool.multibase import (
        BaseTable,
        MultibaseCopy,
        mapped_regions,
        view_regions,
    )
    from sglang.srt.hardware_backend.npu.mempool.page_locations import PageLocations
    from sglang.srt.hardware_backend.npu.mempool.runtime import (
        KVWriteExpectation,
        MempoolRuntime,
    )
else:
    from .alive_pages import PageLease
    from .multibase import BaseTable, MultibaseCopy, mapped_regions, view_regions
    from .page_locations import PageLocations
    from .runtime import KVWriteExpectation, MempoolRuntime


def verify_multibase_graph(device: Any, cohort: Any) -> dict[str, Any]:
    d, t, c = device, device.torch, device.config
    group_size = c.tp_size // c.groups
    segment, member = divmod(d.rank, group_size)
    retained = []
    if member == 0:
        local = [(d.view(d.side, segment), 10 if d.side == 0 else 20)]
        if d.side == 0:
            local.append((d.view(0, segment, region="prefix_main"), 30 + segment))
        for view, marker in local:
            values = t.stack(
                [t.full((576,), marker + page, dtype=t.bfloat16) for page in (0, 1)]
            ).to("npu")
            writer = MultibaseCopy(
                BaseTable([(values.data_ptr(), 2)], "npu", [values]),
                BaseTable(view_regions([view], 1152), "npu", [view]),
                2,
                row_bytes=1152,
            )
            writer.submit(
                t.tensor([0, 1], device="npu"),
                t.tensor([0, c.layers * c.page_size], device="npu"),
                t.ones(2, dtype=t.bool, device="npu"),
            )
            retained.append(writer)
        t.npu.current_stream().synchronize()
    cohort.exchange("multibase-graph/initialized")
    views = mapped_regions(d.pool, 0)
    hbm = [
        t.stack(
            [t.full((576,), marker + page, dtype=t.bfloat16) for page in (0, 1)]
        ).to("npu")
        for marker in (50, 60)
    ]
    source = BaseTable(
        view_regions(views, 1152) + [(v.data_ptr(), 2) for v in hbm],
        "npu",
        [d.pool, *hbm],
    )
    output = t.full((12, 576), -1, dtype=t.bfloat16, device="npu")
    transfer = MultibaseCopy(
        source,
        BaseTable([(output.data_ptr(), 12)], "npu", [output]),
        12,
        row_bytes=1152,
    )
    locations = PageLocations(1, 12, 0, "npu")
    canonical = list(range(12))
    replicas = [(i + 1) % 12 for i in canonical]
    locations.regions.copy_(
        t.tensor([[list(pair) for pair in zip(canonical, replicas)]], device="npu")
    )
    locations.pages.zero_()
    locations.valid.fill_(True)
    handles = t.zeros(12, dtype=t.int64, device="npu")
    logical = t.arange(12, dtype=t.int64, device="npu")
    enabled = t.zeros(12, dtype=t.bool, device="npu")

    def launch() -> None:
        region, page, valid = locations.resolve(handles, logical)
        stride = t.where(region < 10, c.layers * c.page_size, 1)
        transfer.submit(
            source.ranges[region, 0] + page * stride, logical, valid & enabled
        )

    stream = t.npu.Stream()
    stream.wait_stream(t.npu.current_stream())
    with t.npu.stream(stream):
        launch()
        stream.synchronize()
        addresses = [
            v.data_ptr()
            for v in (
                *transfer.metadata,
                locations.regions,
                locations.pages,
                locations.valid,
                locations.selected,
                output,
            )
        ]
        graph = t.npu.NPUGraph()
        with t.npu.graph(graph, stream=stream, auto_dispatch_capture=True):
            launch()
        cases = []
        for name, use_replica, page, omit_p, invalidate in (
            ("all-12", False, 0, False, False),
            ("post25-11", False, 1, True, False),
            ("replica-switch", True, 1, False, False),
            ("invalid-replica-fallback", True, 0, False, True),
            ("zero-valid", False, 0, False, False),
        ):
            locations.selected.fill_(int(use_replica))
            locations.pages.fill_(page)
            locations.valid[:, :, 1].fill_(not invalidate)
            enabled.fill_(name != "zero-valid")
            if omit_p:
                enabled[0] = False
            output.fill_(-1)
            graph.replay()
            stream.synchronize()
            markers = [10, 20, *range(30, 38), 50, 60]
            expected = []
            for slot in range(12):
                region = replicas[slot] if use_replica and not invalidate else slot
                active = (
                    name != "zero-valid"
                    and not (omit_p and slot == 0)
                    and (region < 2 or region >= 10 or region - 2 < c.groups)
                )
                expected.append(markers[region] + page if active else -1)
            if not t.equal(
                output.cpu(),
                t.tensor(expected, dtype=t.bfloat16)[:, None].expand_as(output.cpu()),
            ):
                raise AssertionError(f"multibase graph content differs: {name}")
            if addresses != [
                v.data_ptr()
                for v in (
                    *transfer.metadata,
                    locations.regions,
                    locations.pages,
                    locations.valid,
                    locations.selected,
                    output,
                )
            ]:
                raise AssertionError("graph metadata storage changed")
            cases.append(name)
    # D owns decode replay; P has no native Index K/model pool in this microtest.
    if d.side == 1:
        d.pool.enable_page_tables("npu")
        for side in (0, 1):
            d.pool.install_pages(side, 0, PageLease("graph-negative", 1, (0, 1)))
        runtime = MempoolRuntime(
            d.pool,
            req_pool_rows=3,
            max_context_len=c.capacity,
            start_layer=0,
            device="npu",
        )
        rows = t.zeros(1, dtype=t.int64, device="npu")
        values = t.zeros((1, 576), dtype=t.bfloat16, device="npu")
        batch = SimpleNamespace(
            req_pool_indices=rows,
            seq_lens=rows,
            forward_mode=SimpleNamespace(is_decode=lambda: True),
            out_cache_loc=rows,
        )

        def write_layers() -> None:
            for layer in range(c.layers):
                runtime.write_layer(layer, values[:, :512], values[:, 512:], batch)

        producer = t.npu.current_stream()
        with t.npu.stream(stream):
            stream.wait_stream(producer)
            runtime.begin_forward([], capture=True)
            write_layers()
            runtime.end_forward()
            stream.synchronize()
            replay_graph = t.npu.NPUGraph()
            runtime.begin_forward([], capture=True)
            with t.npu.graph(replay_graph, stream=stream, auto_dispatch_capture=True):
                write_layers()
            runtime.end_forward()
            runtime.bind(1, slot=0, prompt_tokens=c.page_size, prompt_slot=0)
            d.pool.active_page_versions[1, 0] = 2
            try:
                runtime.begin_forward(
                    [KVWriteExpectation(1, c.page_size, 1)], replay=True
                )
            except RuntimeError as error:
                if "stale request page table" not in str(error):
                    raise
            else:
                raise AssertionError("stale page table reached replay")
            stream.synchronize()
        d.pool.page_tables.clear()
        d.pool.page_locations.clear()
        d.pool.write_locations.clear()
        d.pool.page_leases.clear()
        d.pool.active_page_versions.clear()
    cohort.exchange("multibase-graph/verified")
    return dict(
        status="passed",
        source_capacity=12,
        post25_active_sources=c.groups + 3,
        full_shape=c.groups == 8,
        captures=1,
        replays=len(cases),
        cases=cases,
        stale_before_replay="passed" if d.side == 1 else "executed on D peer",
        storage="actual dual-host BM mappings",
    )
