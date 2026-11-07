"""Real BM eager swap-in; no model service, Graph capture or trace exporter."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

from verify_graph import load_runtime
from bandwidth_worker import binary_identity, save
from ascend_mempool.swapin_experiment import ITERATIONS, TOPK, pool_plan, selection
from ascend_mempool.sharing_control import Cohort
from ascend_mempool.manager import MempoolKVManager


def baseline_module(root: Path, name: str):
    qualified = "ascend_mempool._swapin_baseline_" + name
    spec = importlib.util.spec_from_file_location(qualified, root / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = module
    spec.loader.exec_module(module)
    return module


class Timing:
    """Event brackets, not profiler kernel-exclusive timings.

    Eager event intervals may include host dispatch gaps. Metadata spans from
    the previous copy-end (or stream-begin) to the next copy-start, without
    synchronizing between metadata and copy. Cross-stream sums are not total.
    """

    def __init__(self, torch):
        self.t = torch
        self.cursor = {}
        self.meta = []
        self.kernels = []
        self.host_ms = 0.0

    def event(self):
        e = self.t.npu.Event(enable_timing=True)
        e.record()
        return e

    def begin(self, name):
        self.cursor[name] = (self.event(), time.monotonic_ns())

    def call(self, stream, name, fn, *args, **kwargs):
        host_end = time.monotonic_ns()
        start = self.event()
        previous, host_start = self.cursor[stream]
        self.meta.append((previous, start))
        self.host_ms += (host_end - host_start) / 1e6
        fn(*args, **kwargs)
        end = self.event()
        self.kernels.append((name, start, end))
        self.cursor[stream] = (end, time.monotonic_ns())

    def finish(self, name):
        host_end = time.monotonic_ns()
        end = self.event()
        previous, host_start = self.cursor.pop(name)
        self.meta.append((previous, end))
        self.host_ms += (host_end - host_start) / 1e6
        return end

    def result(self, origin):
        starts = [origin.elapsed_time(a) for _, a, _ in self.kernels]
        ends = [origin.elapsed_time(b) for _, _, b in self.kernels]
        kernels = {name: a.elapsed_time(b) for name, a, b in self.kernels}
        return dict(
            metadata_host_ms=self.host_ms,
            metadata_device_span_ms=sum(a.elapsed_time(b) for a, b in self.meta),
            kernels_ms=kernels,
            kernel_sum_ms=sum(kernels.values()),
            copy_envelope_ms=max(ends) - min(starts),
        )


def pattern(t, rows, marker, device="npu"):
    return (
        (t.arange(rows, device=device)[:, None] % 127)
        + (t.arange(576, device=device)[None, :] % 7)
        + marker
    ).to(t.bfloat16)


def initialize_pool(t, pool, side, cohort):
    """Real registered BM, 78-layer stride, reversed pages within each slot."""
    from ascend_mempool.indexed_copy import copy_rows

    pool.enable_page_tables("npu")
    for owner in (0, 1):
        view = pool.view(owner, 0)
        pages = view.layout.tokens // 64
        table = pool.page_tables[owner, "alive_main"]
        for slot in range(16):
            table[slot, :pages].copy_(
                t.arange((slot + 1) * pages - 1, slot * pages - 1, -1, device="npu")
            )
    if pool.session.tp_rank == pool.session.region_members(side)[0]:
        view = pool.view(side, 0)
        tokens = view.layout.tokens
        values = pattern(t, tokens, 10 if side == 0 else 30)
        indices = t.arange(tokens, device="npu")
        for slot in range(16):
            copy_rows(
                t.ops.npu.unidex_copy,
                values,
                values,
                indices,
                slot * view.token_capacity + indices,
                t.ones(tokens, device="npu", dtype=t.bool),
                row_bytes=1152,
                target_view=view,
            )
        t.npu.current_stream().synchronize()
    cohort.exchange("source-kv-initialized")


class Work:
    def __init__(self, t, pool, baseline, batch):
        from ascend_mempool.copy import KVFetch, SparseKVCopy, SparseCopyInputs
        from ascend_mempool.runtime import MempoolRuntime
        from ascend_mempool.decode_write import DecodeKV, DecodeWriter
        from ascend_mempool.multibase import load_kernel
        from ascend_sparse.manager import _build_hit_src_dst_index
        from sgl_kernel_npu.sparsity_driven_kv_offload import (
            unidex_copy_inplace,
            slot_map_lookup,
        )

        self.t, self.pool, self.batch = t, pool, batch
        self.hit_indices = _build_hit_src_dst_index
        self.hit_copy = unidex_copy_inplace
        self.lookup = slot_map_lookup
        self.native = t.ops.npu.unidex_copy
        self.fused = load_kernel()
        self.main = t.npu.current_stream()
        self.hit, self.miss = t.npu.Stream(), t.npu.Stream()
        self.cache = pattern(t, TOPK, 50)[None].expand(17, -1, -1).clone().unsqueeze(2)
        self.current = DecodeKV(pool, DecodeWriter(0, 1), 17, "npu")
        self.current.current[0].copy_(pattern(t, 17, 70)[:, None, :])
        self.outputs = {
            v: t.empty((batch, TOPK, 1, 576), dtype=t.bfloat16, device="npu")
            for v in ("baseline", "materialize")
        }
        self.runtimes = {}
        self.setup = {}
        for variant, runtime_type, copy_module in (
            ("baseline", baseline["runtime"].MempoolRuntime, baseline["copy"]),
            (
                "materialize",
                MempoolRuntime,
                SimpleNamespace(
                    KVFetch=KVFetch,
                    SparseKVCopy=SparseKVCopy,
                    SparseCopyInputs=SparseCopyInputs,
                ),
            ),
        ):
            t.npu.synchronize()
            start = time.monotonic_ns()
            rt = runtime_type.__new__(runtime_type)
            rt.start_layer, rt.layout, rt.manager = (
                0,
                pool.layout.layout_for_rank(pool.rank),
                pool,
            )
            rt.row_slot = t.arange(17, device="npu") - 1
            rt.row_prompt_slot = rt.row_slot.clone()
            rt.row_prompt_len = t.full((17,), 8192, device="npu", dtype=t.int64)
            rt.row_decode_len = t.full((17,), 512, device="npu", dtype=t.int64)
            rt._decode = self.current
            rt.observation_device = rt._fetch_sources = None
            rt._fetch = copy_module.KVFetch(
                pool, 48, self.fused if variant == "materialize" else self.native
            )
            inputs = copy_module.SparseCopyInputs(batch, TOPK, "npu")
            extra = (
                dict(extra_sources=(self.cache, self.current.current[0]))
                if variant == "materialize"
                else {}
            )
            rt._fetch._copies[0, batch, TOPK] = copy_module.SparseKVCopy(
                pool.view(0, 0),
                pool.view(1, 0),
                inputs,
                output=self.outputs[variant],
                kernel=rt._fetch.kernel,
                **extra,
            )
            self.runtimes[variant] = rt
            t.npu.synchronize()
            self.setup[variant] = dict(
                metadata_init_ms=(time.monotonic_ns() - start) / 1e6
            )

    def raw(self, iteration):
        t = self.t
        data = selection(self.batch, iteration)
        raw = {
            k: t.tensor(data[k], device="npu") for k in ("positions", "hits", "slots")
        }
        raw["rows"] = t.arange(1, self.batch + 1, device="npu")
        raw["slot_map"] = t.full((17, 8832), -1, dtype=t.int32, device="npu")
        for request in range(self.batch):
            hit = raw["hits"][request]
            raw["slot_map"][request + 1, raw["positions"][request][hit]] = raw["slots"][
                request
            ][hit].int()
        # A fixed source payload makes metadata/address mistakes observable.
        markers = dict(HBM_cache=50, P_alive=10, D_alive=30, HBM_current=70)
        values = [
            [markers[s] + i % 127 for s, i in zip(ss, ii)]
            for ss, ii in zip(data["sources"], data["indices"])
        ]
        expected = (t.tensor(values)[:, :, None] + t.arange(576)[None, None, :] % 7).to(
            t.bfloat16
        )
        t.npu.synchronize()
        return raw, expected, data["counts"]

    def run(self, variant, raw):
        t, rt = self.t, self.runtimes[variant]
        output = self.outputs[variant]
        output.fill_(-99)
        t.npu.synchronize()
        timer = Timing(t)
        rt._active = SimpleNamespace(
            replay=False, layers={0}, selection_layers={0}, fetch_layers=set()
        )
        rt._decode.kernel = lambda *a: timer.call("miss", "current", self.native, *a)
        copier = rt._fetch._copies[0, self.batch, TOPK]
        if variant == "materialize":
            copier.transfer.kernel = lambda *a: timer.call(
                "miss", "multibase", self.fused, *a
            )
        else:
            names = iter(("P_alive", "D_alive"))
            copier._kernel = lambda *a: timer.call("miss", next(names), self.native, *a)
        start = time.monotonic_ns()
        timer.begin("main")
        origin = timer.cursor["main"][0]
        # Rebuild selected-copy metadata each iteration from upstream selection.
        valid = (raw["positions"] >= 0) & (raw["positions"] < 8704)
        lookup_rows = raw["rows"].to(dtype=t.int32).contiguous()
        lookup_positions = (
            t.where(valid, raw["positions"], -1).to(dtype=t.int32).contiguous()
        )
        token_on_device, device_token_pos = self.lookup(
            raw["slot_map"], lookup_rows, lookup_positions
        )
        token_on_device = token_on_device.to(t.bool) & valid
        hit_src, hit_dst, hit_valid = self.hit_indices(
            token_on_device, device_token_pos, raw["rows"], TOPK
        )
        misses = (~token_on_device) & (raw["positions"] >= 0)
        ready = timer.finish("main")
        if variant == "baseline":
            with t.npu.stream(self.hit):
                self.hit.wait_event(ready)
                timer.begin("hit")
                timer.call(
                    "hit",
                    "HBM_cache",
                    self.hit_copy,
                    self.cache,
                    output,
                    hit_src,
                    hit_dst,
                    hit_valid,
                    2,
                    2,
                    block_dim=24,
                )
                hit_done = timer.finish("hit")
        with t.npu.stream(self.miss):
            self.miss.wait_event(ready)
            timer.begin("miss")
            extra = (
                dict(cache_buffer=self.cache, cache_rows=hit_src, cache_valid=hit_valid)
                if variant == "materialize"
                else {}
            )
            rt.fetch_selected_kv(
                0, raw["rows"], raw["positions"], misses, output, **extra
            )
            miss_done = timer.finish("miss")
        if variant == "baseline":
            self.main.wait_event(hit_done)
        self.main.wait_event(miss_done)
        done = timer.event()
        done.synchronize()
        end = time.monotonic_ns()
        expected_names = (
            ["HBM_cache", "current", "P_alive", "D_alive"]
            if variant == "baseline"
            else ["multibase"]
        )
        if [name for name, _, _ in timer.kernels] != expected_names:
            raise AssertionError("unexpected materialization copy launch count/order")
        return dict(
            copy_launches=len(timer.kernels),
            start_ns=start,
            end_ns=end,
            total_ms=(end - start) / 1e6,
            **timer.result(origin),
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text())
    root = Path(cfg["output"])
    root.mkdir(parents=True)
    try:
        t, mf, env = load_runtime(SimpleNamespace(device_id=cfg["rank"]))
        env["sdk_binaries"] = binary_identity("memfabric_hybrid")
        save(root / "environment.json", env)
        side, rank, tp = cfg["side"], cfg["rank"], cfg["tp"]
        plan = pool_plan(tp, 16, side, cfg["hosts"][side], cfg["port"], cfg["run_id"])
        pool = MempoolKVManager.initialize_session(
            session=plan.session(rank),
            store_host=cfg["hosts"][0],
            nic_host=cfg["hosts"][side],
            device_id=rank,
            timeout=cfg["timeout"],
            bm_module=mf.bm,
        )
        cohort = Cohort(
            cfg["hosts"][0],
            cfg["port"] + 1,
            side * tp + rank,
            cfg["run_id"],
            cfg["timeout"],
            tp,
        )
        save(
            root / "mapping.json",
            dict(
                regions=pool.replica_registration(),
                requested_numa=pool.session.numa_node,
            ),
        )
        initialize_pool(t, pool, side, cohort)
        baseline = {
            name: baseline_module(Path(cfg["baseline_dir"]), name)
            for name in ("copy", "runtime")
        }
        samples, setups = [], {}
        for batch in (1, 16):
            work = Work(t, pool, baseline, batch) if side else None
            if work:
                setups[str(batch)] = work.setup
            for variant in ("baseline", "materialize"):
                for iteration in range(ITERATIONS):
                    if work:
                        raw, expected, counts = work.raw(iteration)
                    cohort.aligned_start(f"b{batch}/{variant}/{iteration}")
                    if work:
                        sample = work.run(variant, raw)
                        actual = work.outputs[variant].cpu().squeeze(2)
                        if not t.equal(actual, expected):
                            raise AssertionError(
                                f"KV mismatch: b{batch}/{variant}/{iteration}"
                            )
                        samples.append(
                            dict(
                                batch=batch,
                                variant=variant,
                                iteration=iteration,
                                counts=counts,
                                **sample,
                            )
                        )
                    cohort.exchange(f"b{batch}/{variant}/{iteration}/checked")
                print(f"PASS b{batch}/{variant}: 100 iterations", flush=True)
            if work:
                del work
        save(
            root / "result.json",
            dict(status="passed", rank=rank, side=side, setup=setups, samples=samples),
        )
        cohort.exchange("all-measured")
        (root / "ready").touch()
        while True:
            time.sleep(1)  # Existing launcher owns SDK/process cleanup.
    except Exception:
        save(root / "failure.json", dict(error=traceback.format_exc()))
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
