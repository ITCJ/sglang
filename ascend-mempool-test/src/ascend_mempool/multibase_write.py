"""Ticket26 kernel write workload: one D replica per pair, one prefix replica."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sglang.srt.hardware_backend.npu.mempool.multibase import (
        BaseTable,
        MultibaseCopy,
        view_regions,
    )
else:
    from .multibase import BaseTable, MultibaseCopy, view_regions


class MultibaseWriteWork:
    def __init__(
        self, device: Any, case: Any, layer: int = 0, variant: int = 0
    ) -> None:
        self.d, self.case, self.layer, self.variant = device, case, layer, variant
        c, t = device.config, device.torch
        self.segment, member = divmod(device.rank, c.tp_size // c.groups)
        group_size = c.tp_size // c.groups
        prefix = device.pool.session.prefix_layout
        allocated = prefix.select(
            set(range(prefix.pages)), (case.tokens + c.page_size - 1) // c.page_size, 0
        )
        token = t.arange(case.tokens, dtype=t.int64)
        page, within = token // c.page_size, token % c.page_size
        remote_mask = (page % group_size == member) & (
            case.targets in ("remote", "dual")
        )
        local_mask = (page % c.tp_size == device.rank) & (
            case.targets in ("local", "dual")
        )
        target_side = 1
        if case.group == "locations":
            from .bandwidth import write_pages

            owned = write_pages(c, case, device.rank)
            remote_mask = t.isin(page, t.tensor(owned, dtype=t.int64))
            local_mask.zero_()
            target_side = 1 - case.side if case.location == "remote" else case.side
            self.segment = 1 if case.location == "other" else 0
        self.pages = sorted(set(page[remote_mask | local_mask].tolist()))
        values = (
            (token[:, None] + t.arange(576)[None, :] * 3 + layer * 7 + variant * 19)
            % 251
        ).to(t.bfloat16)
        self.values = values.to("npu")
        views = [device.view(target_side, self.segment, layer)] + [
            device.view(0, i, layer, "prefix_main") for i in range(c.groups)
        ]
        table = BaseTable(view_regions(views, c.row_bytes), "npu", views)
        self.transfer = MultibaseCopy(
            BaseTable([(self.values.data_ptr(), case.tokens)], "npu", [self.values]),
            table,
            case.tokens * 2,
            row_bytes=c.row_bytes,
        )
        remote_rows = (
            page * 17 % (c.capacity // c.page_size)
        ) * c.layers * c.page_size + within
        if case.group == "locations":
            remote_rows = page * c.layers * c.page_size + within
        locations = [prefix.decode(allocated[p]) for p in page.tolist()]
        prefix_regions = t.tensor([x[0] + 1 for x in locations], dtype=t.int64)
        prefix_rows = (
            t.tensor([x[1] for x in locations], dtype=t.int64) * c.layers * c.page_size
            + within
        )
        starts = t.tensor(table.starts, dtype=t.int64)
        self.si = t.cat((token, token)).to("npu")
        self.di = t.cat((remote_rows, starts[prefix_regions] + prefix_rows)).to("npu")
        self.mask = t.cat((remote_mask, local_mask)).to("npu")
        self.target_bytes = dict(
            remote_main=int(remote_mask.sum()) * c.row_bytes,
            local_main=int(local_mask.sum()) * c.row_bytes,
            local_index=0,
        )
        self.checks = [(views[0], remote_rows[remote_mask], values[remote_mask])]
        for segment in range(c.groups):
            mask = local_mask & (prefix_regions == segment + 1)
            self.checks.append((views[segment + 1], prefix_rows[mask], values[mask]))
        # Index K writes only the local candidate pages; D retains native send.
        from .bandwidth import PRODUCERS

        self.index_transfer = None
        self.index_values = None
        if layer in PRODUCERS and bool(local_mask.any()):
            index_layer = PRODUCERS.index(layer)
            index_views = [
                device.view(0, i, index_layer, "prefix_index") for i in range(c.groups)
            ]
            index_values = (
                (token[:, None] + t.arange(128)[None, :] * 5 + layer + variant * 19)
                % 251
            ).to(t.bfloat16)
            self.index_values = index_values.to("npu")
            index_table = BaseTable(view_regions(index_views, 256), "npu", index_views)
            self.index_transfer = MultibaseCopy(
                BaseTable(
                    [(self.index_values.data_ptr(), case.tokens)],
                    "npu",
                    [self.index_values],
                ),
                index_table,
                case.tokens,
                row_bytes=256,
            )
            index_starts = t.tensor(index_table.starts, dtype=t.int64)
            index_rows = (
                t.tensor([x[1] for x in locations], dtype=t.int64)
                * len(PRODUCERS)
                * c.page_size
                + within
            )
            self.index_si = token.to("npu")
            self.index_di = (index_starts[prefix_regions - 1] + index_rows).to("npu")
            self.index_mask = local_mask.to("npu")
            self.target_bytes["local_index"] = int(local_mask.sum()) * 256
            for segment, view in enumerate(index_views):
                mask = local_mask & (prefix_regions == segment + 1)
                self.checks.append((view, index_rows[mask], index_values[mask]))
        self.nbytes = sum(self.target_bytes.values())
        self.sources = [self.values] + (
            [self.index_values] if self.index_values is not None else []
        )
        self.blocks: list[Any] = []
        self.segments = {
            f"{target_side}/{c.nodes[self.segment]}": self.target_bytes["remote_main"]
        }
        for segment in range(c.groups):
            mask = local_mask & (prefix_regions == segment + 1)
            key = f"0/{c.nodes[segment]}"
            self.segments[key] = self.segments.get(key, 0) + int(mask.sum()) * (
                c.row_bytes + (256 if self.index_transfer else 0)
            )
        self.readbacks = []
        for view, physical_rows, expected in self.checks:
            if not physical_rows.numel():
                continue
            width = expected.shape[1] * 2
            output = t.empty_like(expected, device="npu")
            reader = MultibaseCopy(
                BaseTable(view_regions([view], width), "npu", [view]),
                BaseTable([(output.data_ptr(), output.shape[0])], "npu", [output]),
                output.shape[0],
                row_bytes=width,
            )
            reader.source_rows.copy_(physical_rows.to("npu"))
            reader.target_rows.copy_(
                t.arange(output.shape[0], dtype=t.int64, device="npu")
            )
            reader.valid.fill_(True)
            self.readbacks.append((reader, output, expected))
        self.stream = t.npu.Stream()
        self.graph: Any = None
        self.done = t.npu.Event()
        t.npu.current_stream().synchronize()

    def launch(self) -> None:
        self.transfer.submit(self.si, self.di, self.mask)
        if self.index_transfer is not None:
            self.index_transfer.submit(self.index_si, self.index_di, self.index_mask)

    def run(self) -> None:
        t = self.d.torch
        with t.npu.stream(self.stream):
            if self.graph is None:
                self.launch()
                self.stream.synchronize()
                self.graph = t.npu.NPUGraph()
                with t.npu.graph(
                    self.graph, stream=self.stream, auto_dispatch_capture=True
                ):
                    self.launch()
            self.graph.replay()
            self.done.record()
        self.done.synchronize()

    def verify(self) -> None:
        """D reads through its own mapping immediately after writers acknowledge."""
        t = self.d.torch
        for reader, output, expected in self.readbacks:
            reader.launch()
            t.npu.current_stream().synchronize()
            if not t.equal(output.cpu(), expected):
                raise AssertionError(
                    "kernel event -> D immediate mapped readback differs"
                )
