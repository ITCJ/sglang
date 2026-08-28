#!/usr/bin/env python3
"""Controlled client for dense three-tier KV experiment 0."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


PAGE_SIZE = 64
THEORETICAL_BF16_MLA_BYTES_PER_TOKEN = 70_272
EXPECTED_MOONCAKE_STORE_BYTES = 640_000_000_000
DEFAULT_IDLE_TIMEOUT_S = 600.0
DEFAULT_REQUEST_TIMEOUT_S = 3600.0
HOST_POOL_RE = re.compile(
    r"Allocating\s+(?P<label>\S+)\s+hierarchical KV host pool:\s+"
    r"(?P<tokens>\d+) tokens,\s+(?P<gb>[0-9.]+) GB host memory"
)


class ExperimentError(RuntimeError):
    pass


class SkipExperiment(ExperimentError):
    pass


class SummaryError(ExperimentError):
    pass


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def align_up(value: int, alignment: int) -> int:
    return ((value + alignment - 1) // alignment) * alignment


def align_down(value: int, alignment: int) -> int:
    return (value // alignment) * alignment


def cacheable_tokens(input_len: int, page_size: int = PAGE_SIZE) -> int:
    """SGLang keeps one prompt token uncached so it can produce the next token."""
    return align_down(max(0, input_len - 1), page_size)


def _headers(api_key: str | None = None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _url(base_url: str, path: str, query: dict[str, Any] | None = None) -> str:
    result = base_url.rstrip("/") + path
    if query:
        result += "?" + urllib.parse.urlencode(query)
    return result


def request_json(
    base_url: str,
    path: str,
    *,
    api_key: str | None = None,
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    request = urllib.request.Request(
        _url(base_url, path), headers=_headers(api_key), method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise ExperimentError(f"GET {path} failed ({exc.code}): {body}") from exc
    except (TimeoutError, urllib.error.URLError) as exc:
        raise ExperimentError(f"GET {path} failed: {exc}") from exc


def post_control(
    base_url: str,
    path: str,
    *,
    query: dict[str, Any] | None = None,
    api_key: str | None = None,
    timeout_s: float = DEFAULT_IDLE_TIMEOUT_S,
) -> str:
    request = urllib.request.Request(
        _url(base_url, path, query),
        data=b"",
        headers=_headers(api_key),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s + 30.0) as response:
            return response.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise ExperimentError(f"POST {path} failed ({exc.code}): {body}") from exc
    except (TimeoutError, urllib.error.URLError) as exc:
        raise ExperimentError(f"POST {path} failed: {exc}") from exc


def wait_until_idle(
    base_url: str,
    *,
    api_key: str | None,
    timeout_s: float = DEFAULT_IDLE_TIMEOUT_S,
) -> None:
    post_control(
        base_url,
        "/wait_until_idle",
        query={"timeout": timeout_s},
        api_key=api_key,
        timeout_s=timeout_s,
    )


def reset_all_caches(
    base_url: str,
    *,
    api_key: str | None,
    timeout_s: float = DEFAULT_IDLE_TIMEOUT_S,
) -> None:
    wait_until_idle(base_url, api_key=api_key, timeout_s=timeout_s)
    post_control(
        base_url,
        "/hicache/storage-backend/clear",
        api_key=api_key,
        timeout_s=timeout_s,
    )
    post_control(
        base_url,
        "/flush_cache",
        query={"timeout": timeout_s},
        api_key=api_key,
        timeout_s=timeout_s,
    )


def flush_local_caches(
    base_url: str,
    *,
    api_key: str | None,
    timeout_s: float = DEFAULT_IDLE_TIMEOUT_S,
) -> None:
    """Clear L1/L2 after persistence has drained, leaving external L3 intact."""
    wait_until_idle(base_url, api_key=api_key, timeout_s=timeout_s)
    post_control(
        base_url,
        "/flush_cache",
        query={"timeout": timeout_s},
        api_key=api_key,
        timeout_s=timeout_s,
    )


def send_generate(
    base_url: str,
    input_ids: list[int],
    *,
    rid: str,
    api_key: str | None,
    timeout_s: float,
) -> dict[str, Any]:
    payload = {
        "rid": rid,
        "input_ids": input_ids,
        "sampling_params": {
            "temperature": 0,
            "max_new_tokens": 1,
            "ignore_eos": True,
        },
        "stream": True,
        "routed_dp_rank": 0,
    }
    body = canonical_json(payload)
    request = urllib.request.Request(
        _url(base_url, "/generate"),
        data=body,
        headers=_headers(api_key),
        method="POST",
    )

    send_start_ns = time.perf_counter_ns()
    first_nonempty_ns: int | None = None
    response_end_ns: int | None = None
    meta_info: dict[str, Any] = {}
    generated_text = ""
    status = 0
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            status = response.status
            for raw_line in response:
                line = raw_line.strip()
                if not line:
                    continue
                if line.startswith(b"data: "):
                    line = line[6:]
                if line == b"[DONE]":
                    continue
                event = json.loads(line)
                event_meta = event.get("meta_info") or {}
                meta_info.update(event_meta)
                text = event.get("text")
                if text:
                    if first_nonempty_ns is None:
                        first_nonempty_ns = time.perf_counter_ns()
                    generated_text = text
            response_end_ns = time.perf_counter_ns()
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode(errors="replace")
        raise ExperimentError(
            f"generate {rid} failed ({exc.code}): {error_body}"
        ) from exc
    except (TimeoutError, urllib.error.URLError) as exc:
        raise ExperimentError(f"generate {rid} failed: {exc}") from exc

    if status != 200:
        raise ExperimentError(f"generate {rid} returned HTTP {status}")
    if first_nonempty_ns is None:
        raise ExperimentError(f"generate {rid} produced no non-empty token")
    if int(meta_info.get("prompt_tokens", -1)) != len(input_ids):
        raise ExperimentError(
            f"generate {rid} prompt_tokens={meta_info.get('prompt_tokens')} "
            f"but sent {len(input_ids)}"
        )
    if int(meta_info.get("completion_tokens", -1)) != 1:
        raise ExperimentError(
            f"generate {rid} completion_tokens={meta_info.get('completion_tokens')}"
        )

    return {
        "rid": rid,
        "http_status": status,
        "send_start_ns": send_start_ns,
        "first_nonempty_token_ns": first_nonempty_ns,
        "response_end_ns": response_end_ns,
        "ttft_ms": (first_nonempty_ns - send_start_ns) / 1_000_000.0,
        "latency_ms": (response_end_ns - send_start_ns) / 1_000_000.0,
        "generated_text": generated_text,
        "prompt_tokens": int(meta_info["prompt_tokens"]),
        "completion_tokens": int(meta_info["completion_tokens"]),
        "cached_tokens": int(meta_info.get("cached_tokens", 0)),
        "cached_tokens_details": meta_info.get("cached_tokens_details") or {},
    }


def validate_cache_source(
    response: dict[str, Any], tier: str, min_cached_tokens: int
) -> dict[str, int | str]:
    raw = response.get("cached_tokens_details") or {}
    details: dict[str, int | str] = {
        "device": int(raw.get("device", 0)),
        "host": int(raw.get("host", 0)),
        "storage": int(raw.get("storage", 0)),
        "storage_backend": str(raw.get("storage_backend", "")),
    }
    device = int(details["device"])
    host = int(details["host"])
    storage = int(details["storage"])

    if tier == "l2":
        accepted = device == 0 and storage == 0 and host >= min_cached_tokens
        expected = (
            f"device=0, storage=0, host>={min_cached_tokens}; got {details}"
        )
    elif tier == "l3":
        backend = str(details["storage_backend"]).lower()
        accepted = (
            device == 0
            and host == 0
            and storage >= min_cached_tokens
            and "mooncake" in backend
        )
        expected = (
            f"device=0, host=0, storage>={min_cached_tokens}, "
            f"Mooncake backend; got {details}"
        )
    else:
        raise ValueError(f"unsupported tier: {tier}")

    if not accepted:
        raise ExperimentError(f"{tier.upper()} admission failed: {expected}")
    return details


def build_manifest(
    *,
    tokenizer_path: str,
    revision: str | None,
    prefix_len: int,
    question_len: int,
    num_prefixes: int,
    seed: int,
    trust_remote_code: bool,
) -> dict[str, Any]:
    if not revision:
        raise ExperimentError("a fixed tokenizer/model --revision is required")
    try:
        from transformers import AutoTokenizer
    except ImportError as exc:
        raise ExperimentError(
            "transformers is required only for build-manifest; run this in the "
            "SGLang runtime environment"
        ) from exc

    tokenizer = AutoTokenizer.from_pretrained(
        tokenizer_path,
        revision=revision,
        trust_remote_code=trust_remote_code,
    )
    special_ids = {int(value) for value in tokenizer.all_special_ids}
    vocab_size = int(getattr(tokenizer, "vocab_size", 0) or 0)
    token_ids = sorted(
        {
            int(value)
            for value in tokenizer.get_vocab().values()
            if int(value) >= 0
            and int(value) not in special_ids
            and (vocab_size <= 0 or int(value) < vocab_size)
        }
    )
    if len(token_ids) < 256:
        raise ExperimentError(f"only {len(token_ids)} non-special token IDs found")

    rng = random.Random(seed)
    prompt_len = prefix_len + question_len
    prefixes = []
    first_pages: set[tuple[int, ...]] = set()
    for index in range(num_prefixes):
        while True:
            input_ids = [
                token_ids[rng.randrange(len(token_ids))] for _ in range(prompt_len)
            ]
            first_page = tuple(input_ids[:PAGE_SIZE])
            if first_page not in first_pages:
                first_pages.add(first_page)
                break
        prefixes.append(
            {
                "prefix_id": f"p{index:02d}",
                "input_ids": input_ids,
                "input_sha256": sha256_json(input_ids),
            }
        )

    pool_rng = random.Random(seed ^ 0x5EED5EED)
    filler_pool = pool_rng.sample(token_ids, min(8192, len(token_ids)))
    manifest = {
        "schema_version": 1,
        "seed": seed,
        "tokenizer_path": tokenizer_path,
        "tokenizer_revision": revision,
        "page_size": PAGE_SIZE,
        "prefix_len": prefix_len,
        "question_len": question_len,
        "prompt_len": prompt_len,
        "num_prefixes": num_prefixes,
        "filler_token_pool": filler_pool,
        "prefixes": prefixes,
    }
    manifest["manifest_sha256"] = sha256_json(manifest)
    return manifest


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(manifest) + b"\n")


def load_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_bytes())
    expected_hash = manifest.get("manifest_sha256")
    unhashed = {
        key: value for key, value in manifest.items() if key != "manifest_sha256"
    }
    actual_hash = sha256_json(unhashed)
    if expected_hash != actual_hash:
        raise ExperimentError(
            f"manifest hash mismatch: recorded={expected_hash}, actual={actual_hash}"
        )
    if int(manifest.get("page_size", 0)) != PAGE_SIZE:
        raise ExperimentError(f"manifest page_size must be {PAGE_SIZE}")
    prefixes = manifest.get("prefixes") or []
    if len(prefixes) != int(manifest.get("num_prefixes", -1)):
        raise ExperimentError("manifest prefix count mismatch")
    for prefix in prefixes:
        input_ids = prefix.get("input_ids") or []
        if len(input_ids) != int(manifest["prompt_len"]):
            raise ExperimentError(f"bad prompt length for {prefix.get('prefix_id')}")
        if sha256_json(input_ids) != prefix.get("input_sha256"):
            raise ExperimentError(f"input hash mismatch for {prefix.get('prefix_id')}")
    return manifest


def parse_l2_capacity_tokens(server_log: Path) -> int:
    matches: list[tuple[str, int, float]] = []
    for match in HOST_POOL_RE.finditer(server_log.read_text(errors="replace")):
        matches.append(
            (
                match.group("label").lower(),
                int(match.group("tokens")),
                float(match.group("gb")),
            )
        )
    kv_matches = [item for item in matches if item[0] in {"kv", "mla"}]
    selected = kv_matches or matches
    if not selected:
        raise ExperimentError(
            f"could not find HiCache host-pool allocation in {server_log}"
        )
    return min(item[1] for item in selected)


def _parse_mooncake_size(value: Any) -> int:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized.endswith("gb"):
            number = normalized[:-2].strip()
            if not number:
                raise ExperimentError("invalid Mooncake global_segment_size")
            return int(number) * 1024 * 1024 * 1024
        return int(normalized)
    return int(value)


def validate_mooncake_configs(
    worker_path: Path, store_path: Path, l3_placement: str | None = None
) -> dict[str, Any]:
    worker = json.loads(worker_path.read_bytes())
    store = json.loads(store_path.read_bytes())
    if "CHANGE_ME" in json.dumps(worker) or "CHANGE_ME" in json.dumps(store):
        raise ExperimentError("replace every CHANGE_ME value in Mooncake configs")

    worker_bytes = _parse_mooncake_size(worker.get("global_segment_size", 0))
    store_bytes = _parse_mooncake_size(store.get("global_segment_size", 0))
    if worker_bytes != 0:
        raise ExperimentError(
            f"Mooncake worker must contribute 0 bytes, got {worker_bytes}"
        )
    if store_bytes != EXPECTED_MOONCAKE_STORE_BYTES:
        raise ExperimentError(
            f"Mooncake store must contribute {EXPECTED_MOONCAKE_STORE_BYTES} bytes, "
            f"got {store_bytes}"
        )

    for key in ("metadata_server", "master_server_address", "protocol", "tenant_id"):
        worker_value = worker.get(key)
        store_value = store.get(key)
        if not worker_value or worker_value != store_value:
            raise ExperimentError(
                f"Mooncake {key} must match: worker={worker_value!r}, "
                f"store={store_value!r}"
            )
    if str(worker["protocol"]).lower() != "rdma":
        raise ExperimentError(
            f"Mooncake protocol must be rdma, got {worker['protocol']!r}"
        )

    if l3_placement not in {None, "local", "remote"}:
        raise ExperimentError(f"invalid L3 placement: {l3_placement!r}")
    worker_host = str(worker.get("local_hostname", "")).strip()
    store_host = str(store.get("local_hostname", "")).strip()
    if not worker_host or not store_host:
        raise ExperimentError("Mooncake worker/store local_hostname is required")
    if l3_placement == "local" and worker_host != store_host:
        raise ExperimentError(
            "local L3 requires matching worker/store local_hostname: "
            f"worker={worker_host!r}, store={store_host!r}"
        )
    if l3_placement == "remote" and worker_host == store_host:
        raise ExperimentError(
            "remote L3 requires different worker/store local_hostname, got "
            f"{worker_host!r}"
        )

    return {
        "l3_placement": l3_placement,
        "worker_config_path": str(worker_path),
        "worker_config_sha256": sha256_file(worker_path),
        "worker_local_hostname": worker_host,
        "worker_global_segment_bytes": worker_bytes,
        "store_config_path": str(store_path),
        "store_config_sha256": sha256_file(store_path),
        "store_local_hostname": store_host,
        "store_global_segment_bytes": store_bytes,
        "protocol": str(worker["protocol"]),
        "tenant_id": str(worker["tenant_id"]),
        "metadata_server": str(worker["metadata_server"]),
        "master_server_address": str(worker["master_server_address"]),
    }


def _effective_values(server_info: dict[str, Any], key: str) -> list[Any]:
    states = server_info.get("internal_states") or []
    state_values = [state[key] for state in states if key in state]
    if state_values:
        return state_values
    return [server_info[key]] if key in server_info else []


def _require_value(server_info: dict[str, Any], key: str, expected: Any) -> None:
    values = _effective_values(server_info, key)
    if not values:
        raise ExperimentError(f"/server_info does not expose {key}")
    if any(value != expected for value in values):
        raise ExperimentError(f"expected {key}={expected!r}, got {values!r}")


def _l1_capacity_tokens(server_info: dict[str, Any]) -> int:
    capacities = []
    for state in server_info.get("internal_states") or []:
        memory_usage = state.get("memory_usage") or {}
        if "token_capacity" in memory_usage:
            capacities.append(int(memory_usage["token_capacity"]))
    if not capacities and "max_total_num_tokens" in server_info:
        capacities.append(int(server_info["max_total_num_tokens"]))
    if not capacities:
        raise ExperimentError("could not read L1 token capacity from /server_info")
    return min(capacities)


def preflight(
    *,
    base_url: str,
    manifest: dict[str, Any],
    server_log: Path,
    api_key: str | None,
    protocol: str,
    expected_hicache_size_gb: float,
    enforce_capacity: bool = True,
    mooncake_worker_config: Path | None = None,
    mooncake_store_config: Path | None = None,
    l3_placement: str | None = None,
) -> dict[str, Any]:
    mooncake_config = None
    if mooncake_worker_config is not None or mooncake_store_config is not None:
        if mooncake_worker_config is None or mooncake_store_config is None:
            raise ExperimentError("both Mooncake config paths are required")
        mooncake_config = validate_mooncake_configs(
            mooncake_worker_config, mooncake_store_config, l3_placement
        )
    info = request_json(base_url, "/server_info", api_key=api_key, timeout_s=60.0)
    for key, expected in (
        ("tp_size", 16),
        ("dp_size", 16),
        ("enable_dp_attention", True),
        ("dcp_size", 1),
        ("attention_backend", "ascend"),
        ("kv_cache_dtype", "bfloat16"),
        ("page_size", PAGE_SIZE),
        ("max_running_requests", 128),
        ("enable_hierarchical_cache", True),
        ("hicache_write_policy", "write_through"),
        ("hicache_storage_backend", "mooncake"),
        ("hicache_storage_prefetch_policy", "wait_complete"),
        ("hicache_io_backend", "kernel_ascend"),
        ("hicache_mem_layout", "page_first_kv_split"),
        ("enable_metrics", True),
        ("enable_cache_report", True),
    ):
        _require_value(info, key, expected)

    hicache_sizes = [float(value) for value in _effective_values(info, "hicache_size")]
    if not hicache_sizes or any(
        not math.isclose(value, expected_hicache_size_gb, abs_tol=1e-9)
        for value in hicache_sizes
    ):
        raise ExperimentError(
            f"expected hicache_size={expected_hicache_size_gb}, got {hicache_sizes}"
        )

    prompt_len = int(manifest["prompt_len"])
    max_req_values = _effective_values(info, "max_req_input_len")
    if not max_req_values:
        raise ExperimentError("/server_info does not expose max_req_input_len")
    max_req_input_len = min(int(value) for value in max_req_values)
    if prompt_len >= max_req_input_len:
        raise ExperimentError(
            f"prompt_len={prompt_len} must be below max_req_input_len={max_req_input_len}"
        )

    l1_capacity = _l1_capacity_tokens(info)
    expected_l1_capacity = {65_536: 73_728, 131_072: 139_264}.get(prompt_len)
    if expected_l1_capacity is None:
        raise ExperimentError(f"unsupported experiment-0 prompt_len={prompt_len}")
    if l1_capacity != expected_l1_capacity:
        raise ExperimentError(
            f"expected L1 token capacity {expected_l1_capacity} for "
            f"prompt_len={prompt_len}, got {l1_capacity}"
        )
    l2_capacity = parse_l2_capacity_tokens(server_log)
    filler_tokens = align_up((6 * l1_capacity + 4) // 5, PAGE_SIZE)
    min_cached = cacheable_tokens(prompt_len)
    isolated_required = min_cached + filler_tokens
    batch_required = int(manifest["num_prefixes"]) * min_cached + filler_tokens
    required = isolated_required if protocol == "isolated" else batch_required
    capacity_ok = required <= l2_capacity
    measured_bytes_per_token = expected_hicache_size_gb * 1e9 / l2_capacity
    bytes_per_token_relative_error = abs(
        measured_bytes_per_token - THEORETICAL_BF16_MLA_BYTES_PER_TOKEN
    ) / THEORETICAL_BF16_MLA_BYTES_PER_TOKEN
    if bytes_per_token_relative_error > 0.01:
        raise ExperimentError(
            "startup-derived L2 bytes/token does not match BF16 DeepSeek-V3.1 "
            f"MLA: measured={measured_bytes_per_token:.3f}, "
            f"theoretical={THEORETICAL_BF16_MLA_BYTES_PER_TOKEN}, "
            f"relative_error={bytes_per_token_relative_error:.3%}"
        )

    manifest_revision = manifest.get("tokenizer_revision")
    if not manifest_revision:
        raise ExperimentError("manifest does not pin a tokenizer/model revision")
    _require_value(info, "revision", manifest_revision)

    capacity_message = (
        f"{protocol} protocol needs {required} L2 tokens/rank but startup "
        f"allocated {l2_capacity}"
    )

    result = {
        "manifest_sha256": manifest["manifest_sha256"],
        "l3_placement": l3_placement,
        "protocol": protocol,
        "prompt_len": prompt_len,
        "min_cached_tokens": min_cached,
        "l1_capacity_tokens": l1_capacity,
        "l2_capacity_tokens": l2_capacity,
        "filler_tokens": filler_tokens,
        "isolated_required_l2_tokens": isolated_required,
        "plan_batch_required_l2_tokens": batch_required,
        "required_l2_tokens": required,
        "capacity_ok": capacity_ok,
        "status": "ready" if capacity_ok else "skipped",
        "capacity_message": capacity_message,
        "max_req_input_len": max_req_input_len,
        "l2_bytes_per_token": measured_bytes_per_token,
        "theoretical_bf16_mla_bytes_per_token": (
            THEORETICAL_BF16_MLA_BYTES_PER_TOKEN
        ),
        "bytes_per_token_relative_error": bytes_per_token_relative_error,
        "official_ascend_mla_layout": "page_first_kv_split",
        "server_version": info.get("version"),
        "model_path": info.get("model_path"),
        "model_revision": info.get("revision"),
        "validated_server_config": {
            "tp_size": 16,
            "dp_size": 16,
            "enable_dp_attention": True,
            "dcp_size": 1,
            "attention_backend": "ascend",
            "kv_cache_dtype": "bfloat16",
            "page_size": PAGE_SIZE,
            "max_running_requests": 128,
            "enable_hierarchical_cache": True,
            "hicache_size_gb_per_rank": expected_hicache_size_gb,
            "hicache_write_policy": "write_through",
            "hicache_storage_backend": "mooncake",
            "hicache_storage_prefetch_policy": "wait_complete",
            "hicache_io_backend": "kernel_ascend",
            "hicache_mem_layout": "page_first_kv_split",
            "enable_metrics": True,
            "enable_cache_report": True,
        },
        "mooncake_config": mooncake_config,
    }
    if enforce_capacity and not capacity_ok:
        raise SkipExperiment(
            f"{capacity_message}; refusing to produce invalid measurements"
        )
    return result


def split_filler_tokens(total_tokens: int, max_req_input_len: int) -> list[int]:
    max_cacheable = align_down(max_req_input_len - 2, PAGE_SIZE)
    if max_cacheable <= 0:
        raise ExperimentError(f"max_req_input_len={max_req_input_len} is too small")
    chunks = []
    remaining = total_tokens
    while remaining > 0:
        chunk = min(remaining, max_cacheable)
        chunk = align_down(chunk, PAGE_SIZE)
        if chunk <= 0:
            chunk = PAGE_SIZE
        chunks.append(chunk)
        remaining -= chunk
    return chunks


def make_filler(
    token_pool: list[int], *, cache_tokens: int, seed: int
) -> list[int]:
    if not token_pool:
        raise ExperimentError("manifest has an empty filler token pool")
    rng = random.Random(seed)
    return [token_pool[rng.randrange(len(token_pool))] for _ in range(cache_tokens + 1)]


def write_record(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(record, sort_keys=True, separators=(",", ":")))
        output.write("\n")
        output.flush()


def _base_record(
    *,
    run_id: str,
    length_label: str,
    phase: str,
    prefix_id: str,
    manifest: dict[str, Any],
    l3_placement: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "length": length_label,
        "l3_placement": l3_placement,
        "phase": phase,
        "prefix_id": prefix_id,
        "manifest_sha256": manifest["manifest_sha256"],
        "protocol": "isolated",
        "routed_dp_rank": 0,
        "timestamp_ns": time.time_ns(),
    }


def run_l2(
    *,
    base_url: str,
    manifest: dict[str, Any],
    output_path: Path,
    run_id: str,
    length_label: str,
    api_key: str | None,
    admin_api_key: str | None,
    timeout_s: float,
    idle_timeout_s: float,
    flight: dict[str, Any],
    l3_placement: str = "local",
) -> None:
    control_api_key = admin_api_key or api_key
    token_pool = [int(value) for value in manifest["filler_token_pool"]]
    chunks = split_filler_tokens(
        int(flight["filler_tokens"]), int(flight["max_req_input_len"])
    )
    min_cached = int(flight["min_cached_tokens"])

    for prefix_index, prefix in enumerate(manifest["prefixes"]):
        prefix_id = str(prefix["prefix_id"])
        input_ids = [int(value) for value in prefix["input_ids"]]
        warm = send_generate(
            base_url,
            input_ids,
            rid=f"{run_id}-{length_label}-{prefix_id}-warm",
            api_key=api_key,
            timeout_s=timeout_s,
        )
        warm.update(
            _base_record(
                run_id=run_id,
                length_label=length_label,
                phase="warm",
                prefix_id=prefix_id,
                manifest=manifest,
                l3_placement=l3_placement,
            )
        )
        warm["record_type"] = "setup"
        write_record(output_path, warm)
        wait_until_idle(
            base_url, api_key=control_api_key, timeout_s=idle_timeout_s
        )

        for filler_index, cache_tokens in enumerate(chunks):
            filler = make_filler(
                token_pool,
                cache_tokens=cache_tokens,
                seed=(int(manifest["seed"]) + 1) * 1_000_003
                + prefix_index * 1009
                + filler_index,
            )
            filler_result = send_generate(
                base_url,
                filler,
                rid=(
                    f"{run_id}-{length_label}-{prefix_id}-"
                    f"filler-{filler_index:02d}"
                ),
                api_key=api_key,
                timeout_s=timeout_s,
            )
            filler_result.update(
                _base_record(
                    run_id=run_id,
                    length_label=length_label,
                    phase="filler",
                    prefix_id=prefix_id,
                    manifest=manifest,
                    l3_placement=l3_placement,
                )
            )
            filler_result["filler_index"] = filler_index
            filler_result["record_type"] = "setup"
            write_record(output_path, filler_result)
        wait_until_idle(
            base_url, api_key=control_api_key, timeout_s=idle_timeout_s
        )

        measured = send_generate(
            base_url,
            input_ids,
            rid=f"{run_id}-{length_label}-{prefix_id}-l2",
            api_key=api_key,
            timeout_s=timeout_s,
        )
        details = validate_cache_source(measured, "l2", min_cached)
        measured.update(
            _base_record(
                run_id=run_id,
                length_label=length_label,
                phase="l2",
                prefix_id=prefix_id,
                manifest=manifest,
                l3_placement=l3_placement,
            )
        )
        measured["record_type"] = "measurement"
        measured["accepted"] = True
        measured["admitted_cache_details"] = details
        measured["min_cached_tokens"] = min_cached
        write_record(output_path, measured)
        flush_local_caches(
            base_url, api_key=control_api_key, timeout_s=idle_timeout_s
        )


def run_l3(
    *,
    base_url: str,
    manifest: dict[str, Any],
    output_path: Path,
    run_id: str,
    length_label: str,
    api_key: str | None,
    admin_api_key: str | None,
    timeout_s: float,
    idle_timeout_s: float,
    l3_placement: str = "local",
) -> None:
    control_api_key = admin_api_key or api_key
    min_cached = cacheable_tokens(int(manifest["prompt_len"]))
    for prefix in manifest["prefixes"]:
        prefix_id = str(prefix["prefix_id"])
        measured = send_generate(
            base_url,
            [int(value) for value in prefix["input_ids"]],
            rid=f"{run_id}-{length_label}-{prefix_id}-l3",
            api_key=api_key,
            timeout_s=timeout_s,
        )
        details = validate_cache_source(measured, "l3", min_cached)
        measured.update(
            _base_record(
                run_id=run_id,
                length_label=length_label,
                phase="l3",
                prefix_id=prefix_id,
                manifest=manifest,
                l3_placement=l3_placement,
            )
        )
        measured["record_type"] = "measurement"
        measured["accepted"] = True
        measured["admitted_cache_details"] = details
        measured["min_cached_tokens"] = min_cached
        write_record(output_path, measured)
        wait_until_idle(
            base_url, api_key=control_api_key, timeout_s=idle_timeout_s
        )


def load_measurements(results_root: Path) -> list[dict[str, Any]]:
    records = []
    for path in sorted(results_root.rglob("*.jsonl")):
        with path.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, 1):
                if not line.strip():
                    continue
                record = json.loads(line)
                if record.get("record_type") != "measurement":
                    continue
                if record.get("accepted") is not True:
                    raise SummaryError(
                        f"unaccepted measurement in {path}:{line_number}"
                    )
                record["_source"] = f"{path}:{line_number}"
                records.append(record)
    if not records:
        raise SummaryError(f"no accepted measurements under {results_root}")
    return records


def summarize(
    records: list[dict[str, Any]], expected_runs: int = 3
) -> dict[str, Any]:
    grouped: dict[
        tuple[str, str, str, str], dict[str, dict[str, Any]]
    ] = defaultdict(dict)
    manifests: dict[tuple[str, str], set[str]] = defaultdict(set)
    for record in records:
        phase = str(record.get("phase", "")).lower()
        if phase not in {"l2", "l3"}:
            continue
        placement = str(record.get("l3_placement", "unknown"))
        length = str(record["length"])
        run_id = str(record["run_id"])
        prefix_id = str(record["prefix_id"])
        key = (placement, length, run_id, prefix_id)
        if phase in grouped[key]:
            raise SummaryError(f"duplicate {phase} measurement for {key}")
        grouped[key][phase] = record
        manifests[(placement, length)].add(str(record["manifest_sha256"]))

    for (placement, length), hashes in manifests.items():
        if len(hashes) != 1:
            raise SummaryError(
                f"{placement}/{length} used multiple manifests: {sorted(hashes)}"
            )

    pairs = []
    for (placement, length, run_id, prefix_id), phases in sorted(grouped.items()):
        missing = {"l2", "l3"} - set(phases)
        if missing:
            raise SummaryError(
                "missing "
                f"{sorted(missing)} for {(placement, length, run_id, prefix_id)}"
            )
        l2_ms = float(phases["l2"]["ttft_ms"])
        l3_ms = float(phases["l3"]["ttft_ms"])
        pairs.append(
            {
                "l3_placement": placement,
                "length": length,
                "run_id": run_id,
                "prefix_id": prefix_id,
                "ttft_l2_ms": l2_ms,
                "ttft_l3_ms": l3_ms,
                "delta_ttft_ms": l3_ms - l2_ms,
            }
        )

    by_run: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for pair in pairs:
        by_run[
            (pair["l3_placement"], pair["length"], pair["run_id"])
        ].append(pair)

    run_summaries = []
    for (placement, length, run_id), run_pairs in sorted(by_run.items()):
        if len(run_pairs) != 10:
            raise SummaryError(
                f"{placement}/{length}/{run_id} has {len(run_pairs)} pairs; expected 10"
            )
        run_summaries.append(
            {
                "l3_placement": placement,
                "length": length,
                "run_id": run_id,
                "num_pairs": 10,
                "median_ttft_l2_ms": statistics.median(
                    pair["ttft_l2_ms"] for pair in run_pairs
                ),
                "median_ttft_l3_ms": statistics.median(
                    pair["ttft_l3_ms"] for pair in run_pairs
                ),
                "median_delta_ttft_ms": statistics.median(
                    pair["delta_ttft_ms"] for pair in run_pairs
                ),
            }
        )

    by_placement_length: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(
        list
    )
    for run in run_summaries:
        by_placement_length[(run["l3_placement"], run["length"])].append(run)

    aggregate = []
    for (placement, length), runs in sorted(by_placement_length.items()):
        if len(runs) != expected_runs:
            raise SummaryError(
                f"{placement}/{length} has {len(runs)} complete runs; "
                f"expected {expected_runs}"
            )
        l2_values = [float(run["median_ttft_l2_ms"]) for run in runs]
        l3_values = [float(run["median_ttft_l3_ms"]) for run in runs]
        delta_values = [float(run["median_delta_ttft_ms"]) for run in runs]
        aggregate.append(
            {
                "l3_placement": placement,
                "length": length,
                "num_runs": len(runs),
                "median_of_run_median_ttft_l2_ms": statistics.median(l2_values),
                "min_run_median_ttft_l2_ms": min(l2_values),
                "max_run_median_ttft_l2_ms": max(l2_values),
                "median_of_run_median_ttft_l3_ms": statistics.median(l3_values),
                "min_run_median_ttft_l3_ms": min(l3_values),
                "max_run_median_ttft_l3_ms": max(l3_values),
                "median_of_run_median_delta_ttft_ms": statistics.median(
                    delta_values
                ),
                "min_run_median_delta_ttft_ms": min(delta_values),
                "max_run_median_delta_ttft_ms": max(delta_values),
                "all_run_medians_positive": all(value > 0 for value in delta_values),
            }
        )
    return {
        "schema_version": 1,
        "paired_measurements": pairs,
        "run_summaries": run_summaries,
        "aggregate": aggregate,
    }


def summary_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Experiment 0 Summary",
        "",
        "| L3 | Length | Run | Pairs | Median L2 (ms) | Median L3 (ms) | Median Delta (ms) |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for run in summary["run_summaries"]:
        lines.append(
            f"| {run['l3_placement']} | {run['length']} | {run['run_id']} | "
            f"{run['num_pairs']} | "
            f"{run['median_ttft_l2_ms']:.3f} | "
            f"{run['median_ttft_l3_ms']:.3f} | "
            f"{run['median_delta_ttft_ms']:.3f} |"
        )
    lines.extend(["", "## Across Runs", ""])
    for item in summary["aggregate"]:
        lines.extend(
            [
                f"- L3/length: {item['l3_placement']}/{item['length']} "
                f"({item['num_runs']} runs)",
                "- L2 run medians: "
                f"median={item['median_of_run_median_ttft_l2_ms']:.3f}, "
                f"min={item['min_run_median_ttft_l2_ms']:.3f}, "
                f"max={item['max_run_median_ttft_l2_ms']:.3f} ms",
                "- L3 run medians: "
                f"median={item['median_of_run_median_ttft_l3_ms']:.3f}, "
                f"min={item['min_run_median_ttft_l3_ms']:.3f}, "
                f"max={item['max_run_median_ttft_l3_ms']:.3f} ms",
                "- Delta run medians: "
                f"median={item['median_of_run_median_delta_ttft_ms']:.3f}, "
                f"min={item['min_run_median_delta_ttft_ms']:.3f}, "
                f"max={item['max_run_median_delta_ttft_ms']:.3f} ms",
                f"- All delta medians positive: {item['all_run_medians_positive']}",
            ]
        )
    return "\n".join(lines) + "\n"


def _add_connection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--url", default="http://127.0.0.1:30000")
    parser.add_argument("--api-key")
    parser.add_argument("--admin-api-key")
    parser.add_argument("--idle-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT_S)


def _add_phase_args(parser: argparse.ArgumentParser) -> None:
    _add_connection_args(parser)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--server-log", type=Path, required=True)
    parser.add_argument("--mooncake-worker-config", type=Path, required=True)
    parser.add_argument("--mooncake-store-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--length", choices=("64k", "128k"), required=True)
    parser.add_argument("--l3-placement", choices=("local", "remote"), required=True)
    parser.add_argument(
        "--request-timeout", type=float, default=DEFAULT_REQUEST_TIMEOUT_S
    )
    parser.add_argument("--hicache-size-gb", type=float, default=20.0)
    parser.add_argument("--overwrite", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    manifest = subparsers.add_parser("build-manifest")
    manifest.add_argument("--tokenizer", required=True)
    manifest.add_argument("--revision", required=True)
    manifest.add_argument("--output", type=Path, required=True)
    manifest.add_argument("--prefix-len", type=int, required=True)
    manifest.add_argument("--question-len", type=int, default=128)
    manifest.add_argument("--num-prefixes", type=int, default=10)
    manifest.add_argument("--seed", type=int, default=1)
    manifest.add_argument("--no-trust-remote-code", action="store_true")

    check = subparsers.add_parser("preflight")
    _add_connection_args(check)
    check.add_argument("--manifest", type=Path, required=True)
    check.add_argument("--server-log", type=Path, required=True)
    check.add_argument("--mooncake-worker-config", type=Path, required=True)
    check.add_argument("--mooncake-store-config", type=Path, required=True)
    check.add_argument("--l3-placement", choices=("local", "remote"), required=True)
    check.add_argument(
        "--protocol", choices=("isolated", "plan-batch"), default="isolated"
    )
    check.add_argument("--hicache-size-gb", type=float, default=20.0)
    check.add_argument("--output", type=Path)

    mooncake_check = subparsers.add_parser("validate-mooncake")
    mooncake_check.add_argument("--mooncake-worker-config", type=Path, required=True)
    mooncake_check.add_argument("--mooncake-store-config", type=Path, required=True)
    mooncake_check.add_argument(
        "--l3-placement", choices=("local", "remote"), required=True
    )

    reset = subparsers.add_parser("reset")
    _add_connection_args(reset)

    wait = subparsers.add_parser("wait")
    _add_connection_args(wait)

    l2 = subparsers.add_parser("l2")
    _add_phase_args(l2)

    l3 = subparsers.add_parser("l3")
    _add_phase_args(l3)

    summary = subparsers.add_parser("summarize")
    summary.add_argument("results", type=Path)
    summary.add_argument("--expected-runs", type=int, default=3)
    summary.add_argument("--json-output", type=Path, required=True)
    summary.add_argument("--markdown-output", type=Path, required=True)
    return parser


def _prepare_output(path: Path, overwrite: bool) -> None:
    if path.exists():
        if not overwrite:
            raise ExperimentError(f"output already exists: {path}")
        path.unlink()


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "build-manifest":
            built = build_manifest(
                tokenizer_path=args.tokenizer,
                revision=args.revision,
                prefix_len=args.prefix_len,
                question_len=args.question_len,
                num_prefixes=args.num_prefixes,
                seed=args.seed,
                trust_remote_code=not args.no_trust_remote_code,
            )
            write_manifest(args.output, built)
            print(
                json.dumps(
                    {
                        "manifest": str(args.output),
                        "manifest_sha256": built["manifest_sha256"],
                        "prompt_len": built["prompt_len"],
                        "num_prefixes": built["num_prefixes"],
                    },
                    sort_keys=True,
                )
            )
            return 0

        if args.command == "validate-mooncake":
            checked = validate_mooncake_configs(
                args.mooncake_worker_config,
                args.mooncake_store_config,
                args.l3_placement,
            )
            print(json.dumps(checked, indent=2, sort_keys=True))
            return 0

        if args.command == "reset":
            reset_all_caches(
                args.url,
                api_key=args.admin_api_key or args.api_key,
                timeout_s=args.idle_timeout,
            )
            print("L1, L2, and L3 cleared")
            return 0

        if args.command == "wait":
            wait_until_idle(
                args.url,
                api_key=args.admin_api_key or args.api_key,
                timeout_s=args.idle_timeout,
            )
            print("all scheduler ranks are fully idle")
            return 0

        if args.command == "summarize":
            result = summarize(
                load_measurements(args.results), expected_runs=args.expected_runs
            )
            rendered = summary_markdown(result)
            args.json_output.parent.mkdir(parents=True, exist_ok=True)
            args.json_output.write_text(
                json.dumps(result, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
            args.markdown_output.write_text(rendered, encoding="utf-8")
            print(rendered, end="")
            return 0

        loaded = load_manifest(args.manifest)
        if args.command == "preflight":
            checked = preflight(
                base_url=args.url,
                manifest=loaded,
                server_log=args.server_log,
                api_key=args.api_key,
                protocol=args.protocol,
                expected_hicache_size_gb=args.hicache_size_gb,
                enforce_capacity=False,
                mooncake_worker_config=args.mooncake_worker_config,
                mooncake_store_config=args.mooncake_store_config,
                l3_placement=args.l3_placement,
            )
            encoded = json.dumps(checked, indent=2, sort_keys=True)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(encoded + "\n", encoding="utf-8")
            print(encoded)
            if not checked["capacity_ok"]:
                print(
                    f"SKIP: {checked['capacity_message']}; refusing to produce "
                    "invalid measurements",
                    file=sys.stderr,
                )
                return 3
            return 0

        _prepare_output(args.output, args.overwrite)
        checked = preflight(
            base_url=args.url,
            manifest=loaded,
            server_log=args.server_log,
            api_key=args.api_key,
            protocol="isolated",
            expected_hicache_size_gb=args.hicache_size_gb,
            enforce_capacity=args.command == "l2",
            mooncake_worker_config=args.mooncake_worker_config,
            mooncake_store_config=args.mooncake_store_config,
            l3_placement=args.l3_placement,
        )
        if args.command == "l2":
            run_l2(
                base_url=args.url,
                manifest=loaded,
                output_path=args.output,
                run_id=args.run_id,
                length_label=args.length,
                api_key=args.api_key,
                admin_api_key=args.admin_api_key,
                timeout_s=args.request_timeout,
                idle_timeout_s=args.idle_timeout,
                flight=checked,
                l3_placement=args.l3_placement,
            )
        elif args.command == "l3":
            run_l3(
                base_url=args.url,
                manifest=loaded,
                output_path=args.output,
                run_id=args.run_id,
                length_label=args.length,
                api_key=args.api_key,
                admin_api_key=args.admin_api_key,
                timeout_s=args.request_timeout,
                idle_timeout_s=args.idle_timeout,
                l3_placement=args.l3_placement,
            )
        return 0
    except SkipExperiment as exc:
        print(f"SKIP: {exc}", file=sys.stderr)
        return 3
    except (
        ExperimentError,
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
