"""Ticket01 wire/config contracts; independent of the NPU and HTTP runtime."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import msgspec

from .mempool_protocol import WIRE_TAG, MempoolMessage, decode_message, encode_message

ROLES = ("prefill", "decode")
PRODUCERS = (0, 1, 2, *range(6, 75, 4))
CONFIG_ENV = "SGLANG_NPU_MEMPOOL_ROUTER_CONFIG"
RUN_ENV = "SGLANG_NPU_MEMPOOL_ROUTER_RUN_ID"
# Supported by this GLM-5.2 mempool-min integration, not a claim about every
# Ascend attention backend. Extend only with matching pool/kernel acceptance.
SUPPORTED_PAGE_SIZES = (64, 128)


class RouterConfig(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    prefill_host: str
    decode_host: str
    model_path: str
    interface: str = "bond4"
    numa_nodes: tuple[int, ...] = tuple(range(8))
    main_port: int = 6699
    control_port: int = 16700
    runner_port: int = 18891
    prefill_port: int = 8000
    decode_port: int = 8001
    bootstrap_port: int = 8995
    prompt_capacity: int = 8192
    decode_capacity: int = 128
    page_size: int = 128
    context_length: int = 8448
    max_total_tokens: int = 16384
    operation_timeout: float = 600.0
    startup_timeout: float = 7200.0
    prefix_pages: int = 0
    verify_prefix: bool = False
    verify_decode: bool = False
    match_prefix: bool = False
    prefix_read_assignment: str = "auto"
    prefix_shared: bool = False
    prefix_start_segment: int | None = None
    prefix_admit_mode: str = "owners"
    prefix_admit_rank: int = 0
    pool_copy: bool = False
    pool_copy_sdk_build_id: str = ""
    pool_copy_bind_cpu: bool = True
    pool_copy_delay_seconds: float = 0.0
    prefill_shared: bool = False
    prefill_groups: tuple[tuple[int, ...], ...] = tuple(
        (r, r + 1) for r in range(0, 16, 2)
    )
    prefill_write_assignment: str = "contiguous"
    prefill_index_shared: bool = False
    prefill_index_writers: tuple[int, ...] = (0,)
    decode_shared: bool = False
    decode_groups: tuple[tuple[int, ...], ...] = tuple(
        (r, r + 1) for r in range(0, 16, 2)
    )
    decode_write_assignment: str = "features"

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.pool_copy_delay_seconds)
            or self.pool_copy_delay_seconds < 0
        ):
            raise ValueError("pool copy test delay must be finite and nonnegative")
        if self.prefix_shared and not (
            self.prefill_shared and self.prefill_index_shared
        ):
            raise ValueError("shared prefix requires shared P main and Index K")
        if (
            self.prefix_admit_mode not in ("owners", "single_rank")
            or not 0 <= self.prefix_admit_rank < 16
        ):
            raise ValueError("invalid prefix admit executor")
        if self.prefill_index_shared and not self.prefix_pages:
            raise ValueError("shared Index K requires prefix storage")
        if self.prefill_index_writers not in ((0,), (0, 1)):
            raise ValueError("prefill_index_writers must be (0,) or (0, 1)")
        if self.decode_write_assignment not in ("features", "layers"):
            raise ValueError("decode_write_assignment must be features or layers")
        if not all(self.decode_groups) or sorted(
            r for g in self.decode_groups for r in g
        ) != list(range(16)):
            raise ValueError(
                "decode_groups must cover each TP rank exactly once; first member is owner"
            )
        if (
            self.prefill_shared
            and self.decode_shared
            and self.prefill_groups != self.decode_groups
        ):
            raise ValueError("shared P/D must use the same BM session groups")
        if self.prefill_write_assignment not in ("contiguous", "round_robin"):
            raise ValueError(
                "prefill_write_assignment must be contiguous or round_robin"
            )
        if not all(self.prefill_groups) or sorted(
            r for group in self.prefill_groups for r in group
        ) != list(range(16)):
            raise ValueError(
                "prefill_groups must cover each TP rank exactly once; first member is owner"
            )
        if self.prefix_shared and self.prefix_pages % len(self.prefill_groups):
            raise ValueError("prefix pages must divide evenly across segments")
        if (
            self.prefix_start_segment is not None
            and not 0 <= self.prefix_start_segment < len(self.prefill_groups)
        ):
            raise ValueError("prefix_start_segment must name a segment")
        if self.prefix_read_assignment not in (
            "auto",
            "by_segment",
            "round_robin",
            "contiguous",
        ):
            raise ValueError("invalid prefix_read_assignment")
        if self.prefix_read_assignment == "by_segment" and not self.prefix_shared:
            raise ValueError("by_segment requires shared prefix")
        if type(self.match_prefix) is not bool or (
            self.match_prefix and not self.prefix_pages
        ):
            raise ValueError("match_prefix requires enabled prefix storage")
        if type(self.prefix_pages) is not int or self.prefix_pages not in (
            0,
            40 if self.page_size == 128 else 80,
        ):
            raise ValueError("prefix_pages must be 0 or hold 5120 tokens")
        if type(self.verify_prefix) is not bool or (
            self.verify_prefix and not self.prefix_pages
        ):
            raise ValueError("verify_prefix requires enabled prefix storage")
        if (
            not self.prefill_host
            or not self.decode_host
            or self.prefill_host == self.decode_host
        ):
            raise ValueError("Router requires two distinct P/D hosts")
        if not self.model_path or not self.interface or not self.numa_nodes:
            raise ValueError("model, network interface and NUMA nodes are required")
        if (
            tuple(sorted(set(self.numa_nodes))) != self.numa_nodes
            or min(self.numa_nodes) < 0
        ):
            raise ValueError("NUMA nodes must be sorted, unique and nonnegative")
        if (
            type(self.page_size) is not int
            or self.page_size not in SUPPORTED_PAGE_SIZES
        ):
            raise ValueError(
                f"GLM-5.2 mempool-min supports page_size in {SUPPORTED_PAGE_SIZES}"
            )
        if self.prompt_capacity != 8192 or self.decode_capacity not in (128, 512):
            raise ValueError("mempool-min requires S_P=8192 and S_D in (128, 512)")
        if any(
            n % self.page_size for n in (self.prompt_capacity, self.decode_capacity)
        ):
            raise ValueError("alive slot capacities must be multiples of page_size")
        # TpModelWorker and Scheduler each retain one position. Reserve both
        # margins so an 8192+128 request is not silently clipped to 126 tokens.
        if (
            self.context_length < self.prompt_capacity + self.decode_capacity + 2
            or self.max_total_tokens < self.context_length
        ):
            raise ValueError("native capacity must cover both scheduler margins")
        for port in (
            self.main_port,
            self.control_port,
            self.runner_port,
            self.prefill_port,
            self.decode_port,
            self.bootstrap_port,
        ):
            if not 1 <= port <= 65535:
                raise ValueError("invalid Router deployment port")
        for timeout in (self.operation_timeout, self.startup_timeout):
            if not math.isfinite(timeout) or timeout <= 0:
                raise ValueError("timeouts must be finite and positive")

    @classmethod
    def read(cls, path: str) -> RouterConfig:
        return msgspec.json.decode(Path(path).read_bytes(), type=cls)

    def replicas(self, role: str = "prefill") -> dict[str, int]:
        enabled = 16 if self.prefix_pages else 0
        return dict(
            alive_main=len(self.main_groups(role)),
            alive_index=1 if self.prefill_index_shared else enabled,
            prefix_main=1 if self.prefix_shared else enabled,
            prefix_index=1 if self.prefix_shared else enabled,
        )

    @property
    def read_assignment(self) -> str:
        if self.prefix_read_assignment == "auto":
            return "by_segment" if self.prefix_shared else "round_robin"
        return self.prefix_read_assignment

    def prefix_policy(self) -> dict[str, Any]:
        return dict(
            pool_copy=self.pool_copy,
            pool_copy_sdk_build_id=self.pool_copy_sdk_build_id,
            pool_copy_bind_cpu=self.pool_copy_bind_cpu,
            pool_copy_delay_seconds=self.pool_copy_delay_seconds,
            admit_mode=self.prefix_admit_mode,
            admit_rank=self.prefix_admit_rank,
            start_segment=self.prefix_start_segment,
            read_assignment=self.read_assignment,
            main_write_assignment=self.prefill_write_assignment,
            groups=[list(group) for group in self.prefill_groups],
            index_writers=list(self.prefill_index_writers),
        )

    def main_groups(self, role: str) -> tuple[tuple[int, ...], ...]:
        if role == "prefill" and self.prefill_shared:
            return self.prefill_groups
        if role == "decode" and self.decode_shared:
            return self.decode_groups
        return tuple((r,) for r in range(16))

    @property
    def session_groups(self) -> tuple[tuple[int, ...], ...]:
        return self.prefill_groups if self.prefill_shared else self.decode_groups

    def backend(self, role: str) -> str:
        host, port = (
            (self.prefill_host, self.prefill_port)
            if role == "prefill"
            else (self.decode_host, self.decode_port)
        )
        return f"http://{host}:{port}"

    @property
    def control_endpoint(self) -> str:
        return f"tcp://{self.prefill_host}:{self.control_port}"


def native_wire(message: MempoolMessage) -> dict[str, Any]:
    return json.loads(encode_message(message)[1])  # type: ignore[no-any-return]


def native_message(value: dict[str, Any]) -> MempoolMessage:
    return decode_message([WIRE_TAG, json.dumps(value).encode()])


def model_digest(path: str) -> str:
    """Same content identity on both hosts, without copying model weights."""
    root = Path(path)
    files = sorted(
        {
            p
            for pattern in (
                "config.json",
                "quant_model_description.json",
                "tokenizer*",
                "special_tokens_map.json",
                "added_tokens.json",
                "chat_template*",
                "generation_config.json",
                "vocab.*",
                "merges.txt",
            )
            for p in root.glob(pattern)
            if p.is_file()
        }
    )
    if not (root / "config.json").is_file():
        raise ValueError("model config is missing")
    digest = hashlib.sha256()
    for file in files:
        digest.update(file.name.encode())
        digest.update(hashlib.sha256(file.read_bytes()).digest())
    return digest.hexdigest()


def page_hashes(tokens: list[int], page_size: int) -> list[str]:
    """Use SGLang's storage hash chain including the empty namespace seed."""
    # Lazy: importing mem_cache also loads model/device integrations. The CPU
    # seam injects a hasher; the service always uses this production function.
    from sglang.srt.mem_cache.utils import get_hash_str, storage_namespace_seed

    if type(page_size) is not int or page_size <= 0 or len(tokens) % page_size:
        raise ValueError("page hashes require complete token pages")
    return get_hash_str(tokens, storage_namespace_seed("", ""), page_size=page_size)  # type: ignore[no-any-return]
