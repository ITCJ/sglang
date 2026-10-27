"""Exercise the native allocator seam using real saved prefix pages."""

import ast
import dataclasses
import sys
import unittest
from array import array
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple
from unittest.mock import patch

import test_prefix
import torch
from test_mempool_native_prefix import native_methods
from test_mempool_native_reservation import Pages, Rows
from test_native_release import _exec_definitions, load_functions, load_methods

from ascend_mempool_pd.mempool_prefill_restore import (
    PrefillRestore,
    install_prefill_cache,
)


@contextmanager
def native_cache_code():
    """Load real native cache/allocation bodies; replace only device imports."""
    namespace = dict(
        torch=torch,
        dataclasses=dataclasses,
        NamedTuple=NamedTuple,
        array=array,
        __name__=__name__,
        _is_npu=False,
        _is_hip=False,
        _is_cuda=False,
        is_pin_memory_available=lambda device: False,
        attention_backends=lambda: ("ascend", "ascend"),
        support_triton=lambda backend: False,
        page_interleave_shard_size=lambda allocator: 1,
        HybridReqToTokenPool=type("UnusedHybridPool", (), {}),
        envs=SimpleNamespace(
            SGLANG_RADIX_FORCE_MISS=SimpleNamespace(get=lambda: False)
        ),
    )
    modules = {}
    for path, names in (
        ("mem_cache/base_prefix_cache.py", {"MatchPrefixParams", "MatchResult"}),
        ("mem_cache/cache_init_params.py", {"CacheInitParams"}),
    ):
        source = Path(__file__).resolve().parents[3] / "python/sglang/srt" / path
        definitions = [
            n
            for n in ast.parse(source.read_text()).body
            if isinstance(n, ast.ClassDef) and n.name in names
        ]
        _exec_definitions(source, definitions, namespace)
        modules["sglang.srt." + path[:-3].replace("/", ".")] = SimpleNamespace(
            **{n: namespace[n] for n in names}
        )
    load_methods(
        "mem_cache/base_prefix_cache.py",
        "BasePrefixCache",
        {"swa_reprefill_tail_tokens", "supports_mamba", "free_kv_row"},
        namespace,
        standalone=True,
    )
    load_methods(
        "mem_cache/chunk_cache.py",
        "ChunkCache",
        {"__init__", "match_prefix", "cache_finished_req", "is_chunk_cache"},
        namespace,
    )
    modules["sglang.srt.mem_cache.chunk_cache"] = SimpleNamespace(
        ChunkCache=namespace["ChunkCache"]
    )
    load_methods(
        "mem_cache/radix_cache.py",
        "RadixKey",
        {"__init__", "__len__", "_raw_len"},
        namespace,
    )
    load_methods(
        "managers/schedule_batch.py",
        "Req",
        {"init_next_round_input", "_refresh_fill_ids", "_compute_max_prefix_len"},
        namespace,
        standalone=True,
    )
    load_functions(
        "mem_cache/common.py",
        {"coalesce_ranges", "free_kv_row_segments", "evict_from_tree_cache"},
        namespace,
    )
    modules["sglang.srt.mem_cache.common"] = SimpleNamespace(**namespace)
    load_functions(
        "mem_cache/allocation.py",
        {
            "alloc_for_extend",
            "alloc_req_slots",
            "_alloc_page_size",
            "alloc_paged_token_slots_extend",
            "write_cache_indices",
        },
        namespace,
    )
    with patch.dict(sys.modules, modules):
        yield namespace


class TestPrefillRestore(unittest.TestCase):
    def test_identical_prompt_restores_31_allocator_pages_and_keeps_last_page_for_extend(
        self,
    ):
        helper = test_prefix.TestPrefixStore()
        self.addCleanup(helper.doCleanups)
        store, _, _, events = helper.make_store(
            pages=40, page_size=128, prompt_capacity=8192
        )
        save = dict(
            rid="A",
            op_id=1,
            slot=3,
            prompt_tokens=4096,
            source_pages=list(range(32)),
            target_pages=list(range(32)),
        )
        store.admit(**save)
        events[-1].done = True
        store.admit(**save)
        store.release_source("A")
        pages = Pages()
        buffers = [torch.zeros((8704, 1, 2), dtype=torch.bfloat16) for _ in range(5)]
        pool = SimpleNamespace(
            start_layer=0,
            layer_num=2,
            kv_lora_rank=2,
            qk_rope_head_dim=2,
            page_size=128,
            indexer_layer_ids=(0,),
            index_head_dim=2,
            index_k_scale_buffer=None,
            get_key_buffer=lambda layer: buffers[layer],
            get_value_buffer=lambda layer: buffers[2 + layer],
            get_index_k_buffer=lambda layer: buffers[4],
        )
        pool = native_methods(pool)
        pages.get_kvcache = lambda: pool
        restore = PrefillRestore(pages, store, verify_boundaries=True)
        request = dict(rid="C", room=3, input_ids=[7] * 4096, hit_len=3968)
        restore.begin_request(request, slot=5)
        req = SimpleNamespace(rid="C", bootstrap_room=3, origin_input_ids=[7] * 4096)
        with self.assertRaisesRegex(RuntimeError, "restored"):
            restore.match(req)
        self.assertIsNone(restore.load_back(op_id=2, source_pages=list(range(31))))
        self.assertEqual(pages.available_size(), 3 * 128)
        events[-1].done = True
        restore.load_back(op_id=2, source_pages=list(range(31)))
        indices = restore.match(req)
        self.assertEqual(len(indices), 3968)
        self.assertEqual(indices[128:130].tolist(), [640, 641])
        self.assertIn(3, pages.pages)  # The skipped native page was returned.
        # The installed adapter must reach Req matching and the real extend
        # allocator; its cache protection must let native cleanup free all 32 pages.
        with native_cache_code() as code:
            rows = Rows()
            pages.device = "cpu"
            pages.free_segments = lambda segments: pages.free(
                torch.cat([part for part, start in segments])
            )

            def extend(prefix, prefix_cpu, seq, seq_cpu, last_loc, count):
                self.assertEqual(prefix_cpu.tolist(), [3968])
                self.assertEqual(seq_cpu.tolist(), [4096])
                self.assertEqual(last_loc.item(), indices[-1].item())
                self.assertEqual(count, 128)
                return pages.alloc(count)

            pages.alloc_extend = extend

            def alloc_rows(reqs):
                result = rows.alloc_rows(len(reqs))
                for item, row in zip(reqs, result):
                    item.kv.req_pool_idx = row
                return result

            rows.alloc, rows.alloc_aux_to_lengths = alloc_rows, lambda **kw: None
            params = code["CacheInitParams"](True, rows, pages, 128)
            scheduler = SimpleNamespace(
                tree_cache=code["ChunkCache"](params),
                req_to_token_pool=rows,
                token_to_kv_pool_allocator=pages,
                server_args=SimpleNamespace(page_size=128),
            )
            install_prefill_cache(scheduler, restore)
            native_req = code["Req"]()
            native_req.__dict__.update(req.__dict__)
            native_req.__dict__.update(
                bootstrap_host="p",
                output_ids=[],
                full_untruncated_fill_ids=[],
                session=None,
                return_logprob=False,
                positional_embed_overrides=None,
                extra_key=None,
                cache_salt=None,
                is_retracted=False,
                is_dllm=lambda: False,
                kv=SimpleNamespace(
                    req_pool_idx=None, cache_protected_len=-1, swa_evicted_seqlen=0
                ),
            )
            native_req.init_next_round_input(scheduler.tree_cache)
            self.assertTrue(torch.equal(native_req.prefix_indices, indices))
            self.assertEqual(native_req.kv.cache_protected_len, 0)
            batch = SimpleNamespace(
                reqs=[native_req],
                tree_cache=scheduler.tree_cache,
                req_to_token_pool=rows,
                device="cpu",
                prefix_lens=[3968],
                extend_lens=[128],
                extend_num_tokens=128,
                seq_lens=torch.tensor([4096]),
                seq_lens_cpu=torch.tensor([4096]),
                is_dllm=lambda: False,
                maybe_evict_swa=lambda: None,
            )
            out, _, _ = code["alloc_for_extend"](batch)
            self.assertEqual(len(out), 128)
            self.assertEqual(pages.available_size(), 2 * 128)
            self.assertTrue(torch.equal(rows.req_to_token[1, :3968], indices))
            self.assertTrue(torch.equal(rows.req_to_token[1, 3968:4096], out))
            self.assertEqual(len(rows.req_to_token[1, :4096].unique()), 4096)
            scheduler.tree_cache.cache_finished_req(native_req, owned_kv_len=4096)
            rows.free(native_req)
            self.assertEqual(pages.available_size(), 34 * 128)
            self.assertEqual(rows.free_slots, [1])
        with self.assertRaisesRegex(ValueError, "request"):
            restore.match(
                SimpleNamespace(
                    rid="other", bootstrap_room=3, origin_input_ids=[7] * 4096
                )
            )
        with self.assertRaisesRegex(RuntimeError, "native"):
            restore.finish_request()
        restore.native_released(req)
        restore.finish_request()


if __name__ == "__main__":
    unittest.main()
