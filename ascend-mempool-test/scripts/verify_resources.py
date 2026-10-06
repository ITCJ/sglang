"""Ticket03 S3: verify actual NPU resource allocation and retained host paths.

Run one mode per process so SHM names and global managers cannot leak between
cases. No model, BM peer or transfer worker is started. The Ascend PD adapter
runs with only its parent transport boundary replaced by a recorder.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional, Sequence
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        required=True,
        choices=(
            "pd_prefill_mempool",
            "pd_decode_mempool",
            "local_offload",
            "pd_decode_offload",
        ),
    )
    parser.add_argument("--device-id", type=int, default=0)
    parser.add_argument("--report", type=Path, required=True)
    return parser.parse_args(argv)


def require_rejected(action: Callable[[], Any], message: str) -> None:
    try:
        action()
    except (RuntimeError, ValueError) as exc:
        if message not in str(exc):
            raise
    else:
        raise AssertionError(f"Expected rejection containing {message!r}")


def check_pd_adapter(cache: Any, pool: Any, req: Any, pages: Any) -> None:
    """Exercise the real adapter's construction, index routing and completion."""
    import numpy as np
    import torch
    from sglang.srt.disaggregation.ascend import conn

    # These buffers exercise only the adapter; the parent transport boundary
    # never registers or sends them in this resource gate.
    infos = (
        pool.get_contiguous_buf_infos()
        if cache.mode.uses_pd_decode_staging
        else pool.get_state_buf_infos()
    )
    args = SimpleNamespace(
        page_size=pool.page_size,
        kv_data_ptrs=infos[0],
        kv_data_lens=infos[1],
        kv_item_lens=infos[2],
    )
    with patch.object(conn.MooncakeKVManager, "__init__", return_value=None):
        adapter = conn.AscendKVManager(args, "decode", SimpleNamespace(), True)
    staging = adapter.sparse_pd_decode_staging
    assert (staging is not None) == cache.mode.uses_pd_decode_staging
    receiver = conn.AscendKVReceiver.__new__(conn.AscendKVReceiver)
    receiver.kv_mgr = adapter
    receiver.bootstrap_infos = object()
    receiver.bootstrap_room = req.bootstrap_room
    with patch.object(conn.MooncakeKVReceiver, "send_metadata", autospec=True) as send:
        receiver.send_metadata(pages, aux_index=7, decode_prefix_len=0)
        forwarded = send.call_args
        assert forwarded.args[2] == 7
        if staging is None:
            np.testing.assert_array_equal(forwarded.args[1], pages)
            assert forwarded.kwargs["device_kv_indices"] is None
        else:
            np.testing.assert_array_equal(forwarded.args[1], np.array([0], np.int32))
            np.testing.assert_array_equal(forwarded.kwargs["device_kv_indices"], pages)
            for k, v in zip(cache.pd_decode_k_staging, cache.pd_decode_v_staging):
                k[0, :2].fill_(11)
                v[0, :2].fill_(22)
            # Match transfer completion before the adapter's copy stream reads.
            torch.npu.synchronize()
    with patch.object(conn.MooncakeKVManager, "update_status", autospec=True) as status:
        adapter.update_status(req.bootstrap_room, conn.KVPoll.Success)
        status.assert_called_once_with(adapter, req.bootstrap_room, conn.KVPoll.Success)
    if staging is not None:
        assert staging.available_slots() == 1


def check_host_roundtrip(cache: Any, req: Any, loc: Any, torch: Any) -> None:
    """Original compact write -> host prefix read -> sparse miss -> HBM hit."""
    row = req.kv.req_pool_idx
    device = cache.device
    batch = SimpleNamespace(
        batch_size=1,
        req_pool_indices=torch.tensor([row], dtype=torch.int32, device=device),
        seq_lens=torch.tensor([3], dtype=torch.int32, device=device),
        out_cache_loc=loc[:1],
        forward_mode=SimpleNamespace(is_decode=lambda: True),
        extend_seq_lens=None,
        extend_prefix_lens=None,
        extend_seq_lens_cpu=None,
        global_num_token_non_padded_cpu=None,
    )
    k = torch.full((1, 1, 512), 33, dtype=torch.bfloat16, device=device)
    rope = torch.full((1, 1, 64), 44, dtype=torch.bfloat16, device=device)
    stream = torch.npu.current_stream()
    topk = torch.full((1, 2048), -1, dtype=torch.int32, device=device)
    topk[0, 0], topk[0, 1] = 0, 2
    for layer_idx in range(cache.layer_num):
        layer = cache.start_layer + layer_idx
        if not cache.mode.uses_pd_decode_staging:
            # LOCAL_OFFLOAD has no PD source, so seed its host prompt explicitly.
            cache.host_kv_buffer[layer_idx][row, :2, :, :512].fill_(11)
            cache.host_kv_buffer[layer_idx][row, :2, :, 512:].fill_(22)
        cache.offload_v2(k, rope, SimpleNamespace(layer_id=layer), batch, stream)
        actual_k, actual_rope = cache.get_forward_kv(layer, batch, stream)
        torch.npu.synchronize()
        torch.testing.assert_close(
            actual_k[:, 0, 0].cpu(), torch.tensor([11, 11, 33], dtype=k.dtype)
        )
        torch.testing.assert_close(
            actual_rope[:, 0, 0].cpu(), torch.tensor([22, 22, 44], dtype=k.dtype)
        )
        expected = torch.zeros((1, 2048, 1, 576), dtype=k.dtype)
        expected[0, 0, 0, :512], expected[0, 0, 0, 512:] = 11, 22
        expected[0, 1, 0, :512], expected[0, 1, 0, 512:] = 33, 44
        for _ in range(2):
            selected = torch.zeros_like(expected, device=device)
            cache.materialize_selected_kv(
                SimpleNamespace(layer_id=layer), batch, topk, selected, stream
            )
            torch.npu.synchronize()
            torch.testing.assert_close(selected.cpu(), expected, rtol=0, atol=0)
            # The second call must hit the HBM cache after the host source changes.
            cache.host_kv_buffer[layer_idx][row].fill_(-9)


def verify(args: argparse.Namespace) -> dict[str, Any]:
    import numpy as np
    import torch

    importlib.import_module("torch_npu")

    from sglang.srt.disaggregation.ascend.sparse_pd import SparsePDDecodeStagingPool
    from sglang.srt.hardware_backend.npu.memory_pool_npu import NPUMLATokenToKVPool
    from sglang.srt.hardware_backend.npu.sparsity_driven_kv_offload import (
        manager as sparse,
    )
    from sglang.srt.hardware_backend.npu.sparsity_driven_kv_offload.config import (
        SparseKVOffloadMode,
    )
    from sglang.srt.managers.schedule_batch import ReqKvInfo
    from sglang.srt.mem_cache.allocator import PagedTokenToKVPoolAllocator
    from sglang.srt.mem_cache.memory_pool import ReqToTokenPool

    torch.npu.set_device(args.device_id)
    device = f"npu:{args.device_id}"
    mode = SparseKVOffloadMode(args.mode)
    page_size, layers = 128, 2
    formal = mode in (
        SparseKVOffloadMode.PD_PREFILL_MEMPOOL,
        SparseKVOffloadMode.PD_DECODE_MEMPOOL,
    )
    pool = NPUMLATokenToKVPool(
        size=2 * page_size,
        page_size=page_size,
        dtype=torch.bfloat16,
        kv_lora_rank=512,
        qk_rope_head_dim=64,
        layer_num=layers,
        device=device,
        enable_memory_saver=False,
        index_head_dim=128,
        sparse_kv_offload_mode=mode,
    )
    allocator = PagedTokenToKVPoolAllocator(
        size=pool.size,
        page_size=page_size,
        dtype=torch.bfloat16,
        device=device,
        kvcache=pool,
        need_sort=False,
    )
    loc = allocator.alloc(page_size)
    assert loc is not None
    for layer in range(layers):
        index = pool.get_index_k_buffer(layer).view(-1, 1, 128)
        index.index_fill_(0, loc.long(), 17 + layer)
        assert bool((index[loc.long()].cpu() == 17 + layer).all())
    result = {
        "mode": mode.value,
        "index_k_bytes": sum(pool.get_state_buf_infos()[1]),
        "native_kv_bytes": sum(
            b.nbytes for b in (pool.k_buffer, pool.v_buffer) if b is not None
        ),
        "host_kv_bytes": 0,
        "host_mapping_count": 0,
        "host_metadata_bytes": 0,
        "main_kv_staging_bytes": 0,
        "sparse_cache_bytes": 0,
    }
    assert result["index_k_bytes"] > 0
    assert (result["native_kv_bytes"] == 0) == mode.uses_sparse_kv_cache
    if formal:
        assert pool.get_contiguous_buf_infos() == pool.get_state_buf_infos()
    if not mode.uses_sparse_kv_cache:
        allocator.free(loc)
        assert allocator.available_size() == pool.size
        torch.npu.synchronize()
        return result

    rows = ReqToTokenPool(
        2, max_context_len=16, device=device, enable_memory_saver=False
    )
    # Any hidden formal SHM allocation is an immediate failure, in addition to
    # measuring the retained buffers below. Ordinary modes use the real SDK.
    with patch.object(
        sparse,
        "create_shm_tensor",
        side_effect=AssertionError("formal mode allocated host SHM")
        if formal
        else sparse.create_shm_tensor,
    ):
        cache = sparse.SparseKVCacheManager(
            rows, allocator, sparse_context_len=2048, mode=mode
        )
    sparse.register_sparse_kv_manager(cache)
    req = SimpleNamespace(kv=ReqKvInfo(), origin_input_ids=[11, 22], bootstrap_room=42)
    assert rows.alloc([req]) is not None
    row = req.kv.req_pool_idx
    req.kv.kv_allocated_len = page_size
    cache.init_req(req)
    pages = np.asarray([int(loc[0].item()) // page_size], dtype=np.int32)
    if mode is not SparseKVOffloadMode.LOCAL_OFFLOAD:
        check_pd_adapter(cache, pool, req, pages)
    if formal:
        for entry in (cache.offload, cache.offload_v2):
            require_rejected(
                lambda: entry(None, None, None, None, None), "host KV is disabled"
            )
        require_rejected(
            lambda: cache.get_forward_kv(None, None), "host KV is disabled"
        )
        require_rejected(
            lambda: cache.ensure_pd_decode_staging_buffers(), "staging is disabled"
        )
        require_rejected(
            lambda: SparsePDDecodeStagingPool(cache, page_size), "staging is disabled"
        )
        assert not cache._pd_room_to_req_pool_idx
        assert not cache._pd_room_to_input_len
        assert not cache._pd_req_pool_idx_to_room
        assert cache._pd_decode_copy_stream is None
    else:
        check_host_roundtrip(cache, req, loc, torch)
    result.update(
        host_kv_bytes=sum(b.nbytes for b in cache.host_kv_buffer),
        host_mapping_count=len(cache.host_ptr_list) + len(cache.dev_ptr_list),
        host_metadata_bytes=cache.host_kv_ctx_len.nbytes
        if cache.host_kv_ctx_len is not None
        else 0,
        main_kv_staging_bytes=sum(
            b.nbytes
            for buffers in (cache.pd_decode_k_staging, cache.pd_decode_v_staging)
            for b in (buffers or [])
        ),
        sparse_cache_bytes=sum(b.nbytes for b in cache.device_kv_buffer),
    )
    assert result["sparse_cache_bytes"] > 0
    assert (result["host_kv_bytes"] > 0) == mode.uses_host_kv_offload
    assert (result["host_mapping_count"] > 0) == mode.uses_host_kv_offload
    assert (result["host_metadata_bytes"] > 0) == mode.uses_host_kv_offload
    assert (result["main_kv_staging_bytes"] > 0) == mode.uses_pd_decode_staging
    for table in cache.device_slot_map:
        table[row].fill_(0)
    # Existing chunk retains cache; a new request reusing its row resets it.
    assert rows.alloc([req]) == [row]
    assert all(bool((m[row].cpu() == 0).all()) for m in cache.device_slot_map)
    rows.free(req)
    req = SimpleNamespace(kv=ReqKvInfo(), origin_input_ids=[33], bootstrap_room=43)
    assert rows.alloc([req]) == [row]
    cache.init_req(req)
    assert all(bool((m[row].cpu() == -1).all()) for m in cache.device_slot_map)
    for table in cache.device_slot_map:
        table[row].fill_(0)
    rows.free(req)
    rows.clear()
    assert rows.available_size() == 2
    assert all(bool((m.cpu() == -1).all()) for m in cache.device_slot_map)
    assert not cache._pd_room_to_req_pool_idx
    allocator.free(loc)
    assert allocator.available_size() == pool.size
    torch.npu.synchronize()
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if not __debug__:
        raise RuntimeError("Run this verification without Python -O / PYTHONOPTIMIZE")
    try:
        # Modes are explicit component inputs; environment flags must not cause
        # server initialization or change the ordinary regression cases.
        with patch.dict("os.environ", {"SGLANG_NPU_ENABLE_MEMPOOL": "0"}):
            report = {"success": True, **verify(args)}
    except Exception as exc:
        traceback.print_exc()
        report = {
            "success": False,
            "mode": args.mode,
            "error": f"{type(exc).__name__}: {exc}",
        }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(
        "RESOURCE_PASS" if report["success"] else "RESOURCE_FAIL",
        json.dumps(report),
        flush=True,
    )
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
