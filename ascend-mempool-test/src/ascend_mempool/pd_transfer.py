"""Small transport fixtures shared by CPU checks and the standalone NPU gate.

Only construction and network delivery are supplied here. Copy planning,
sender chunking, worker completion and abort draining run production methods.
This is test support, never imported by the model server.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from queue import Queue
from types import SimpleNamespace
from typing import Any


def make_gate_descriptor(descriptor_cls: Any) -> Any:
    """Describe the standalone gate's BM fixture without allocating BM storage."""
    layers, slots, prompt_tokens, decode_tokens, heads, dim = 8, 16, 256, 32, 1, 576
    all_slot_row_bytes = layers * slots * heads * dim * 2  # BF16
    prompt_bytes = prompt_tokens * all_slot_row_bytes
    decode_bytes = decode_tokens * all_slot_row_bytes
    return descriptor_cls(
        layers=layers,
        slots=slots,
        prompt_tokens=prompt_tokens,
        decode_tokens=decode_tokens,
        heads=heads,
        dim=dim,
        dtype="bfloat16",
        prompt_bytes=prompt_bytes,
        decode_bytes=decode_bytes,
        stride_bytes=max(prompt_bytes, decode_bytes),
    )


def make_args(args_cls: Any, pool: Any, aux: Any) -> Any:
    """Fill the existing KVArgs fields with real buffer addresses."""
    args = args_cls()
    args.page_size = pool.page_size
    args.kv_data_ptrs, args.kv_data_lens, args.kv_item_lens = (
        pool.get_contiguous_buf_infos()
    )
    args.kv_layer_ids = []
    args.kv_buf_groups = 0
    args.num_draft_entries = 0
    args.aux_data_ptrs = [aux.data_ptr()]
    args.aux_data_lens = [aux.nbytes]
    args.aux_item_lens = [aux[0].nbytes]
    for field in (
        "state_types",
        "state_data_ptrs",
        "state_data_lens",
        "state_item_lens",
        "state_layer_ids",
        "state_dim_per_tensor",
    ):
        setattr(args, field, [])
    return args


def make_manager(manager_cls: Any, args: Any, engine: Any, mode: Any) -> Any:
    """Construct one logical TP rank without server sockets or worker threads.

    PoolPeer's fixed TP16 contract is retained. This fixture tests just one
    paired rank; it does not execute a TP collective or claim TP16 acceptance.
    """
    manager = manager_cls.__new__(manager_cls)
    manager.kv_args = args
    manager.engine = engine
    manager.disaggregation_mode = mode
    manager.mempool_control = None
    manager._mempool_transfer_layout = None
    manager.sparse_pd_decode_staging = None
    manager.is_mla_backend = True
    manager.is_hybrid_mla_backend = False
    manager.enable_staging = manager.enable_custom_mem_pool = False
    manager.pp_size = manager.dcp_size = manager.attn_cp_size = 1
    manager.attn_tp_size = 16
    manager.attn_tp_rank = manager.pp_rank = manager.attn_cp_rank = 0
    manager.enable_all_cp_ranks_for_transfer = manager.is_dummy_cp_rank = False
    manager.max_transfer_batch_indices = 0
    manager.enable_trace = False
    manager.enable_deferred_decode_kv_release = True
    manager.bootstrap_port = 0
    manager.request_status = {}
    manager.transfer_infos = {}
    manager.decode_kv_args_table = {}
    manager.req_to_decode_prefix_len = {}
    manager._staging_outstanding = defaultdict(int)
    manager._deferred_ack_targets = {}
    manager.failure_lock = threading.Lock()
    manager.session_lock = threading.Lock()
    manager.failure_records = {}
    manager.failed_sessions = set()
    manager.session_failures = defaultdict(int)
    manager.transfer_queues = [Queue()]
    return manager


def target_fields(args: Any, session: str) -> dict[str, Any]:
    """Carry native registration fields over the gate's test channel."""
    return dict(
        room="0",
        endpoint="127.0.0.1",
        dst_port=1,
        mooncake_session_id=session,
        dst_kv_ptrs=args.kv_data_ptrs,
        dst_aux_ptrs=args.aux_data_ptrs,
        dst_kv_layer_ids=args.kv_layer_ids,
        dst_kv_item_len=args.kv_item_lens[0],
        dst_kv_item_lens=args.kv_item_lens,
        dst_tp_rank=0,
        dst_attn_tp_size=16,
        dst_dcp_size=1,
        requires_dcp_relayout=False,
        dst_state_data_ptrs=args.state_data_ptrs,
        dst_state_item_lens=args.state_item_lens,
        dst_state_layer_ids=args.state_layer_ids,
        dst_state_dim_per_tensor=args.state_dim_per_tensor,
    )


def make_sender(sender_cls: Any, manager: Any, room: int, pages: Any, poll: Any) -> Any:
    """One real sender/worker request with non-contiguous destination pages."""
    session = manager.mempool_control.peer.transport_session
    manager.request_status[room] = poll.WaitingForInput
    manager.transfer_infos[room] = {
        session: SimpleNamespace(
            room=room,
            mooncake_session_id=session,
            is_dummy=False,
            dst_kv_indices=pages,
            dst_device_kv_indices=None,
            dst_aux_index=3,
            endpoint="127.0.0.1",
            dst_port=1,
            required_dst_info_num=1,
        )
    }
    sender = sender_cls.__new__(sender_cls)
    sender.kv_mgr = manager
    sender.bootstrap_room = room
    sender.curr_idx = 0
    sender.num_kv_indices = len(pages)
    sender.aux_index = 1
    sender._transfer_num_kv_indices = sender._transfer_num_state_indices = 0
    # Tracing is disabled; the sender still copies its no-op context on enqueue.
    from copy import copy

    context = SimpleNamespace()
    context.copy_for_thread = lambda: copy(context)
    sender.trace_ctx = context
    return sender


def drain_worker(manager: Any) -> None:
    """Consume already queued chunks through the original worker loop."""
    queue = manager.transfer_queues[0]
    queue.put(None)
    manager.transfer_worker(queue, executor=None)
