"""Indexed copies from BM Host mappings into the existing MLA L1 layout."""

import mmap
import time

from check import K_DIM, LAYERS, PAGE_BYTES, PAGE_SIZE, ROPE_DIM
from unidex_engine import UINT32_MAX, configure_soc


PATHS = {"H": "L2-L1_UNIDEX_bm_host", "I": "L3-L1_UNIDEX_bm_remote_host"}
K_PAGE = LAYERS * PAGE_SIZE * K_DIM * 2
ROPE_PAGE = PAGE_BYTES - K_PAGE
MAX_PAGES = 131072 // PAGE_SIZE


def wait_device_mapping(handle, bm, gva, timeout, label):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        address = handle.gva_to_va(gva, bm.BmMemType.LOCAL_DEVICE)
        if address and int(address) > 0:
            return int(address)
        time.sleep(0.2)
    raise TimeoutError(f"{label} GVA has no LOCAL_DEVICE mapping after {timeout}s")


class BmUnidexPlan:
    def __init__(self, handle, bm, torch, source_gva, local_gva, batch,
                 physical_slots, device_k, device_rope, block_dim, timeout,
                 owner_registry):
        owner_registry.append(self)
        self.device_owners = (device_k, device_rope)
        configure_soc()
        from sgl_kernel_npu.sparsity_driven_kv_offload import unidex_copy_inplace

        self.copy = unidex_copy_inplace
        self.torch = torch
        self.block_dim = block_dim
        self.plans = {"H": [], "I": []}
        self.keepalive = []
        self.metadata = {}
        self.metadata_reserved_bytes = {"H": 0, "I": 0}
        if not hasattr(handle, "gva_to_va") or not hasattr(bm.BmMemType, "LOCAL_DEVICE"):
            raise RuntimeError("installed BM lacks gva_to_va(..., BmMemType.LOCAL_DEVICE)")
        if (len(batch["pages"]) != len(batch["slots"])
                or batch["pages"] != list(range(len(batch["pages"])))
                or len(set(batch["slots"])) != len(batch["slots"])):
            raise RuntimeError("invalid logical page coverage or duplicate destination")
        if any(slot <= 0 or slot >= physical_slots for slot in batch["slots"]):
            raise RuntimeError("invalid destination physical page")
        for dest, dim in ((device_k, K_DIM), (device_rope, ROPE_DIM)):
            if tuple(dest.shape) != (LAYERS, physical_slots, PAGE_SIZE, 1, dim):
                raise RuntimeError("unexpected MLA L1 layout")
            if dest[0].numel() * dest.element_size() > UINT32_MAX:
                raise RuntimeError("one L1 layer exceeds UNIDEX uint32 address range")

        start = time.perf_counter()
        self.remote_lva = wait_device_mapping(handle, bm, source_gva, timeout, "remote Host")
        self.local_lva = wait_device_mapping(handle, bm, local_gva, timeout, "local BM Host")
        self.metadata["bm_mapping_s"] = time.perf_counter() - start
        pool_bytes = int(handle.local_mem_size(bm.BmMemType.HOST))
        if pool_bytes < (3 * MAX_PAGES * PAGE_BYTES):
            raise RuntimeError("BM Host pool cannot address all remote source pages")
        start = time.perf_counter()
        before = len(self.keepalive)
        self._build_remote(batch, physical_slots, device_k, device_rope, pool_bytes)
        remote_index_bytes = self._index_bytes(self.keepalive[before:])
        before = len(self.keepalive)
        self._build_local(batch, physical_slots, device_k, device_rope)
        local_index_bytes = self._index_bytes(self.keepalive[before:])
        torch.npu.synchronize()
        self.metadata.update(
            index_prepare_s=time.perf_counter() - start,
            index_prepare_scope="both BM-local and BM-remote UNIDEX plans",
            index_bytes_by_path={"H": local_index_bytes, "I": remote_index_bytes},
            launch_count_by_path={code: len(plans) for code, plans in self.plans.items()},
            source_metadata="anonymous mmap-backed CPU tensor views; virtual address space reserved, no payload copied",
            source_metadata_virtual_bytes_by_path=self.metadata_reserved_bytes,
            source_metadata_page_touch="no explicit CPU read/write of metadata payload; physical page faults not measured",
            index_preparation="fixed request mapping, uploaded before timed samples",
            block_dim=block_dim,
        )

    def _index_bytes(self, owners):
        return sum(t.numel() * t.element_size() for t in owners
                   if isinstance(t, self.torch.Tensor) and t.device.type == "npu")

    def _append(self, code, source_base, selected, first_page, page_stride,
                component_offset, dim, destination, physical_slots):
        torch = self.torch
        row_bytes = dim * 2
        extent = page_stride * (max(page for page, _ in selected) - first_page + 1)
        if extent > UINT32_MAX or extent % row_bytes:
            raise RuntimeError("source view exceeds uint32 extent or row alignment")
        metadata_mapping = mmap.mmap(-1, extent, access=mmap.ACCESS_WRITE)
        try:
            source = torch.frombuffer(metadata_mapping, dtype=torch.bfloat16).view(
                extent // row_bytes, 1, dim)
        except BaseException:
            metadata_mapping.close()
            raise
        self.keepalive.extend((metadata_mapping, source))
        self.metadata_reserved_bytes[code] += extent
        dst_rows = [slot * PAGE_SIZE + token for _, slot in selected for token in range(PAGE_SIZE)]
        if len(set(dst_rows)) != len(dst_rows) or max(dst_rows) >= physical_slots * PAGE_SIZE:
            raise RuntimeError("UNIDEX destination rows are not unique and in range")
        dst_index = torch.tensor(dst_rows, dtype=torch.int64, device="npu")
        mask = torch.ones(len(dst_rows), dtype=torch.bool, device="npu")
        self.keepalive.extend((dst_index, mask))
        for layer in range(LAYERS):
            src_rows = [((page - first_page) * page_stride + component_offset
                         + layer * PAGE_SIZE * row_bytes) // row_bytes + token
                        for page, _ in selected for token in range(PAGE_SIZE)]
            if (min(src_rows) < 0 or max(src_rows) >= extent // row_bytes
                    or any((page_stride * (page - first_page) + component_offset
                            + layer * PAGE_SIZE * row_bytes) % row_bytes
                           for page, _ in selected)):
                raise RuntimeError("UNIDEX source row outside mapped view")
            src_index = torch.tensor(src_rows, dtype=torch.int64, device="npu")
            self.keepalive.append(src_index)
            self.plans[code].append((source, destination[layer], src_index, dst_index, mask, source_base))

    def _build_remote(self, batch, physical_slots, device_k, device_rope, pool_bytes):
        # Each remote physical page contains all K layers followed by all RoPE layers.
        chunk_pages = UINT32_MAX // PAGE_BYTES
        groups = {}
        for page, slot in zip(batch["pages"], batch["slots"]):
            source_page = page if batch["layout"] == "contiguous" else MAX_PAGES + 1 + 2 * page
            if (source_page + 1) * PAGE_BYTES > pool_bytes:
                raise RuntimeError("remote page outside BM Host pool")
            groups.setdefault(source_page // chunk_pages, []).append((source_page, slot))
        for group, selected in groups.items():
            first = group * chunk_pages
            base = self.remote_lva + first * PAGE_BYTES
            self._append("I", base, selected, first, PAGE_BYTES, 0, K_DIM,
                         device_k, physical_slots)
            self._append("I", base, selected, first, PAGE_BYTES, K_PAGE, ROPE_DIM,
                         device_rope, physical_slots)

    def _build_local(self, batch, physical_slots, device_k, device_rope):
        # The client's BM Host contribution has separate packed K and RoPE planes.
        chunk_pages = UINT32_MAX // K_PAGE
        groups = {}
        for slot in batch["slots"]:
            groups.setdefault(slot // chunk_pages, []).append((slot, slot))
        for group, selected in groups.items():
            first = group * chunk_pages
            self._append("H", self.local_lva + first * K_PAGE, selected, first,
                         K_PAGE, 0, K_DIM, device_k, physical_slots)
            self._append("H", self.local_lva + physical_slots * K_PAGE + first * ROPE_PAGE,
                         selected, first, ROPE_PAGE, 0, ROPE_DIM, device_rope, physical_slots)

    def submit(self, code):
        for source, destination, src_index, dst_index, mask, src_ptr in self.plans[code]:
            self.copy(source, destination, src_index, dst_index, mask,
                      src_address_ndims=1, dst_address_ndims=2,
                      block_dim=self.block_dim, src_ptr=src_ptr)
