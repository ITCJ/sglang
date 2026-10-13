"""Device fork/join and production materialization for the sharing experiment."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from ascend_sparse.fixture import allocate_cache


class Materialization:
    """Read-only fixture at the production cache manager's materialization seam."""

    def __init__(self, work: Any, torch: Any) -> None:
        self.work, self.torch = work, torch
        layout = work.pool.layout
        self.cache = allocate_cache(
            rows=work.batch + 1,
            context=layout.prompt.tokens + layout.decode.tokens,
            topk=work.topk,
            layers=layout.prompt.layers,
            heads=1,
            dim=layout.prompt.dim,
            device=work.device,
        )
        self.batch = SimpleNamespace(
            req_pool_indices=torch.arange(
                1, work.batch + 1, dtype=torch.int64, device=work.device
            ),
            seq_lens=torch.full(
                (work.batch,),
                layout.prompt.tokens + layout.decode.tokens,
                dtype=torch.int64,
                device=work.device,
            ),
        )
        self.runtime = SimpleNamespace(
            selected_kv_valid=self.valid, fetch_selected_kv=self.fetch
        )
        self.seed_map = torch.full_like(self.cache.device_slot_map[work.layer], -1)
        self.seed_values = torch.zeros_like(self.cache.device_kv_buffer[work.layer])
        self.update_seed()

    def valid(self, layer: int, rows: Any, positions: Any) -> Any:
        work = self.work
        return (
            work.inputs.active.unsqueeze(1)
            & (positions >= 0)
            & (
                positions
                < work.pool.layout.prompt.tokens + work.pool.layout.decode.tokens
            )
        )

    def fetch(
        self, layer: int, rows: Any, positions: Any, misses: Any, output: Any
    ) -> None:
        # Same production route builder and P-then-D kernel order as KVFetch.
        self.work.inputs.valid.copy_(misses)
        self.work.copy.gather()

    def update_seed(self) -> None:
        work, torch = self.work, self.torch
        self.seed_map.fill_(-1)
        self.seed_values.zero_()
        hits = work.topk * work.case.hit_percent // 100
        for row in range(work.case.active if work.enabled and not work.empty else 0):
            self.seed_values[row + 1].copy_(work.expected[row].to(work.device))
            selected = work.host_positions[row, :hits].to(work.device)
            self.seed_map[row + 1, selected] = torch.arange(
                hits, dtype=torch.int32, device=work.device
            )
        work.read_payload = (
            (work.case.active if work.enabled and not work.empty else 0)
            * (work.topk - hits)
            * work.pool.layout.prompt.row_bytes
        )

    def prepare(self) -> None:
        """Restore the requested hit rate before EVERY timed call, including replay."""
        self.cache.device_slot_map[self.work.layer].copy_(self.seed_map)
        self.cache.device_kv_buffer[self.work.layer].copy_(self.seed_values)

    def launch(self, stream: Any) -> None:
        self.work.output.zero_()
        self.cache.materialize_selected_kv(
            SimpleNamespace(layer_id=self.work.layer),
            self.batch,
            self.work.inputs.positions,
            self.work.output,
            stream,
            mempool_runtime=self.runtime,
        )

    def verify_cache(self) -> None:
        work, torch = self.work, self.torch
        cache = self.cache.device_kv_buffer[work.layer].cpu()
        for row in range(work.case.active if work.enabled and not work.empty else 0):
            if not torch.equal(cache[row + 1], work.expected[row]):
                raise AssertionError("production cache refill differs from selected KV")


class Pipeline:
    """A bounded component DAG; writer completion is separate from selected-ready."""

    def __init__(
        self,
        work: Any,
        torch: Any,
        *,
        delay_matmuls: int = 0,
        publication: bool = False,
        publication_timing: bool = False,
        measure_selected: bool = False,
    ) -> None:
        self.work, self.torch = work, torch
        self.main = torch.npu.Stream()
        # Allocate only live branches. The materialization fixture already owns
        # four streams; unused writer/current branches can otherwise wrap an
        # eight-stream NPU pool and alias its slot-map stream with main.
        self.writer = (
            torch.npu.Stream()
            if work.case.operation
            in ("current_serial", "current_parallel", "write_with_history")
            else self.main
        )
        self.history = (
            torch.npu.Stream()
            if work.case.operation == "current_parallel"
            else self.main
        )
        self.current = (
            torch.npu.Stream()
            if work.case.operation == "current_parallel"
            else self.main
        )
        self.ready = torch.npu.Event(enable_timing=publication_timing)
        self.writer_done = torch.npu.Event(enable_timing=publication_timing)
        self.history_done = torch.npu.Event()
        self.current_done = torch.npu.Event()
        self.selected_ready = torch.npu.Event(enable_timing=publication_timing)
        self.observer_graph = None
        self.selected_signal = None
        if measure_selected and work.case.operation in (
            "current_serial",
            "current_parallel",
            "materialize",
            "write_then_read",
            "write_with_history",
        ):
            if not hasattr(torch.npu, "ExternalEvent"):
                raise RuntimeError(
                    "selected-ready Graph measurement requires torch.npu.ExternalEvent"
                )
            self.selected_signal = torch.npu.ExternalEvent()
        self.observer = (
            torch.npu.Stream() if self.selected_signal is not None else self.main
        )
        self.delay_matmuls, self.publication = delay_matmuls, publication
        self.materialization = (
            Materialization(work, torch)
            if work.case.operation == "materialize"
            else None
        )
        self.collective_value = torch.zeros(1, device=work.device)
        self.delay_source = (
            torch.eye(1024, dtype=torch.float16, device=work.device)
            if delay_matmuls
            else None
        )
        self.delay_target = (
            torch.empty_like(self.delay_source) if delay_matmuls else None
        )
        self.published = torch.empty_like(work.current_values)
        self.publish_src = torch.zeros(
            work.batch, dtype=torch.int64, device=work.device
        )
        self.publish_dst = torch.arange(
            work.batch, dtype=torch.int64, device=work.device
        )
        self.publish_valid = torch.zeros(
            work.batch, dtype=torch.bool, device=work.device
        )
        self.main.wait_stream(torch.npu.current_stream())
        if self.selected_signal is not None:
            # Capture the wait ONCE as well as the main graph's record. Repeated
            # host waits violate ExternalEvent's one-wait-per-record bookkeeping
            # when only the captured record is replayed (value-wait backend).
            # Seed and consume once so capture starts with an initialized event.
            self.main.record_event(self.selected_signal)
            self.main.synchronize()
            self.observer_graph = torch.npu.NPUGraph()
            with torch.npu.graph(
                self.observer_graph, stream=self.observer, auto_dispatch_capture=True
            ):
                self.observer.wait_event(self.selected_signal)
            with torch.npu.stream(self.observer):
                self.observer_graph.replay()
            self.observer.synchronize()

    def run(self, invoke: Any, timers: Any = None) -> None:
        """Pair every actual invocation with signal consumption, including warmup."""
        if not self.work.side_active:
            return
        torch = self.torch
        self.prepare()
        start, selected, end = timers if timers is not None else (None, None, None)
        if self.observer_graph is not None:
            # Fresh outside-graph Events carry timestamps for THIS replay. The
            # start fence also orders reset (legacy backend) before the wait.
            start = start if start is not None else torch.npu.Event()
            selected = selected if selected is not None else torch.npu.Event()
        if start is not None:
            self.main.record_event(start)
        # HCCL caches a stream from the same pool as these Python streams. After
        # enough cases the observer can alias that cached communication stream.
        # Submit the producer/collective before the observer's external wait;
        # otherwise that wait blocks HCCL, while main waits for HCCL before it
        # can record the signal. This also applies to replayed producer graphs.
        invoke()
        if end is not None:
            self.main.record_event(end)
        if self.observer_graph is not None:
            with torch.npu.stream(self.observer):
                self.observer.wait_event(start)
                self.observer_graph.replay()
                self.observer.record_event(selected)
        if self.observer_graph is not None:
            # Do not reset/re-record until the previous wait consumed its signal.
            self.main.wait_event(selected)

    def prepare(self) -> None:
        if self.selected_signal is not None:
            self.selected_signal.reset(self.main)
        if self.materialization is not None:
            self.materialization.prepare()

    def selected(self) -> None:
        self.main.record_event(self.selected_ready)
        if self.selected_signal is not None:
            self.main.record_event(self.selected_signal)

    def update(self) -> None:
        work = self.work
        local = work.pool.layout.layout_for_rank(work.pool.rank)
        self.publish_src.copy_(
            work.host_slots[work.pool.rank] * local.tokens + work.first_write
        )
        self.publish_valid.copy_(work.inputs.active)
        if self.materialization is not None:
            self.materialization.update_seed()

    def launch(self) -> None:
        torch, work, stream = self.torch, self.work, self.main
        op = work.case.operation
        if self.materialization is not None:
            self.materialization.launch(stream)
            for event in (
                self.materialization.cache.hit_done,
                self.materialization.cache.miss_done,
            ):
                stream.wait_event(event)
            self.selected()
            for event in (
                self.materialization.cache.refill_done,
                self.materialization.cache.slot_map_done,
            ):
                stream.wait_event(event)
            return
        if op in ("logical", "equal_bytes"):
            work.write()
            self.selected()
            return
        if op == "read":
            work.history()
            self.selected()
            return

        # No current copy or write completion precedes this common ready event.
        work.output.zero_()
        stream.record_event(self.ready)
        async_write = op in ("write_with_history", "current_serial", "current_parallel")
        # Only the synthetic publication fixture defers writer submission. Its
        # hundreds of eager mm calls can consume their own device delay while the
        # host is still submitting, starving selected of any chance to run.
        # Submit the independent selected branch first; no device dependency is
        # added from selected to writer, and the benchmark order stays unchanged.
        defer_writer = async_write and self.publication and self.delay_matmuls > 0
        if async_write:
            if not defer_writer:
                self.submit_writer()
        else:
            work.write()
            if op == "write_then_read":
                # A complete current row can span both writers. Use the same
                # pair collective in BOTH pool modes before reading it from BM.
                torch.distributed.all_reduce(self.collective_value)
        if op == "current_parallel":
            with torch.npu.stream(self.history):
                self.history.wait_event(self.ready)
                work.history()
                self.history.record_event(self.history_done)
            with torch.npu.stream(self.current):
                self.current.wait_event(self.ready)
                work.current()
                self.current.record_event(self.current_done)
            stream.wait_event(self.history_done)
            stream.wait_event(self.current_done)
        else:
            if work.has_current:
                work.current()
            work.history()
        self.selected()
        if defer_writer:
            self.submit_writer()
        if async_write:
            # Teardown/next source use needs the writer, selected-ready does not.
            stream.wait_event(self.writer_done)
        if self.publication:
            # Component equivalent of the existing TP publication edge, not a
            # claim about service hooks. Read back BEFORE any host rendezvous.
            torch.distributed.all_reduce(self.collective_value)
            local = work.pool.layout.layout_for_rank(work.pool.rank)
            work.kernel(
                work.dtype_placeholder,
                self.published,
                self.publish_src,
                self.publish_dst,
                self.publish_valid,
                local.slots * local.tokens,
                work.batch,
                local.row_bytes,
                work.batch,
                work.block_dim,
                work.pool.view(work.pool.rank, work.layer).device_base,
                None,
            )

    def submit_writer(self) -> None:
        torch, work = self.torch, self.work
        with torch.npu.stream(self.writer):
            self.writer.wait_event(self.ready)
            if work.pool.member == 1:
                for _ in range(self.delay_matmuls):
                    torch.mm(
                        self.delay_source, self.delay_source, out=self.delay_target
                    )
            work.write()
            self.writer.record_event(self.writer_done)

    def verify(self) -> None:
        self.work.verify_selected()
        if self.materialization is not None:
            self.materialization.verify_cache()
        if self.publication:
            active = self.work.case.active
            if not self.torch.equal(
                self.published[:active].cpu(), self.work.current_values[:active].cpu()
            ):
                raise AssertionError(
                    "TP publication did not protect complete shared rows"
                )
