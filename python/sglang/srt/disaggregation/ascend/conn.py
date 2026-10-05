import concurrent.futures
import enum
import logging
from typing import Any, Callable, List, Optional, Tuple

import numpy as np
import numpy.typing as npt

from sglang.srt.disaggregation.ascend.mempool_control import (
    MempoolFrameRouter,
    MempoolPDControl,
)
from sglang.srt.disaggregation.ascend.mempool_protocol import (
    IndexKTransferLayout,
    MempoolMessage,
    encode_message,
)
from sglang.srt.disaggregation.ascend.sparse_pd import (
    SparsePDDecodeStagingPool,
    get_sparse_pd_manager,
    is_sparse_pd_decode_enabled,
)
from sglang.srt.disaggregation.ascend.transfer_engine import AscendTransferEngine
from sglang.srt.disaggregation.base.conn import KVPoll, StateType
from sglang.srt.disaggregation.common.utils import group_concurrent_contiguous
from sglang.srt.disaggregation.mooncake.conn import (
    MooncakeKVBootstrapServer,
    MooncakeKVManager,
    MooncakeKVReceiver,
    MooncakeKVSender,
)
from sglang.srt.runtime_context import get_parallel
from sglang.srt.utils.network import NetworkAddress, get_local_ip_auto

logger = logging.getLogger(__name__)


class AscendStateType(str, enum.Enum):
    """DSV4-on-NPU PD components without a cross-hardware equivalent."""

    DSV4_C128 = "dsv4_c128"
    # C4 compress-state rows (attention + indexer) addressed within each
    # req_pool_idx bank on A5 (CYCLE cache_mode).  Separate from StateType.SWA
    # because each peer maps logical positions into its own local ring.
    DSV4_C4_STATE = "dsv4_c4_state"


_DSV4_KVCACHE_STATE_TYPES = tuple(AscendStateType)


class AscendKVManager(MooncakeKVManager):
    def __init__(
        self,
        args,
        disaggregation_mode,
        server_args,
        is_mla_backend: Optional[bool] = False,
    ):
        self.sparse_pd_decode_staging = None
        self.mempool_control: Optional[MempoolPDControl] = None
        self._mempool_transfer_layout: Optional[IndexKTransferLayout] = None
        self._mempool_discovery = None
        self._mempool_discovery_executor = None
        self._mempool_frame_router = MempoolFrameRouter()

        sparse_kv_manager = get_sparse_pd_manager()
        if is_sparse_pd_decode_enabled(sparse_kv_manager):
            native_kv_pool = sparse_kv_manager.paged_kv_cache
            if getattr(native_kv_pool, "dsa_kv_cache_store_fp8", False):
                raise NotImplementedError(
                    "Ascend sparse KV PD does not support FP8-packed DSA KV "
                    "cache yet. The temporary PD staging path requires "
                    "separate native K and V source buffers."
                )
            self.sparse_pd_decode_staging = SparsePDDecodeStagingPool(
                sparse_kv_manager,
                page_size=args.page_size,
                slot_count=1,
            )
            layer_num = int(sparse_kv_manager.layer_num)
            staging_entry_count = 2 * layer_num
            total_entry_count = len(args.kv_data_ptrs)
            state_entry_count = total_entry_count - staging_entry_count
            state_layer_ids = native_kv_pool.get_state_layer_ids()
            expected_layer_ids = (
                list(
                    range(
                        sparse_kv_manager.start_layer,
                        sparse_kv_manager.start_layer + layer_num,
                    )
                )
                * 2
                + state_layer_ids
            )
            if total_entry_count < staging_entry_count or (
                state_entry_count != len(state_layer_ids)
            ):
                raise RuntimeError(
                    "Ascend sparse KV PD decode transfer layout is inconsistent: "
                    f"entries={total_entry_count}, K/V staging={staging_entry_count}, "
                    f"native state entries={state_entry_count}, "
                    f"state layer ids={len(state_layer_ids)}."
                )
            if (
                len(args.kv_data_lens) != total_entry_count
                or len(args.kv_item_lens) != total_entry_count
            ):
                raise RuntimeError(
                    "Ascend sparse KV PD decode received inconsistent transfer "
                    "buffer metadata."
                )
            # The native indexer can cover only selected model layers (for
            # example GLM-5.2). Explicit layer ids let the generic transfer
            # pair K/V staging and compact index state without forcing a
            # rectangular [group, layer] layout.
            args.kv_buf_groups = 2
            args.kv_layer_ids = expected_layer_ids
            logger.info(
                "Ascend sparse KV PD decode uses K/V HBM staging plus native "
                "index-state buffers for transfer: entries=%s, state_entries=%s, "
                "page_size=%s",
                total_entry_count,
                state_entry_count,
                args.page_size,
            )

        super().__init__(args, disaggregation_mode, server_args, is_mla_backend)
        from sglang.srt.environ import envs

        if envs.SGLANG_NPU_ENABLE_MEMPOOL.get():
            # Keep native abort drain ACKs alive; mempool never uses the generic
            # deferred-release timeout that frees unconfirmed buffers.
            self.enable_deferred_decode_kv_release = True

    def configure_mempool_transfer(self, pool: Any) -> Optional[IndexKTransferLayout]:
        """Finalize existing KVArgs before receivers publish them to a peer.

        The NPU pool already filtered addresses before engine registration.
        Service construction supplies the actual pool here, before admission;
        only logical metadata changes, never a registered address or length.
        """
        if self.mempool_control is not None or self.request_status:
            raise RuntimeError("Configure mempool transfer before admitting requests")
        if not pool.sparse_kv_offload_mode.uses_index_k_only_transfer:
            return None
        args = self.kv_args
        if (
            not self.is_mla_backend
            or self.pp_size != 1
            or self.dcp_size != 1
            or self.enable_staging
            or args.num_draft_entries
        ):
            raise ValueError(
                "Index K-only transfer requires MLA, PP=1, DCP=1 without staging/draft"
            )
        if pool.get_contiguous_buf_infos() != (
            args.kv_data_ptrs,
            args.kv_data_lens,
            args.kv_item_lens,
        ):
            raise ValueError(
                "Registered buffers differ from the NPU Index K publication"
            )
        if args.page_size != pool.page_size:
            raise ValueError("Native page size differs from the NPU Index K pool")
        layout = IndexKTransferLayout(
            page_size=args.page_size,
            layer_ids=tuple(pool.get_kv_layer_ids()),
            item_lens=tuple(args.kv_item_lens),
            dtypes=tuple(
                str(buf.dtype).removeprefix("torch.") for buf in pool.index_k_buffer
            ),
            aux_item_lens=tuple(args.aux_item_lens),
            state_types=tuple(kind.value for kind in args.state_types),
            state_item_lens=tuple(tuple(row) for row in args.state_item_lens),
            state_layer_ids=tuple(tuple(row) for row in args.state_layer_ids),
            state_dim_per_tensor=tuple(tuple(row) for row in args.state_dim_per_tensor),
        )
        for ptrs, sizes, strides in [
            (args.aux_data_ptrs, args.aux_data_lens, args.aux_item_lens),
            *zip(args.state_data_ptrs, args.state_data_lens, args.state_item_lens),
        ]:
            if not (len(ptrs) == len(sizes) == len(strides)) or any(
                ptr <= 0 or stride <= 0 or size < stride or size % stride
                for ptr, size, stride in zip(ptrs, sizes, strides)
            ):
                raise ValueError("Invalid mempool native handoff buffer metadata")
        if len(args.state_data_ptrs) != len(layout.state_types) or len(
            args.state_data_lens
        ) != len(layout.state_types):
            raise ValueError("Invalid mempool native state components")
        args.kv_layer_ids = list(layout.layer_ids)
        # Formal BF16 publishes exactly one Index K component group. Its layer
        # count can be smaller than the model's total number of layers.
        args.kv_buf_groups = 1
        self._mempool_transfer_layout = layout
        logger.info(
            "mempool native_transfer kind=index_k_only entries=%s main_kv_entries=0",
            len(layout.layer_ids),
        )
        return layout

    def attach_mempool_control(self, control: MempoolPDControl) -> None:
        """Attach the mapped pool's request control after PD manager startup."""
        if self.mempool_control is not None:
            raise RuntimeError("Ascend mempool control is already attached")
        if (
            control.local.transfer_layout != self._mempool_transfer_layout
            or control.local.transport_session != self.get_session_id()
        ):
            raise ValueError(
                "Mempool control differs from the configured native transport"
            )
        parallel = get_parallel()
        if (
            control.local.role != self.disaggregation_mode.value
            or control.local.tp_rank != parallel.tp_rank
            or control.local.tp_size != parallel.tp_size
            or control.local.pp_size != parallel.pp_size
        ):
            raise ValueError("Ascend mempool control does not match this PD worker")
        self._mempool_frame_router.attach(control)
        self.mempool_control = control

    def send_mempool_message(
        self, endpoint: str, message: MempoolMessage, is_ipv6: bool = False
    ) -> None:
        """Send a tagged control frame over the existing cached PD PUSH socket."""
        if self.mempool_control is None:
            raise RuntimeError("Ascend mempool control is not attached")
        self._send_multipart_locked(endpoint, encode_message(message), is_ipv6=is_ipv6)

    def discover_mempool_peer(
        self, host: str, port: int, tp_rank: int
    ) -> Optional[str]:
        """Poll the existing P bootstrap registry without blocking TP intake."""
        if self._mempool_discovery_executor is None:
            self._mempool_discovery_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=1
            )
        if self._mempool_discovery is None:
            self._mempool_discovery = self._mempool_discovery_executor.submit(
                self._query_mempool_peer, host, port, tp_rank
            )
            return None
        if not self._mempool_discovery.done():
            return None
        endpoint = self._mempool_discovery.result()
        self._mempool_discovery = None
        return endpoint

    @staticmethod
    def _query_mempool_peer(host: str, port: int, tp_rank: int) -> Optional[str]:
        """Resolve P_i's existing PULL endpoint, fixing DP/CP/PP at zero."""
        import requests

        try:
            response = requests.get(
                NetworkAddress(host, port).to_url() + "/route",
                params={
                    "prefill_dp_rank": 0,
                    "prefill_cp_rank": 0,
                    "target_tp_rank": tp_rank,
                    "target_pp_rank": 0,
                },
                timeout=2,
            )
            if response.status_code != 200:
                return None
            info = response.json()
            return NetworkAddress(info["rank_ip"], int(info["rank_port"])).to_tcp()
        except (requests.RequestException, KeyError, ValueError):
            return None

    def mempool_prefill_transfer_drained(self, room: int) -> bool:
        """Require terminal native status and no transfer worker using P pages."""
        return (
            self.check_status(room) in (KVPoll.Success, KVPoll.Failed)
            and self._staging_outstanding.get(room, 0) == 0
        )

    def _make_worker_recv(
        self, socket: Any, timeout_ms: int = 500
    ) -> Callable[[], Optional[List[bytes]]]:
        """Route tagged mempool frames through the existing single ZMQ reader."""
        receive: Callable[[], Optional[List[bytes]]] = super()._make_worker_recv(
            socket, timeout_ms
        )
        wrapped: Callable[[], Optional[List[bytes]]] = (
            self._mempool_frame_router.wrap_receive(receive)
        )
        return wrapped

    def _requires_exact_state_index_match(self, st: StateType) -> bool:
        return (
            super()._requires_exact_state_index_match(st)
            or st in _DSV4_KVCACHE_STATE_TYPES
        )

    def init_engine(self):
        # TransferEngine initialized on ascend.
        local_ip = get_local_ip_auto()
        self.engine = AscendTransferEngine(
            hostname=local_ip,
            npu_id=self.kv_args.gpu_id,
            disaggregation_mode=self.disaggregation_mode,
        )

    def register_buffer_to_engine(self):
        # MemFabric aligns registered buffers to 2 MiB. Register everything in
        # one batch so overlapping aligned ranges from small tensors are merged
        # before they are published to the peer.
        ptrs = list(self.kv_args.kv_data_ptrs)
        lens = list(self.kv_args.kv_data_lens)
        ptrs.extend(self.kv_args.aux_data_ptrs)
        lens.extend(self.kv_args.aux_data_lens)
        for component_ptrs, component_lens in zip(
            self.kv_args.state_data_ptrs or [],
            self.kv_args.state_data_lens or [],
        ):
            ptrs.extend(component_ptrs)
            lens.extend(component_lens)
        if ptrs:
            self.engine.batch_register(ptrs, lens)

    def get_mla_kv_ptrs_with_pp(
        self, src_kv_ptrs: List[int], dst_kv_ptrs: List[int], state_type=None
    ) -> Tuple[List[int], List[int], int]:
        mla_ratios = getattr(self.kv_args, "mla_compression_ratios", None)
        if mla_ratios:
            if len(src_kv_ptrs) == len(dst_kv_ptrs):
                return src_kv_ptrs, dst_kv_ptrs, len(src_kv_ptrs)

            start_layer = self.kv_args.prefill_start_layer
            end_layer = self.kv_args.prefill_end_layer
            c4_full = sum(ratio == 4 for ratio in mla_ratios)
            c4_start = sum(ratio == 4 for ratio in mla_ratios[:start_layer])
            c4_end = sum(ratio == 4 for ratio in mla_ratios[:end_layer])
            c128_start = sum(ratio == 128 for ratio in mla_ratios[:start_layer])
            c128_end = sum(ratio == 128 for ratio in mla_ratios[:end_layer])

            if state_type == AscendStateType.DSV4_C128:
                dst = dst_kv_ptrs[c128_start:c128_end]
                return src_kv_ptrs, dst, len(src_kv_ptrs)

            if state_type == AscendStateType.DSV4_C4_STATE:
                # Layout: [attn_state_0..attn_{c4_full-1},
                #          idx_state_0..idx_{c4_full-1}]
                # Two groups, each c4_full entries; slice both by PP stage.
                dst = []
                for offset in (0, c4_full):
                    dst.extend(dst_kv_ptrs[offset + c4_start : offset + c4_end])
                return src_kv_ptrs, dst, len(src_kv_ptrs)

            # NPU main KV layout: [C4 KV, index K, index scale].
            if state_type is None and len(dst_kv_ptrs) == 3 * c4_full:
                dst = []
                for offset in (0, c4_full, 2 * c4_full):
                    dst.extend(dst_kv_ptrs[offset + c4_start : offset + c4_end])
                return src_kv_ptrs, dst, len(src_kv_ptrs)

            # On A5 (CYCLE cache_mode), StateType.SWA only contains SWA KV
            # buffers (C4 compress state is registered separately as
            # DSV4_C4_STATE).  The common _mla_slice_ptrs_for_pp assumes
            # SWA + C4 state are bundled (swa_L + 2*c4_full), so intercept
            # here and slice SWA KV by layer index directly.
            if state_type == StateType.SWA and AscendStateType.DSV4_C4_STATE in (
                self.kv_args.state_types or []
            ):
                dst = list(dst_kv_ptrs[start_layer:end_layer])
                return src_kv_ptrs, dst, len(src_kv_ptrs)

            return super().get_mla_kv_ptrs_with_pp(src_kv_ptrs, dst_kv_ptrs, state_type)

        # src_kv_ptrs: k_data, v_data, index_k_data(optional)
        # dst_kv_ptrs: k_data, v_data, index_k_data(optional)
        # state_type is accepted for parity with the common disaggregation path;
        # the NPU kv_buf_groups slicing below is state-type agnostic.
        kv_buf_groups = getattr(self.kv_args, "kv_buf_groups", 1)
        hidden_kv_layers = getattr(self.kv_args, "hidden_kv_layers", 0)
        draft_kv_layers = getattr(self.kv_args, "draft_kv_layers", 0)
        src_layers = len(src_kv_ptrs) // kv_buf_groups
        dst_layers = len(dst_kv_ptrs) // kv_buf_groups
        if src_layers == dst_layers:
            sliced_dst_kv_ptrs = dst_kv_ptrs
        else:
            sliced_dst_kv_ptrs = []
            start_layer = self.kv_args.prefill_start_layer
            transfer_draft_kv = get_parallel().pp_group.is_last_rank and draft_kv_layers
            if transfer_draft_kv:
                end_layer = start_layer + src_layers - draft_kv_layers
            else:
                end_layer = start_layer + src_layers

            # target kv
            for i in range(kv_buf_groups):
                layer_offset = i * hidden_kv_layers
                sliced_dst_kv_ptrs.extend(
                    dst_kv_ptrs[layer_offset + start_layer : layer_offset + end_layer]
                )
            # draft kv
            if transfer_draft_kv:
                for i in range(kv_buf_groups):
                    layer_offset = (
                        i * draft_kv_layers + kv_buf_groups * hidden_kv_layers
                    )
                    sliced_dst_kv_ptrs.extend(
                        dst_kv_ptrs[layer_offset : layer_offset + draft_kv_layers]
                    )
        layers_current_pp_stage = len(src_kv_ptrs)
        return src_kv_ptrs, sliced_dst_kv_ptrs, layers_current_pp_stage

    def _validate_mempool_target(self, session: str) -> None:
        """Bind the native registration to the peer checked by the handshake."""
        layout = self._mempool_transfer_layout
        control = self.mempool_control
        if (
            layout is None
            or control is None
            or control.peer is None
            or control.local.transfer_layout != layout
            or control.peer.transfer_layout != layout
            or control.peer.transfer_kind != "index_k_only"
            or control.peer.transport_session != session
        ):
            raise ValueError("Index K transfer has no matching mempool peer/session")
        target = self.decode_kv_args_table.get(session)
        if target is None:
            raise ValueError("Index K transfer has no native destination registration")
        if (
            tuple(target.dst_kv_layer_ids) != layout.layer_ids
            or tuple(target.dst_kv_item_lens) != layout.item_lens
            or target.dst_kv_item_len != layout.item_lens[0]
            or len(target.dst_kv_ptrs) != len(layout.layer_ids)
            or len(target.dst_aux_ptrs) != len(layout.aux_item_lens)
            or target.dst_attn_tp_size != self.attn_tp_size
            or target.dst_dcp_size != 1
            or target.requires_dcp_relayout
            or tuple(tuple(row) for row in target.dst_state_item_lens)
            != layout.state_item_lens
            or tuple(tuple(row) for row in target.dst_state_layer_ids)
            != layout.state_layer_ids
            or tuple(tuple(row) for row in target.dst_state_dim_per_tensor)
            != layout.state_dim_per_tensor
            or len(target.dst_state_data_ptrs) != len(layout.state_item_lens)
            or any(
                len(ptrs) != len(sizes)
                for ptrs, sizes in zip(
                    target.dst_state_data_ptrs, layout.state_item_lens
                )
            )
            or any(
                ptr <= 0
                for ptr in [
                    *target.dst_kv_ptrs,
                    *target.dst_aux_ptrs,
                    *(ptr for row in target.dst_state_data_ptrs for ptr in row),
                ]
            )
        ):
            raise ValueError(
                "Native destination registration differs from the Index K transfer layout"
            )

    def _send_mempool_index_k(
        self,
        session,
        src_indices,
        dst_ptrs,
        dst_indices,
        executor,
        dst_layer_ids,
        dst_device_indices,
    ):
        try:
            self._validate_mempool_target(session)
            target = self.decode_kv_args_table[session]
            if (
                dst_ptrs != target.dst_kv_ptrs
                or dst_layer_ids != target.dst_kv_layer_ids
                or dst_device_indices is not None
            ):
                raise ValueError(
                    "Index K transfer requires the registered native page space"
                )
        except ValueError as exc:
            logger.error(
                "mempool native transfer rejected session=%s: %s", session, exc
            )
            return -1
        return self._send_kvcache_generic(
            mooncake_session_id=session,
            src_data_ptrs=self.kv_args.kv_data_ptrs,
            dst_data_ptrs=dst_ptrs,
            item_lens=self.kv_args.kv_item_lens,
            prefill_data_indices=src_indices,
            dst_data_indices=dst_indices,
            executor=executor,
            src_layer_ids=self.kv_args.kv_layer_ids,
            dst_layer_ids=dst_layer_ids,
        )

    def maybe_send_extra(
        self, req, prefill_state_indices, executor, target_rank_registration_info=None
    ):
        if self._mempool_transfer_layout is not None:
            try:
                self._validate_mempool_target(req.mooncake_session_id)
            except ValueError as exc:
                logger.error("mempool state transfer rejected: %s", exc)
                return -1
        return super().maybe_send_extra(
            req, prefill_state_indices, executor, target_rank_registration_info
        )

    def send_aux(self, req, prefill_aux_index, dst_aux_ptrs):
        # The existing worker skips send_kvcache for an empty final chunk.
        # Check that path here too, without changing shared worker semantics.
        if self._mempool_transfer_layout is not None:
            try:
                self._validate_mempool_target(req.mooncake_session_id)
                if (
                    dst_aux_ptrs
                    != self.decode_kv_args_table[req.mooncake_session_id].dst_aux_ptrs
                ):
                    raise ValueError("Aux buffers differ from the native registration")
            except ValueError as exc:
                logger.error("mempool aux transfer rejected: %s", exc)
                return -1
        return super().send_aux(req, prefill_aux_index, dst_aux_ptrs)

    def send_kvcache(
        self,
        mooncake_session_id: str,
        prefill_kv_indices: npt.NDArray[np.int32],
        dst_kv_ptrs: list[int],
        dst_kv_indices: npt.NDArray[np.int32],
        executor: concurrent.futures.ThreadPoolExecutor,
        dst_layer_ids: Optional[List[int]] = None,
        dst_device_kv_indices: Optional[npt.NDArray[np.int32]] = None,
        dst_kv_item_len: Optional[int] = None,
        dst_attn_tp_size: Optional[int] = None,
    ):
        if self._mempool_transfer_layout is not None:
            return self._send_mempool_index_k(
                mooncake_session_id,
                prefill_kv_indices,
                dst_kv_ptrs,
                dst_kv_indices,
                executor,
                dst_layer_ids,
                dst_device_kv_indices,
            )
        use_sparse_pd_split_indices = (
            dst_device_kv_indices is not None and self.is_mla_backend
        )
        if dst_device_kv_indices is not None and not use_sparse_pd_split_indices:
            raise NotImplementedError(
                "Ascend KV transfer only supports separate destination device "
                "indices for sparse PD MLA K/V staging plus native index state."
            )
        self._validate_envelope_kv_layout(
            dst_kv_ptrs, dst_kv_item_len, dst_attn_tp_size
        )
        if use_sparse_pd_split_indices:
            if not dst_layer_ids or len(dst_layer_ids) != len(dst_kv_ptrs):
                raise RuntimeError(
                    "Sparse KV PD requires explicit destination layer metadata: "
                    f"entries={len(dst_kv_ptrs)}, "
                    f"layer_ids={len(dst_layer_ids) if dst_layer_ids else 0}."
                )
            dst_main_layer_count = self._get_sparse_pd_main_layer_count(dst_layer_ids)
            native_state_ptrs = set(dst_kv_ptrs[2 * dst_main_layer_count :])
            if not native_state_ptrs:
                raise RuntimeError(
                    "Sparse KV PD destination transfer is missing native "
                    "index-state buffers."
                )
            return self._send_kvcache_generic(
                mooncake_session_id=mooncake_session_id,
                src_data_ptrs=self.kv_args.kv_data_ptrs,
                dst_data_ptrs=dst_kv_ptrs,
                item_lens=self.kv_args.kv_item_lens,
                prefill_data_indices=prefill_kv_indices,
                dst_data_indices=dst_kv_indices,
                executor=executor,
                src_layer_ids=self._get_sparse_pd_source_layer_ids(dst_layer_ids),
                dst_layer_ids=dst_layer_ids,
                dst_device_data_indices=dst_device_kv_indices,
                dst_device_data_ptrs=native_state_ptrs,
            )

        # Hybrid MLA prefill stages expose PP-local entries, while a PP=1
        # decode peer registers all model layers. Pair only this layout by
        # global layer id; every other Ascend layout keeps the legacy path.
        if self.is_hybrid_mla_backend and self.pp_size > 1:
            return self._send_kvcache_generic(
                mooncake_session_id=mooncake_session_id,
                src_data_ptrs=self.kv_args.kv_data_ptrs,
                dst_data_ptrs=dst_kv_ptrs,
                item_lens=self.kv_args.kv_item_lens,
                prefill_data_indices=prefill_kv_indices,
                dst_data_indices=dst_kv_indices,
                executor=executor,
                src_layer_ids=self.kv_args.kv_layer_ids,
                dst_layer_ids=dst_layer_ids,
            )

        # Group by indices
        prefill_kv_blocks, dst_kv_blocks = group_concurrent_contiguous(
            prefill_kv_indices, dst_kv_indices
        )

        if self.pp_size > 1:
            if self.is_mla_backend:
                src_kv_ptrs, sliced_dst_kv_ptrs, layers_current_pp_stage = (
                    self.get_mla_kv_ptrs_with_pp(self.kv_args.kv_data_ptrs, dst_kv_ptrs)
                )
                layers_params = [
                    (
                        layer_id,
                        src_kv_ptrs[layer_id],
                        sliced_dst_kv_ptrs[layer_id],
                        self.kv_args.kv_item_lens[layer_id],
                    )
                    for layer_id in range(layers_current_pp_stage)
                ]
            else:
                (
                    src_k_ptrs,
                    src_v_ptrs,
                    dst_k_ptrs,
                    dst_v_ptrs,
                    layers_current_pp_stage,
                ) = self.get_mha_kv_ptrs_with_pp(self.kv_args.kv_data_ptrs, dst_kv_ptrs)

                layers_params = [
                    (
                        layer_id,
                        src_k_ptrs[layer_id],
                        dst_k_ptrs[layer_id],
                        self.kv_args.kv_item_lens[layer_id],
                    )
                    for layer_id in range(layers_current_pp_stage)
                ] + [
                    (
                        layers_current_pp_stage + layer_id,
                        src_v_ptrs[layer_id],
                        dst_v_ptrs[layer_id],
                        self.kv_args.kv_item_lens[layers_current_pp_stage + layer_id],
                    )
                    for layer_id in range(layers_current_pp_stage)
                ]
        else:
            num_layers = len(self.kv_args.kv_data_ptrs)
            layers_params = [
                (
                    layer_id,
                    self.kv_args.kv_data_ptrs[layer_id],
                    dst_kv_ptrs[layer_id],
                    self.kv_args.kv_item_lens[layer_id],
                )
                for layer_id in range(num_layers)
            ]

        def set_transfer_blocks(
            src_ptr: int,
            dst_ptr: int,
            item_len: int,
        ) -> List[Tuple[int, int, int]]:
            transfer_blocks = []
            for prefill_index, decode_index in zip(prefill_kv_blocks, dst_kv_blocks):
                src_addr = src_ptr + int(prefill_index[0]) * item_len
                dst_addr = dst_ptr + int(decode_index[0]) * item_len
                length = item_len * len(prefill_index)
                transfer_blocks.append((src_addr, dst_addr, length))
            return transfer_blocks

        # Worker function for processing a single layer
        def process_layer(
            src_ptr: int,
            dst_ptr: int,
            item_len: int,
        ) -> int:
            transfer_blocks = set_transfer_blocks(src_ptr, dst_ptr, item_len)
            return self._transfer_data(mooncake_session_id, transfer_blocks)

        # Worker function for processing all layers in a batch
        def process_layers(layers_params: List[Tuple[int, int, int, int]]) -> int:
            transfer_blocks = []
            for _, src_ptr, dst_ptr, item_len in layers_params:
                transfer_blocks.extend(set_transfer_blocks(src_ptr, dst_ptr, item_len))
            return self._transfer_data(mooncake_session_id, transfer_blocks)

        if self.enable_custom_mem_pool:
            futures = [
                executor.submit(
                    process_layer,
                    src_ptr,
                    dst_ptr,
                    item_len,
                )
                for (_, src_ptr, dst_ptr, item_len) in layers_params
            ]
            for future in concurrent.futures.as_completed(futures):
                status = future.result()
                if status != 0:
                    for f in futures:
                        f.cancel()
                    return status
        else:
            # Combining all layers' params in one batch transfer is more efficient
            # compared to using multiple threads
            return process_layers(layers_params)

        return 0

    @staticmethod
    def _get_sparse_pd_main_layer_count(dst_layer_ids: List[int]) -> int:
        """Return the shared K/V layer-group length in sparse-PD metadata."""
        layer_ids = list(dst_layer_ids)
        for layer_count in range(1, len(layer_ids) // 2 + 1):
            if layer_ids[:layer_count] == layer_ids[layer_count : 2 * layer_count]:
                return layer_count
        raise RuntimeError(
            "Sparse KV PD destination layer metadata must begin with identical "
            "K and V layer-id groups."
        )

    def _get_sparse_pd_source_layer_ids(self, dst_layer_ids: List[int]) -> List[int]:
        """Build P-local ids for native K/V plus compact DSA index state."""
        if int(getattr(self.kv_args, "num_draft_entries", 0)) != 0:
            raise NotImplementedError(
                "Ascend sparse KV PD does not support draft KV transfer yet."
            )

        start_layer = int(self.kv_args.prefill_start_layer)
        end_layer = self.kv_args.prefill_end_layer
        if end_layer is None:
            raise RuntimeError(
                "Sparse KV PD requires prefill_end_layer to map PP-local buffers."
            )
        end_layer = int(end_layer)
        source_main_layer_ids = list(range(start_layer, end_layer))
        source_state_count = len(self.kv_args.kv_data_ptrs) - 2 * len(
            source_main_layer_ids
        )
        if source_state_count < 0:
            raise RuntimeError(
                "Sparse KV PD source transfer layout has fewer entries than its "
                f"K/V layer groups: entries={len(self.kv_args.kv_data_ptrs)}, "
                f"layers={len(source_main_layer_ids)}."
            )

        dst_main_layer_count = self._get_sparse_pd_main_layer_count(dst_layer_ids)
        dst_state_layer_ids = list(dst_layer_ids[2 * dst_main_layer_count :])
        source_state_layer_ids = [
            layer_id
            for layer_id in dst_state_layer_ids
            if start_layer <= layer_id < end_layer
        ]
        if len(source_state_layer_ids) != source_state_count:
            raise RuntimeError(
                "Sparse KV PD source/destination index-state layout mismatch: "
                f"source state entries={source_state_count}, "
                f"destination state entries in PP range={len(source_state_layer_ids)}, "
                f"prefill layers=[{start_layer}, {end_layer})."
            )
        return source_main_layer_ids * 2 + source_state_layer_ids

    def _is_generic_kvcache_state_type(self, st) -> bool:
        # DSV4 per-pool components also use the page-indexed send path.
        return (
            super()._is_generic_kvcache_state_type(st)
            or st in _DSV4_KVCACHE_STATE_TYPES
        )

    def update_status(self, bootstrap_room: int, status: KVPoll):
        staging = getattr(self, "sparse_pd_decode_staging", None)
        if staging is not None:
            if status == KVPoll.Success and staging.has_room(bootstrap_room):
                try:
                    metadata = staging.offload_room_to_host(
                        bootstrap_room,
                        release=self.mempool_control is None,
                    )
                    if self.mempool_control is not None:
                        staging.release_room(bootstrap_room)
                    logger.debug(
                        "Ascend sparse KV PD staged transfer committed: "
                        "room=%s slot=%s req_pool_idx=%s token_count=%s",
                        metadata.room,
                        metadata.slot_id,
                        metadata.req_pool_idx,
                        metadata.token_count,
                    )
                except Exception as exc:
                    if self.mempool_control is None:
                        staging.release_room(bootstrap_room)
                    self.record_failure(
                        bootstrap_room,
                        "Failed to offload Ascend sparse KV PD staging buffer "
                        f"to host sparse KV cache: {exc}",
                    )
                    return super().update_status(bootstrap_room, KVPoll.Failed)
            elif status == KVPoll.Failed and self.mempool_control is None:
                staging.release_room(bootstrap_room)

        return super().update_status(bootstrap_room, status)


class AscendKVSender(MooncakeKVSender):
    pass


class AscendKVReceiver(MooncakeKVReceiver):
    def send_metadata(
        self,
        kv_indices: npt.NDArray[np.int32],
        aux_index: Optional[int] = None,
        state_indices: Optional[List[int]] = None,
        decode_prefix_len: Optional[int] = None,
        device_kv_indices: Optional[npt.NDArray[np.int32]] = None,
    ):
        staging = getattr(self.kv_mgr, "sparse_pd_decode_staging", None)
        if staging is not None and getattr(self, "bootstrap_infos", None) is not None:
            if device_kv_indices is not None:
                raise RuntimeError(
                    "Ascend sparse KV PD owns device KV indices for native "
                    "index-K transfer."
                )
            native_kv_indices = np.asarray(kv_indices, dtype=np.int32)
            kv_indices = staging.rewrite_dst_indices(
                self.bootstrap_room,
                native_kv_indices,
                decode_prefix_len=decode_prefix_len or 0,
                wait_for_slot=True,
            )
            device_kv_indices = native_kv_indices

        return super().send_metadata(
            kv_indices,
            aux_index,
            state_indices,
            decode_prefix_len=decode_prefix_len,
            device_kv_indices=device_kv_indices,
        )

    def clear(self) -> None:
        staging = getattr(self.kv_mgr, "sparse_pd_decode_staging", None)
        if staging is not None:
            staging.release_room(self.bootstrap_room)
        return super().clear()

    def abort(self):
        """Retain staging on mempool cancellation until native writers acknowledge drain."""
        if self.kv_mgr.mempool_control is None:
            staging = getattr(self.kv_mgr, "sparse_pd_decode_staging", None)
            if staging is not None:
                staging.release_room(self.bootstrap_room)
        return super().abort()

    def _send_abort_notification(self):
        """Arm drain accounting before either explicit abort or poll failure sends."""
        if self.kv_mgr.mempool_control is not None and not getattr(
            self, "_mempool_abort_armed", False
        ):
            self.kv_mgr.register_deferred_abort_room(self.bootstrap_room)
            self._mempool_abort_armed = True
        return super()._send_abort_notification()


class AscendKVBootstrapServer(MooncakeKVBootstrapServer):
    pass
