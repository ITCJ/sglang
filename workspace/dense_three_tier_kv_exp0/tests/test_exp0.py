from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


exp0 = load_module("exp0", ROOT / "exp0.py")


def server_info(l1_capacity: int, max_req_input_len: int) -> dict:
    return {
        "tp_size": 16,
        "dp_size": 16,
        "enable_dp_attention": True,
        "dcp_size": 1,
        "attention_backend": "ascend",
        "kv_cache_dtype": "bfloat16",
        "page_size": 64,
        "max_running_requests": 128,
        "enable_hierarchical_cache": True,
        "hicache_size": 20.0,
        "hicache_write_policy": "write_through",
        "hicache_storage_backend": "mooncake",
        "hicache_storage_prefetch_policy": "wait_complete",
        "hicache_io_backend": "kernel_ascend",
        "hicache_mem_layout": "page_first_kv_split",
        "enable_metrics": True,
        "enable_cache_report": True,
        "revision": "fixed-revision",
        "max_req_input_len": max_req_input_len,
        "internal_states": [
            {"memory_usage": {"token_capacity": l1_capacity}} for _ in range(16)
        ],
    }


def manifest(prefix_len: int, question_len: int = 128) -> dict:
    return {
        "manifest_sha256": "fixed",
        "seed": 1,
        "tokenizer_revision": "fixed-revision",
        "prefix_len": prefix_len,
        "question_len": question_len,
        "prompt_len": prefix_len + question_len,
        "num_prefixes": 10,
        "filler_token_pool": list(range(256)),
        "prefixes": [],
    }


class FakeResponse:
    status = 200

    def __init__(self, lines):
        self.lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def __iter__(self):
        return iter(self.lines)


class TestExperimentZero(unittest.TestCase):
    def test_filler_split_is_page_aligned_and_bounded(self):
        chunks = exp0.split_filler_tokens(88_512, 73_728)

        self.assertEqual(sum(chunks), 88_512)
        self.assertTrue(all(chunk % 64 == 0 for chunk in chunks))
        self.assertTrue(all(chunk + 1 < 73_728 for chunk in chunks))

    def test_cache_source_admission(self):
        l2 = {"cached_tokens_details": {"device": 0, "host": 65_408, "storage": 0}}
        l3 = {
            "cached_tokens_details": {
                "device": 0,
                "host": 0,
                "storage": 65_408,
                "storage_backend": "MooncakeStore",
            }
        }

        self.assertEqual(exp0.validate_cache_source(l2, "l2", 65_408)["host"], 65_408)
        self.assertEqual(
            exp0.validate_cache_source(l3, "l3", 65_408)["storage"], 65_408
        )

        with self.assertRaises(exp0.ExperimentError):
            exp0.validate_cache_source(
                {"cached_tokens_details": {"device": 64, "host": 65_408}},
                "l2",
                65_408,
            )

        with self.assertRaises(exp0.ExperimentError):
            exp0.validate_cache_source(
                {
                    "cached_tokens_details": {
                        "device": 0,
                        "host": 64,
                        "storage": 65_408,
                        "storage_backend": "MooncakeStore",
                    }
                },
                "l3",
                65_408,
            )

    def test_mooncake_configs_pin_external_640_decimal_gb_store(self):
        shared = {
            "metadata_server": "http://master:8080/metadata",
            "master_server_address": "master:50051",
            "protocol": "rdma",
            "tenant_id": "exp0",
        }
        worker = {
            **shared,
            "local_hostname": "worker",
            "global_segment_size": 0,
        }
        store = {
            **shared,
            "local_hostname": "store",
            "global_segment_size": 640_000_000_000,
        }
        with tempfile.TemporaryDirectory() as directory:
            worker_path = Path(directory) / "worker.json"
            store_path = Path(directory) / "store.json"
            worker_path.write_text(json.dumps(worker))
            store_path.write_text(json.dumps(store))

            checked = exp0.validate_mooncake_configs(
                worker_path, store_path, "remote"
            )
            self.assertEqual(
                checked["store_global_segment_bytes"], 640_000_000_000
            )
            self.assertEqual(checked["l3_placement"], "remote")

            with self.assertRaises(exp0.ExperimentError):
                exp0.validate_mooncake_configs(worker_path, store_path, "local")

            store["local_hostname"] = "worker"
            store_path.write_text(json.dumps(store))
            checked = exp0.validate_mooncake_configs(
                worker_path, store_path, "local"
            )
            self.assertEqual(checked["l3_placement"], "local")
            with self.assertRaises(exp0.ExperimentError):
                exp0.validate_mooncake_configs(worker_path, store_path, "remote")

            store["global_segment_size"] = "640gb"
            store_path.write_text(json.dumps(store))
            with self.assertRaises(exp0.ExperimentError):
                exp0.validate_mooncake_configs(worker_path, store_path)

    def test_streaming_ttft_uses_first_nonempty_text(self):
        lines = [
            b'data: {"text":"","meta_info":{"prompt_tokens":3}}\n',
            b'data: {"text":"x","meta_info":{"completion_tokens":1,'
            b'"cached_tokens":0}}\n',
            b"data: [DONE]\n",
        ]
        with patch.object(exp0.urllib.request, "urlopen", return_value=FakeResponse(lines)):
            with patch.object(exp0.time, "perf_counter_ns", side_effect=[100, 250, 400]):
                result = exp0.send_generate(
                    "http://server",
                    [1, 2, 3],
                    rid="r",
                    api_key=None,
                    timeout_s=1,
                )

        self.assertEqual(result["first_nonempty_token_ns"], 250)
        self.assertEqual(result["ttft_ms"], 0.00015)
        self.assertEqual(result["generated_text"], "x")

    def test_generate_and_control_use_separate_api_keys(self):
        one_prefix_manifest = {
            "manifest_sha256": "fixed",
            "prompt_len": 65_536,
            "prefix_len": 65_408,
            "prefixes": [{"prefix_id": "p00", "input_ids": [1]}],
        }
        response = {
            "cached_tokens_details": {
                "device": 0,
                "host": 0,
                "storage": 65_472,
                "storage_backend": "MooncakeStore",
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(exp0, "send_generate", return_value=response) as send:
                with patch.object(exp0, "wait_until_idle") as wait:
                    exp0.run_l3(
                        base_url="http://server",
                        manifest=one_prefix_manifest,
                        output_path=Path(directory) / "l3.jsonl",
                        run_id="run-1",
                        length_label="64k",
                        api_key="normal-key",
                        admin_api_key="admin-key",
                        timeout_s=1,
                        idle_timeout_s=1,
                    )

        self.assertEqual(send.call_args.kwargs["api_key"], "normal-key")
        self.assertEqual(wait.call_args.kwargs["api_key"], "admin-key")

    def test_l2_isolation_flushes_only_local_cache_after_each_prefix(self):
        one_prefix_manifest = {
            "manifest_sha256": "fixed",
            "seed": 1,
            "filler_token_pool": [7, 8],
            "prefixes": [{"prefix_id": "p00", "input_ids": [1]}],
        }
        response = {
            "cached_tokens_details": {
                "device": 0,
                "host": 64,
                "storage": 0,
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(exp0, "send_generate", return_value=response):
                with patch.object(exp0, "wait_until_idle"):
                    with patch.object(exp0, "flush_local_caches") as flush_local:
                        exp0.run_l2(
                            base_url="http://server",
                            manifest=one_prefix_manifest,
                            output_path=Path(directory) / "l2.jsonl",
                            run_id="run-1",
                            length_label="64k",
                            api_key="normal-key",
                            admin_api_key="admin-key",
                            timeout_s=1,
                            idle_timeout_s=2,
                            flight={
                                "filler_tokens": 64,
                                "max_req_input_len": 128,
                                "min_cached_tokens": 64,
                            },
                        )

        flush_local.assert_called_once_with(
            "http://server", api_key="admin-key", timeout_s=2
        )

    def test_preflight_skip_is_written_before_exit(self):
        checked = {
            "capacity_ok": False,
            "capacity_message": "needs 298176 but allocated 284608",
            "status": "skipped",
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "preflight.json"
            with patch.object(exp0, "load_manifest", return_value={"loaded": True}):
                with patch.object(exp0, "preflight", return_value=checked):
                    return_code = exp0.main(
                        [
                            "preflight",
                            "--manifest",
                            str(Path(directory) / "manifest.json"),
                            "--server-log",
                            str(Path(directory) / "server.log"),
                            "--mooncake-worker-config",
                            str(Path(directory) / "worker.json"),
                            "--mooncake-store-config",
                            str(Path(directory) / "store.json"),
                            "--l3-placement",
                            "local",
                            "--output",
                            str(output),
                        ]
                    )

            self.assertEqual(return_code, 3)
            self.assertEqual(json.loads(output.read_text()), checked)

    def test_64k_isolated_passes_but_plan_batch_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "server.log"
            log.write_text(
                "Allocating kv hierarchical KV host pool: 284608 tokens, "
                "20.00 GB host memory.\n"
            )
            info = server_info(73_728, 73_728)
            with patch.object(exp0, "request_json", return_value=info):
                checked = exp0.preflight(
                    base_url="http://server",
                    manifest=manifest(65_408),
                    server_log=log,
                    api_key=None,
                    protocol="isolated",
                    expected_hicache_size_gb=20.0,
                )
                with self.assertRaises(exp0.SkipExperiment):
                    exp0.preflight(
                        base_url="http://server",
                        manifest=manifest(65_408),
                        server_log=log,
                        api_key=None,
                        protocol="plan-batch",
                        expected_hicache_size_gb=20.0,
                    )

        self.assertEqual(checked["filler_tokens"], 88_512)
        self.assertEqual(checked["min_cached_tokens"], 65_472)
        self.assertEqual(checked["isolated_required_l2_tokens"], 153_984)
        self.assertEqual(checked["plan_batch_required_l2_tokens"], 743_232)

    def test_128k_isolated_is_rejected_at_20gb(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "server.log"
            log.write_text(
                "Allocating kv hierarchical KV host pool: 284608 tokens, "
                "20.00 GB host memory.\n"
            )
            with patch.object(
                exp0, "request_json", return_value=server_info(139_264, 139_264)
            ):
                with self.assertRaises(exp0.SkipExperiment):
                    exp0.preflight(
                        base_url="http://server",
                        manifest=manifest(130_944),
                        server_log=log,
                        api_key=None,
                        protocol="isolated",
                        expected_hicache_size_gb=20.0,
                    )


class TestSummarize(unittest.TestCase):
    def test_three_run_paired_summary(self):
        records = []
        for placement, l3_offset in (("local", 15), ("remote", 18)):
            for run_number in range(1, 4):
                for prefix_number in range(10):
                    for phase, ttft in (
                        ("l2", 10 + prefix_number),
                        ("l3", l3_offset + prefix_number),
                    ):
                        records.append(
                            {
                                "length": "64k",
                                "l3_placement": placement,
                                "run_id": f"run-{run_number}",
                                "prefix_id": f"p{prefix_number:02d}",
                                "phase": phase,
                                "ttft_ms": ttft,
                                "manifest_sha256": "same",
                                "accepted": True,
                                "_source": "test",
                            }
                        )

        result = exp0.summarize(records)

        self.assertEqual(len(result["run_summaries"]), 6)
        self.assertEqual(len(result["aggregate"]), 2)
        self.assertEqual(result["aggregate"][0]["l3_placement"], "local")
        self.assertEqual(
            result["aggregate"][0]["median_of_run_median_delta_ttft_ms"], 5
        )
        self.assertEqual(
            result["aggregate"][0]["median_of_run_median_ttft_l2_ms"], 14.5
        )
        self.assertEqual(
            result["aggregate"][0]["median_of_run_median_ttft_l3_ms"], 19.5
        )
        self.assertTrue(result["aggregate"][0]["all_run_medians_positive"])
        self.assertEqual(result["aggregate"][1]["l3_placement"], "remote")
        self.assertEqual(
            result["aggregate"][1]["median_of_run_median_delta_ttft_ms"], 8
        )

    def test_missing_pair_is_rejected(self):
        record = {
            "length": "64k",
            "l3_placement": "local",
            "run_id": "run-1",
            "prefix_id": "p00",
            "phase": "l2",
            "ttft_ms": 1,
            "manifest_sha256": "same",
            "accepted": True,
            "_source": "test",
        }
        with self.assertRaises(exp0.SummaryError):
            exp0.summarize([record])


if __name__ == "__main__":
    unittest.main()
