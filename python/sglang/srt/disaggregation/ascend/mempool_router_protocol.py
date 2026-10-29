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
SUPPORTED_PAGE_SIZES = (128,)


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
    match_prefix: bool = False
    prefix_read_assignment: str = "round_robin"
    # Temporary K1-03 diagnostic; remove after user acceptance.
    profile_prefix: bool = False

    def __post_init__(self) -> None:
        if self.prefix_read_assignment not in ("round_robin", "contiguous"):
            raise ValueError("prefix_read_assignment must be round_robin or contiguous")
        if type(self.match_prefix) is not bool or (
            self.match_prefix and not self.prefix_pages
        ):
            raise ValueError("match_prefix requires enabled prefix storage")
        if type(self.prefix_pages) is not int or self.prefix_pages not in (0, 40):
            raise ValueError("prefix_pages must be 0 or 40")
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
        if (self.prompt_capacity, self.decode_capacity) != (
            8192,
            128,
        ):
            raise ValueError("ticket01 requires S_P=8192 and S_D=128")
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

    def replicas(self) -> dict[str, int]:
        enabled = 16 if self.prefix_pages else 0
        return dict(
            alive_main=16,
            alive_index=enabled,
            prefix_main=enabled,
            prefix_index=enabled,
        )

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
