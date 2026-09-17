#!/usr/bin/env python3
"""Single-case Ascend HiCache benchmark. Run through run.py for timeout/logging."""
import argparse
import csv
import json
import math
import statistics
import subprocess
import tempfile
import time
from array import array
from importlib import metadata
from pathlib import Path

PAGE_SIZE = 128
LAYERS = 61
K_DIM = 512
ROPE_DIM = 64
BYTES_PER_TOKEN = LAYERS * (K_DIM + ROPE_DIM) * 2


def page_order(count, scatter=False):
    """Logical-page order used by both Host and device pools."""
    if count < 1:
        raise ValueError('page count must be positive')
    return list(range(count))


def page_slots(count, scatter=False, reserved_zero=True):
    """Physical page slots; scattered pages are separated by page-sized gaps."""
    order = page_order(count, scatter)
    base = 1 if reserved_zero else 0
    step = 2 if scatter else 1
    return [base + step * page for page in order]


def summarize(samples, nbytes):
    result = {}
    for key in samples[0]:
        values = [s[key] for s in samples]
        result[key] = dict(median_ms=statistics.median(values) * 1000,
                           p95_ms=sorted(values)[math.ceil(.95 * len(values)) - 1] * 1000)
    result['effective_GBps'] = nbytes / statistics.median(
        s['total_s'] for s in samples) / 1e9
    return result


def run(args):
    import torch
    import torch_npu  # noqa: F401
    from sglang.srt.server_args import ServerArgs, set_global_server_args_for_scheduler
    from sglang.srt.hardware_backend.npu.memory_pool_npu import NPUMLATokenToKVPool
    from sglang.srt.mem_cache.allocator.paged import PagedTokenToKVPoolAllocator
    from sglang.srt.mem_cache.cache_init_params import CacheInitParams
    from sglang.srt.mem_cache.hiradix_cache import HiRadixCache
    from sglang.srt.mem_cache.radix_cache import RadixKey
    from sglang.srt.mem_cache.base_prefix_cache import MatchPrefixParams, InitLoadBackParams

    torch.npu.set_device(args.device)
    # No TCP rendezvous, model weights, Store, or multi-rank communication.
    rendezvous = tempfile.TemporaryDirectory(prefix='hicache-l2-')
    torch.distributed.init_process_group('gloo', init_method=(
        Path(rendezvous.name) / 'rendezvous').as_uri(), rank=0, world_size=1)
    cache = None
    try:
        server_args = ServerArgs(
            model_path='dummy', device='npu',
            page_size=PAGE_SIZE, enable_hierarchical_cache=True,
            hicache_io_backend='kernel_ascend', hicache_mem_layout='page_first_kv_split',
            hicache_write_policy='write_back', hicache_storage_backend=None,
            hicache_ratio=1.0, hicache_size=0,
        )
        set_global_server_args_for_scheduler(server_args)
        physical_tokens = args.tokens * (2 if args.scatter else 1)
        pool = NPUMLATokenToKVPool(
            size=physical_tokens, page_size=PAGE_SIZE, dtype=torch.bfloat16,
            kv_lora_rank=K_DIM, qk_rope_head_dim=ROPE_DIM, layer_num=LAYERS,
            device='npu', enable_memory_saver=False,
        )
        allocator = PagedTokenToKVPoolAllocator(
            size=physical_tokens, page_size=PAGE_SIZE, dtype=torch.bfloat16,
            device='npu', kvcache=pool, need_sort=False,
        )
        cache = HiRadixCache(CacheInitParams(
            disable=False, req_to_token_pool=None,
            token_to_kv_pool_allocator=allocator, page_size=PAGE_SIZE,
            tp_cache_group=torch.distributed.group.WORLD,
        ), server_args)
        host = cache.token_to_kv_pool_host
        cc = cache.cache_controller
        assert not cache.enable_storage and not cc.enable_storage
        assert host.pin_memory and host.layout == 'page_first_kv_split'
        # Fail closed if the official allocator does not actually yield pinned memory.
        pinned = dict(k=host.k_buffer.is_pinned(), rope=host.v_buffer.is_pinned())
        if not all(pinned.values()):
            raise RuntimeError(f'official Host pool is not reported pinned: {pinned}')
        pages = args.tokens // PAGE_SIZE
        layout = 'scattered' if args.scatter else 'contiguous'
        l1_slots = page_slots(pages, args.scatter, reserved_zero=True)
        l2_slots = page_slots(pages, args.scatter, reserved_zero=False)
        free_page_order = torch.tensor(l1_slots, dtype=allocator.free_pages.dtype,
                                       device=allocator.free_pages.device)
        host_free_order = torch.tensor(
            [slot * PAGE_SIZE + token for slot in l2_slots for token in range(PAGE_SIZE)],
            dtype=host.free_slots.dtype)
        key = RadixKey(array('q', range(args.tokens)))
        host_indices = None
        initialized_slots = None

        def prepare():
            nonlocal host_indices, initialized_slots
            torch.npu.synchronize()
            if cache.ongoing_load_back or cc.ack_load_queue:
                raise RuntimeError('previous load was not fully acknowledged')
            cache.reset()
            allocator.clear()
            # Fixture only: both direct and managed use the real allocator.alloc().
            # Set the same free-page order before either path, outside timing.
            allocator.free_pages = free_page_order.clone()
            host.free_slots = host_free_order.clone()
            host_indices = host.alloc(args.tokens)
            if host_indices is None:
                raise RuntimeError('Host allocation failed')
            slots = host_indices[::PAGE_SIZE].tolist()
            if initialized_slots is not None and slots != initialized_slots:
                raise RuntimeError('Host allocator reset changed the fixture mapping')
            if initialized_slots is None:
                # Deterministic page/layer/token/component-dependent bytes, bounded scratch.
                if args.validate:
                    host.k_buffer.zero_()
                    host.v_buffer.zero_()
                for page, slot in enumerate(host_indices[::PAGE_SIZE].tolist()):
                    hp = slot // PAGE_SIZE
                    for buf, dim, salt in ((host.k_buffer, K_DIM, 0),
                                           (host.v_buffer, ROPE_DIM, 91)):
                        layer = torch.arange(LAYERS).view(-1, 1, 1, 1)
                        token = torch.arange(PAGE_SIZE).view(1, -1, 1, 1)
                        values = ((page * 11 + layer * 7 + token * 3 + salt) % 251 + 1)
                        buf[hp].copy_(values.expand(LAYERS, PAGE_SIZE, 1, dim))
                initialized_slots = slots
            pool.k_buffer.zero_()
            pool.v_buffer.zero_()
            # One known full-length Host-only prefix; no eviction pressure.
            cache._insert_helper_host(cache.root_node, key, host_indices,
                                      [f'fixture-{p}' for p in range(pages)])
            torch.npu.synchronize()

        def validate(device_indices):
            slots = device_indices.cpu()[::PAGE_SIZE].tolist()
            if len(slots) != pages or len(set(slots)) != pages:
                raise RuntimeError('incorrect destination page coverage')
            for hs, ds in zip(host_indices[::PAGE_SIZE].tolist(), slots):
                for h, d in ((host.k_buffer, pool.k_buffer),
                             (host.v_buffer, pool.v_buffer)):
                    actual = d[:, ds // PAGE_SIZE].cpu().contiguous()
                    expected = h[hs // PAGE_SIZE].contiguous()
                    if not torch.equal(actual.view(torch.uint8), expected.view(torch.uint8)):
                        raise RuntimeError(f'KV mismatch host={hs} device={ds}')
            if torch.count_nonzero(pool.k_buffer[:, 0]).item() or torch.count_nonzero(pool.v_buffer[:, 0]).item():
                raise RuntimeError('reserved NPU page overwritten')
            if args.scatter and pages > 1:
                l1_guards = torch.tensor(list(range(2, pages * 2 + 1, 2)),
                                         dtype=torch.int64, device='npu')
                if (torch.count_nonzero(pool.k_buffer[:, l1_guards]).item()
                        or torch.count_nonzero(pool.v_buffer[:, l1_guards]).item()):
                    raise RuntimeError('unused NPU guard page overwritten')
                l2_guards = list(range(1, pages * 2, 2))
                if (torch.count_nonzero(host.k_buffer[l2_guards]).item()
                        or torch.count_nonzero(host.v_buffer[l2_guards]).item()):
                    raise RuntimeError('unused Host guard page overwritten')

        def direct():
            # Allocation and CPU index preparation are outside pure-copy timing.
            indices = allocator.alloc(args.tokens)
            if indices is None:
                raise RuntimeError('NPU allocation failed')
            cpu_indices = indices.cpu()
            torch.npu.synchronize()
            t0 = time.perf_counter()
            # One task submits all its pages; the kernel handles page iteration.
            host.load_to_device_per_layer(
                pool, host_indices, cpu_indices, 0, 'kernel_ascend')
            torch.npu.synchronize()
            t1 = time.perf_counter()
            return indices, dict(total_s=t1-t0)

        def managed():
            t0 = time.perf_counter()
            match = cache.match_prefix(MatchPrefixParams(key=key))
            t1 = time.perf_counter()
            if match.host_hit_length != args.tokens or match.device_indices.numel():
                raise RuntimeError('fixture must be a full Host-only hit')
            indices, _ = cache.init_load_back(InitLoadBackParams(
                best_match_node=match.best_match_node, host_hit_length=match.host_hit_length))
            t2 = time.perf_counter()
            if indices.numel() != args.tokens:
                raise RuntimeError('load-back did not allocate the entire prefix')
            consumer = cache.ready_to_load_host_cache()
            t3 = time.perf_counter()
            if consumer < 0 or len(cc.ack_load_queue) != 1:
                raise RuntimeError('expected exactly one load acknowledgement')
            ack = cc.ack_load_queue[0]
            ack.finish_event.synchronize()
            t4 = time.perf_counter()
            # Run real readiness checking + reference-count/ack maintenance.
            cache.loading_check()
            t5 = time.perf_counter()
            if cache.ongoing_load_back or cc.ack_load_queue:
                raise RuntimeError('load acknowledgement was not drained')
            sample = dict(total_s=t5-t0, match_s=t1-t0, load_back_s=t2-t1,
                          submit_s=t3-t2, wait_s=t4-t3, acknowledge_s=t5-t4,
                          submit_to_complete_s=t4-t2)
            if ack.timing_enabled:
                # Includes controller per-layer event recording, NOT pure DMA alone.
                sample['controller_stream_s'] = ack.start_event.elapsed_time(ack.finish_event) / 1000
            return indices, sample

        actions = {'copy_whole': direct, 'hicache_load': managed}
        samples = {name: [] for name in actions}
        versions = {}
        for pkg in ('torch', 'torch-npu', 'sglang', 'sgl-kernel-npu'):
            try:
                versions[pkg] = metadata.version(pkg)
            except metadata.PackageNotFoundError:
                versions[pkg] = 'unknown'
        result = dict(status='running', tokens=args.tokens, layout=layout,
                      l1_slots=l1_slots, l2_slots=l2_slots,
                      physical_tokens=physical_tokens,
                      mapping_version='page_gap_v2',
                      bytes=args.tokens * BYTES_PER_TOKEN, warmup=args.warmup,
                      repeats=args.repeats, layers=LAYERS, page_size=PAGE_SIZE,
                      validation_enabled=args.validate, correct=None,
                      versions=versions, pinned=pinned, samples=samples,
                      device_name=torch.npu.get_device_name(args.device),
                      server_args_subset={name: getattr(server_args, name) for name in
                          ('device', 'hicache_io_backend', 'hicache_mem_layout',
                           'hicache_write_policy', 'hicache_storage_backend', 'hicache_ratio')},
                      host_bytes=sum(x.numel()*x.element_size() for x in (host.k_buffer,host.v_buffer)),
                      npu_bytes=sum(x.numel()*x.element_size() for x in (pool.k_buffer,pool.v_buffer)),
                      commit=subprocess.check_output(['git','rev-parse','HEAD'], cwd=Path(__file__).parent, text=True).strip(),
                      code_paths={'hiradix': __import__('inspect').getfile(HiRadixCache),
                                  'npu_pool': __import__('inspect').getfile(NPUMLATokenToKVPool)},
                      fixture='single full Host-only prefix; no eviction pressure; TP=1; no scheduler/model/L3')
        def save():
            args.output.write_text(json.dumps(result, indent=2) + '\n')
        save()
        for iteration in range(args.warmup + args.repeats):
            names = list(actions)
            offset = iteration % len(names)
            for name in names[offset:] + names[:offset]:
                prepare()
                indices, sample = actions[name]()
                # Cheap mapping check, not a KV readback; excluded from timing.
                actual_slots = (indices[::PAGE_SIZE] // PAGE_SIZE).cpu().tolist()
                if actual_slots != l1_slots:
                    raise RuntimeError(f'{name} allocated unexpected L1 page mapping')
                if args.validate:
                    validate(indices)
                if iteration >= args.warmup:
                    samples[name].append(sample)
                save()
        result['summary'] = {name: summarize(values, result['bytes'])
                             for name, values in samples.items()}
        result['correct'] = True if args.validate else None
        result['status'] = 'ok'
        save()
        with args.output.with_suffix('.csv').open('w') as f:
            writer = csv.writer(f)
            writer.writerow(('tokens','layout','path','metric','median_ms','p95_ms'))
            for name, stats in result['summary'].items():
                for metric, values in stats.items():
                    if isinstance(values, dict):
                        writer.writerow((args.tokens,layout,name,metric,values['median_ms'],values['p95_ms']))
        for name, stats in result['summary'].items():
            print(f"tokens={args.tokens} layout={layout} {name}={stats['total_s']['median_ms']:.3f} ms "
                  f"{stats['effective_GBps']:.3f} GB/s", flush=True)
        print('L2_OK', flush=True)
    finally:
        if cache is not None:
            cache.shutdown()
        torch.distributed.destroy_process_group()
        rendezvous.cleanup()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--tokens', type=int, default=128)
    p.add_argument('--scatter', action='store_true', help='use page-sized gaps in both L2 and L1 mappings')
    p.add_argument('--device', type=int, default=0)
    p.add_argument('--validate', action='store_true', help='validate all KV bytes after every iteration (default: off)')
    p.add_argument('--warmup', type=int, default=2)
    p.add_argument('--repeats', type=int, default=10)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    if args.tokens < 128 or args.tokens > 131072 or args.tokens % 128 or args.warmup < 0 or args.repeats < 1 or args.device < 0:
        p.error('invalid tokens/device/warmup/repeats')
    run(args)


if __name__ == '__main__':
    main()
