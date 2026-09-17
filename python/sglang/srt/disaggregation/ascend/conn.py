import concurrent.futures
import enum
import logging
from typing import List, Optional, Tuple

import numpy as np
import numpy.typing as npt

from sglang.srt.disaggregation.ascend.transfer_engine import AscendTransferEngine
from sglang.srt.disaggregation.ascend.sparse_pd import (
    SparsePDDecodeStagingPool,
    get_sparse_pd_manager,
    is_sparse_pd_decode_enabled,
)
from sglang.srt.disaggregation.base.conn import KVPoll, StateType
from sglang.srt.disaggregation.common.utils import group_concurrent_contiguous
from sglang.srt.disaggregation.mooncake.conn import (
    MooncakeKVBootstrapServer,
    MooncakeKVManager,
    MooncakeKVReceiver,
    MooncakeKVSender,
)
from sglang.srt.utils.network import get_local_ip_auto

logger = logging.getLogger(__name__)


class AscendStateType(str, enum.Enum):
    """DSV4-on-NPU per-pool PD components, kept out of the cross-hardware
    StateType enum. Sent via the same page-indexed path as SWA."""

    DSV4_SWA = "dsv4_swa"
    DSV4_C4 = "dsv4_c4"
    DSV4_C128 = "dsv4_c128"
    DSV4_INDEXER = "dsv4_indexer"
    DSV4_C4_STATE = "dsv4_c4_state"
    DSV4_C128_STATE = "dsv4_c128_state"


_DSV4_KVCACHE_STATE_TYPES = tuple(AscendStateType)


class AscendKVManager(MooncakeKVManager):
    def __init__(
        self,
        args,
        disaggregation_mode,
        server_args,
        is_mla_backend: Optional[bool] = False,
    ):
        self.use_sparse_pd_decode = False
        self.sparse_pd_manager = None
        self.sparse_pd_decode_staging = None

        sparse_kv_manager = get_sparse_pd_manager()
        if is_sparse_pd_decode_enabled(
            server_args,
            disaggregation_mode,
            sparse_kv_manager=sparse_kv_manager,
        ):
            self.use_sparse_pd_decode = True
            self.sparse_pd_manager = sparse_kv_manager
            self.sparse_pd_decode_staging = SparsePDDecodeStagingPool(
                sparse_kv_manager,
                page_size=args.page_size,
                slot_count=1,
            )
            expected_entries = sparse_kv_manager.layer_num * 3
            if len(args.kv_data_ptrs) != expected_entries:
                raise RuntimeError(
                    "Ascend sparse KV PD decode expects transfer buffers in "
                    "K-staging/V-staging/native-index-K groups, got "
                    f"{len(args.kv_data_ptrs)} entries for "
                    f"{sparse_kv_manager.layer_num} layers."
                )
            args.kv_buf_groups = 3
            args.kv_layer_ids = list(
                range(
                    sparse_kv_manager.start_layer,
                    sparse_kv_manager.start_layer + sparse_kv_manager.layer_num,
                )
            ) * 3
            logger.info(
                "Ascend sparse KV PD decode uses K/V HBM staging plus native "
                "index-K buffers for transfer: "
                "entries=%s, page_size=%s",
                len(args.kv_data_ptrs),
                args.page_size,
            )

        super().__init__(args, disaggregation_mode, server_args, is_mla_backend)

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
        # src_kv_ptrs: k_data, v_data, index_k_data(optional)
        # dst_kv_ptrs: k_data, v_data, index_k_data(optional)
        # state_type is accepted for parity with the common disaggregation path;
        # the NPU kv_buf_groups slicing below is state-type agnostic.
        start_layer = self.kv_args.prefill_start_layer
        kv_buf_groups = getattr(self.kv_args, "kv_buf_groups", 1)
        total_kv_layers = getattr(self.kv_args, "total_kv_layers", 0)
        src_layers = len(src_kv_ptrs) // kv_buf_groups

        # Backward-compatible temporary sparse-PD path: older decode workers
        # registered only split K/V staging buffers. Newer workers register
        # K/V staging plus native index-K, so the standard 3-group path below
        # handles them.
        if (
            kv_buf_groups == 3
            and len(dst_kv_ptrs) != len(src_kv_ptrs)
            and len(dst_kv_ptrs) % 2 == 0
        ):
            dst_buf_groups = 2
            dst_total_layers = len(dst_kv_ptrs) // dst_buf_groups
            end_layer = start_layer + src_layers
            if src_layers == dst_total_layers:
                sliced_dst_kv_ptrs = dst_kv_ptrs
            else:
                if end_layer > dst_total_layers:
                    raise RuntimeError(
                        "Sparse KV PD destination staging does not cover the "
                        "prefill PP layer range: "
                        f"start={start_layer}, end={end_layer}, "
                        f"dst_total_layers={dst_total_layers}."
                    )
                sliced_dst_kv_ptrs = []
                for i in range(dst_buf_groups):
                    layer_offset = i * dst_total_layers
                    sliced_dst_kv_ptrs.extend(
                        dst_kv_ptrs[
                            layer_offset + start_layer : layer_offset + end_layer
                        ]
                    )
            sliced_src_kv_ptrs = []
            for i in range(dst_buf_groups):
                layer_offset = i * src_layers
                sliced_src_kv_ptrs.extend(
                    src_kv_ptrs[layer_offset : layer_offset + src_layers]
                )
            if len(sliced_src_kv_ptrs) != len(sliced_dst_kv_ptrs):
                raise RuntimeError(
                    "Sparse KV PD source/destination transfer entry mismatch: "
                    f"src={len(sliced_src_kv_ptrs)}, "
                    f"dst={len(sliced_dst_kv_ptrs)}."
                )
            return sliced_src_kv_ptrs, sliced_dst_kv_ptrs, len(sliced_src_kv_ptrs)

        # When only speculative-algorithm is enabled for decode
        # the KV has one more layer than prefill.
        # The draft layer needs to be skipped.
        dst_total_layers = (
            min(len(dst_kv_ptrs) // kv_buf_groups, total_kv_layers)
            if total_kv_layers
            else len(dst_kv_ptrs) // kv_buf_groups
        )
        end_layer = start_layer + src_layers
        if src_layers == dst_total_layers:
            sliced_dst_kv_ptrs = dst_kv_ptrs
        else:
            sliced_dst_kv_ptrs = []
            for i in range(kv_buf_groups):
                layer_offset = i * dst_total_layers
                sliced_dst_kv_ptrs.extend(
                    dst_kv_ptrs[layer_offset + start_layer : layer_offset + end_layer]
                )
        layers_current_pp_stage = len(src_kv_ptrs)
        return src_kv_ptrs, sliced_dst_kv_ptrs, layers_current_pp_stage

    def send_kvcache(
        self,
        mooncake_session_id: str,
        prefill_kv_indices: npt.NDArray[np.int32],
        dst_kv_ptrs: list[int],
        dst_kv_indices: npt.NDArray[np.int32],
        executor: concurrent.futures.ThreadPoolExecutor,
        dst_layer_ids: Optional[List[int]] = None,
        dst_device_kv_indices: Optional[npt.NDArray[np.int32]] = None,
    ):
        kv_buf_groups = getattr(self.kv_args, "kv_buf_groups", 1)
        use_sparse_pd_split_indices = (
            dst_device_kv_indices is not None
            and self.is_mla_backend
            and kv_buf_groups == 3
        )
        if dst_device_kv_indices is not None and not use_sparse_pd_split_indices:
            raise NotImplementedError(
                "Ascend KV transfer only supports separate device KV indices "
                "for sparse PD MLA K/V staging plus native index-K."
            )

        # Group by indices
        prefill_kv_blocks, dst_kv_blocks = group_concurrent_contiguous(
            prefill_kv_indices, dst_kv_indices
        )
        device_prefill_kv_blocks, device_dst_kv_blocks = (None, None)
        if use_sparse_pd_split_indices:
            device_prefill_kv_blocks, device_dst_kv_blocks = (
                group_concurrent_contiguous(prefill_kv_indices, dst_device_kv_indices)
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
            use_device_indices: bool = False,
        ) -> List[Tuple[int, int, int]]:
            current_prefill_blocks = prefill_kv_blocks
            current_dst_blocks = dst_kv_blocks
            if use_device_indices:
                current_prefill_blocks = device_prefill_kv_blocks
                current_dst_blocks = device_dst_kv_blocks
                if current_prefill_blocks is None or current_dst_blocks is None:
                    raise RuntimeError(
                        "Sparse PD index-K transfer requires device KV indices."
                    )
            transfer_blocks = []
            for prefill_index, decode_index in zip(
                current_prefill_blocks, current_dst_blocks
            ):
                src_addr = src_ptr + int(prefill_index[0]) * item_len
                dst_addr = dst_ptr + int(decode_index[0]) * item_len
                length = item_len * len(prefill_index)
                transfer_blocks.append((src_addr, dst_addr, length))
            return transfer_blocks

        # Worker function for processing a single layer
        def process_layer(
            layer_idx: int,
            total_layer_entries: int,
            src_ptr: int,
            dst_ptr: int,
            item_len: int,
        ) -> int:
            transfer_blocks = set_transfer_blocks(
                src_ptr,
                dst_ptr,
                item_len,
                _use_device_indices_for_layer(layer_idx, total_layer_entries),
            )
            return self._transfer_data(mooncake_session_id, transfer_blocks)

        # Worker function for processing all layers in a batch
        def process_layers(layers_params: List[Tuple[int, int, int, int]]) -> int:
            transfer_blocks = []
            total_layer_entries = len(layers_params)
            for layer_idx, src_ptr, dst_ptr, item_len in layers_params:
                transfer_blocks.extend(
                    set_transfer_blocks(
                        src_ptr,
                        dst_ptr,
                        item_len,
                        _use_device_indices_for_layer(layer_idx, total_layer_entries),
                    )
                )
            return self._transfer_data(mooncake_session_id, transfer_blocks)

        def _use_device_indices_for_layer(
            layer_idx: int,
            total_layer_entries: int,
        ) -> bool:
            if not use_sparse_pd_split_indices:
                return False
            if total_layer_entries % kv_buf_groups != 0:
                raise RuntimeError(
                    "Sparse PD transfer entries are not divisible by "
                    f"kv_buf_groups={kv_buf_groups}: {total_layer_entries}."
                )
            layers_per_group = total_layer_entries // kv_buf_groups
            return int(layer_idx) >= 2 * layers_per_group

        if self.enable_custom_mem_pool:
            futures = [
                executor.submit(
                    process_layer,
                    layer_idx,
                    len(layers_params),
                    src_ptr,
                    dst_ptr,
                    item_len,
                )
                for (layer_idx, src_ptr, dst_ptr, item_len) in layers_params
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
                        release=True,
                    )
                    logger.debug(
                        "Ascend sparse KV PD staged transfer committed: "
                        "room=%s slot=%s req_pool_idx=%s token_count=%s",
                        metadata.room,
                        metadata.slot_id,
                        metadata.req_pool_idx,
                        metadata.token_count,
                    )
                except Exception as exc:
                    staging.release_room(bootstrap_room)
                    self.record_failure(
                        bootstrap_room,
                        "Failed to offload Ascend sparse KV PD staging buffer "
                        f"to host sparse KV cache: {exc}",
                    )
                    return super().update_status(bootstrap_room, KVPoll.Failed)
            elif status == KVPoll.Failed:
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
        staging = getattr(self.kv_mgr, "sparse_pd_decode_staging", None)
        if staging is not None:
            staging.release_room(self.bootstrap_room)
        return super().abort()


class AscendKVBootstrapServer(MooncakeKVBootstrapServer):
    pass
