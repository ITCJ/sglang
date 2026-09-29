"""SysV registered Host storage and indexed MLA copies (target NPU only)."""

import os
import time
import uuid

from kv_layout import K_DIM, LAYERS, PAGE_SIZE, ROPE_DIM


PATH_NAME = "L2-L1_unidex_sysv_registered"
HOST_MEMORY = "sysv_registered"
SOC = "Ascend910_9382"
UINT32_MAX = (1 << 32) - 1
_INCOMPLETE_ENGINES = []


def configure_soc():
    for key in ("SOC_VERSION", "ASCEND_SOC_VERSION"):
        value = os.environ.get(key)
        if value is not None and value != SOC:
            raise RuntimeError(f"{key} must be {SOC}, got {value!r}")
    os.environ["SOC_VERSION"] = SOC
    os.environ["ASCEND_SOC_VERSION"] = SOC


class UnidexEngine:
    def __init__(self, torch, device_id, block_dim):
        self.torch = torch
        self.device_id = device_id
        self.block_dim = block_dim
        self.stage = "unidex imports"
        from sgl_kernel_npu.sparsity_driven_kv_offload import (
            create_shm_tensor, free_shm, unidex_copy_inplace,
        )

        self.create_shm_tensor = create_shm_tensor
        self.free_shm = free_shm
        self.copy = unidex_copy_inplace
        self.owners = []
        self.plans = []
        self.indices = []
        self.registration_started = False
        self.metadata = dict(
            process_pid=os.getpid(), shm_names=[],
            host_guard_initialization="upstream memset(ptr, 0, size) after successful IPC_RMID",
            sysv_reclamation="upstream IPC_RMID before registration; OS reclaims after last attachment",
            shm_names_are_allocator_keys=True, shm_ids_exposed_by_upstream=False,
            host_unregister_confirmed=None,
        )

    def allocate(self, physical_slots):
        self.stage = "SysV Host allocation and registration"
        start = time.perf_counter()
        self.physical_slots = physical_slots
        allocation_id = uuid.uuid4().hex
        for label, dim in (("k", K_DIM), ("rope", ROPE_DIM)):
            name = f"kv_unidex_{os.getpid()}_{allocation_id}_{label}"
            self.metadata["shm_names"].append(name)
            self.registration_started = True
            tensor, host_ptr, dev_ptr = self.create_shm_tensor(
                (physical_slots, LAYERS, PAGE_SIZE, 1, dim),
                self.torch.bfloat16, device_id=self.device_id,
                name=name,
            )
            self.owners.append((tensor, host_ptr, dev_ptr))
            if host_ptr <= 0 or dev_ptr <= 0 or tensor.data_ptr() != host_ptr:
                raise RuntimeError("invalid SysV Host/device alias")
        self.host_k, self.host_rope = (owner[0] for owner in self.owners)
        self.metadata.update(
            host_allocation_s=time.perf_counter() - start,
            host_allocation_blocks=[
                dict(component=label, first_physical_page=0,
                     physical_pages=physical_slots, bytes=tensor.numel() * tensor.element_size())
                for label, (tensor, _, _) in zip(("k", "rope"), self.owners)
            ],
            allocation_size_abi="Torch int64 -> allocator uint64 -> arm64 size_t; no int size cast",
            host_allocation_chunked=False, soc_version=SOC, block_dim=self.block_dim,
        )

    def prepare_indices(self, pages, slots, device_k, device_rope):
        self.stage = "unidex fixed mapping and index preparation"
        start = time.perf_counter()
        if len(pages) != len(slots) or len(set(pages)) != len(pages):
            raise RuntimeError("invalid logical page coverage")
        if list(pages) != list(range(len(pages))) or len(set(slots)) != len(slots):
            raise RuntimeError("page coverage/unique destination check failed")
        if any(slot <= 0 or slot >= self.physical_slots for slot in slots):
            raise RuntimeError("page index outside allocation or reserved page")
        for tensor, dim in ((device_k, K_DIM), (device_rope, ROPE_DIM)):
            if tuple(tensor.shape) != (LAYERS, self.physical_slots, PAGE_SIZE, 1, dim):
                raise RuntimeError("NPU MLA layout mismatch")
            if tensor[0].numel() * tensor.element_size() > UINT32_MAX:
                raise RuntimeError("single-layer destination exceeds uint32 extent")

        # Both components use the same complete physical-page view boundaries.
        chunk_pages = UINT32_MAX // (LAYERS * PAGE_SIZE * K_DIM * 2)
        covered = []
        views = []
        for first in range(0, self.physical_slots, chunk_pages):
            end = min(self.physical_slots, first + chunk_pages)
            selected = [slot for slot in slots if first <= slot < end]
            if not selected:
                continue
            covered.extend(selected)
            dst_rows = [slot * PAGE_SIZE + token for slot in selected for token in range(PAGE_SIZE)]
            if len(set(dst_rows)) != len(dst_rows) or max(dst_rows) >= self.physical_slots * PAGE_SIZE:
                raise RuntimeError("destination row bounds/uniqueness check failed")
            dst_index = self.torch.tensor(dst_rows, dtype=self.torch.int64, device="npu")
            mask = self.torch.ones(len(dst_rows), dtype=self.torch.bool, device="npu")
            self.indices.extend((dst_index, mask))
            for layer in range(LAYERS):
                src_rows = [((slot - first) * LAYERS + layer) * PAGE_SIZE + token
                            for slot in selected for token in range(PAGE_SIZE)]
                if (len(src_rows) != len(dst_rows) or min(src_rows) < 0
                        or max(src_rows) >= (end - first) * LAYERS * PAGE_SIZE):
                    raise RuntimeError("source row bounds/coverage check failed")
                src_index = self.torch.tensor(src_rows, dtype=self.torch.int64, device="npu")
                self.indices.append(src_index)
                for (host, _, alias), destination, dim in zip(
                    self.owners, (device_k, device_rope), (K_DIM, ROPE_DIM),
                ):
                    source = host[first:end]
                    extent = source.numel() * source.element_size()
                    offset = first * LAYERS * PAGE_SIZE * dim * 2
                    if (not source.is_contiguous() or extent > UINT32_MAX
                            or source.data_ptr() != host.data_ptr() + offset):
                        raise RuntimeError("invalid zero-copy Host view/alias extent")
                    self.plans.append((source, destination[layer], src_index, dst_index, mask, alias + offset))
            views.append(dict(first_physical_page=first, physical_pages=end - first,
                              selected_pages=len(selected),
                              k_bytes=(end - first) * LAYERS * PAGE_SIZE * K_DIM * 2,
                              rope_bytes=(end - first) * LAYERS * PAGE_SIZE * ROPE_DIM * 2))
        if sorted(covered) != sorted(slots):
            raise RuntimeError("physical views did not cover the complete logical request")
        self.torch.npu.synchronize()
        self.metadata.update(
            index_prepare_s=time.perf_counter() - start,
            index_bytes=sum(t.numel() * t.element_size() for t in self.indices),
            launch_count=len(self.plans), host_views=views,
            index_checks=dict(bounds=True, coverage=True, unique_destination=True),
            index_preparation="fixed mapping prepared/uploaded once outside timed samples",
            logical_batches=1, copy_row="one token: K=1024 bytes, RoPE=128 bytes",
        )

    def submit(self):
        self.stage = "unidex whole-request submission/completion"
        for source, destination, src_index, dst_index, mask, alias in self.plans:
            self.copy(source, destination, src_index, dst_index, mask,
                      src_address_ndims=3, dst_address_ndims=2,
                      block_dim=self.block_dim, src_ptr=alias)

    def close(self):
        if not self.registration_started:
            return
        # Preserve every owner and alias if completion cannot be established.
        try:
            self.torch.npu.synchronize()
        except BaseException:
            _INCOMPLETE_ENGINES.append(self)
            self.metadata["cleanup"] = "completion not established; free_shm not called; owners retained"
            raise
        self.plans.clear()
        self.indices.clear()
        self.free_shm(self.device_id)
        self.metadata["cleanup"] = "completion synchronized; free_shm call returned; unregister/detach not confirmed"
        self.owners.clear()
        self.host_k = self.host_rope = None
        self.registration_started = False
