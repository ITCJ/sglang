"""Check HTTP rejection/disconnect and the deployable command/evidence boundary."""

import asyncio
import contextlib
import io
import json
import socket
import sys
import tempfile
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_mempool_router import payload, registration
from test_service_gate import service_logs

from ascend_mempool_pd.mempool_router import MainRouter, RequestRejected
from ascend_mempool_pd.mempool_router_protocol import CONFIG_ENV, RouterConfig
from ascend_mempool_pd.mempool_router_transport import MainServer, attach_subrouter

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import run_mempool_min as runner
import verify_mempool_min as verifier
from ticket10_processes import Peer, Processes


class TestHTTPEntry(unittest.IsolatedAsyncioTestCase):
    async def test_http_rejection_and_client_disconnect_do_not_free_resources(self):
        main = MainRouter(
            RouterConfig("p", "d", "/model"),
            "cpu",
            hasher=lambda ids, page_size: [
                str(i) for i in range(len(ids) // page_size)
            ],
        )
        main.register(registration("prefill"))
        main.register(registration("decode"))
        server = MainServer(main)

        async def body():
            return payload()

        handler = asyncio.create_task(server.generate(SimpleNamespace(json=body)))
        await asyncio.sleep(0)
        before = main.status()
        response = await server.generate(SimpleNamespace(json=body))
        self.assertEqual(response.status, 429)
        self.assertEqual(main.status(), before)
        handler.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await handler
        self.assertEqual(main.status(), before)
        self.assertTrue(all(not f.cancelled() for f in server.clients.values()))
        for future in server.clients.values():
            future.cancel()


class TestPreflightIdentity(unittest.TestCase):
    def make_runner(self, root, *, change=None, disconnect=False):
        def send(kind, *, value):
            self.assertEqual(kind, "identity")
            peer.value = json.loads(json.dumps(value))
            if change is not None:
                change(peer.value)

        def expect(kind):
            self.assertEqual(kind, "identity")
            if disconnect:
                raise ConnectionError("peer disconnected")
            return dict(value=peer.value)

        peer = SimpleNamespace(send=send, expect=expect, barrier=lambda phase: None)
        config = RouterConfig("p", "d", "/model")
        args = SimpleNamespace(
            role="prefill", mode="acceptance", run_id="identity-test"
        )
        return runner.Runner(args, config, peer, root)

    @contextlib.contextmanager
    def environment(self):
        def git(command, **kwargs):
            if command[1] == "diff":
                return b""
            if command[1] == "ls-files":
                return ""
            if command[1] == "rev-parse":
                return "a" * 40
            self.fail(f"unexpected subprocess: {command}")

        with (
            patch.object(runner.socket, "if_nameindex", return_value=[(1, "bond4")]),
            patch.object(Path, "is_dir", return_value=True),
            patch.object(
                runner,
                "model_identity",
                return_value={"manifest": {"config.json": "hash-a"}},
            ),
            patch.object(runner, "version", return_value="1.0"),
            patch.object(runner.subprocess, "check_output", side_effect=git),
            patch.object(runner.subprocess, "run") as npu_smi,
            patch.object(runner, "available_port"),
            patch.object(
                Path, "read_text", return_value="MemAvailable: 999999999 kB\n"
            ),
        ):
            yield npu_smi

    def test_mismatch_keeps_both_identities_and_names_the_differing_field(self):
        for category, key, value in (
            ("versions", "torch-npu", "2.0"),
            ("model.manifest", "config.json", "hash-b"),
            ("untracked_sha256", "python/local.py", "hash-c"),
        ):

            def change(identity):
                target = identity
                for part in category.split("."):
                    target = target[part]
                target[key] = value

            with (
                self.subTest(field=category + "." + key),
                tempfile.TemporaryDirectory() as folder,
            ):
                root = Path(folder)
                instance = self.make_runner(root, change=change)
                with (
                    self.environment() as npu_smi,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    with self.assertRaisesRegex(ValueError, "P/D") as error:
                        instance.preflight()
                    npu_smi.assert_not_called()
                self.assertTrue((root / "identity.json").is_file())
                local = json.loads((root / "identity.json").read_text())
                peer = json.loads((root / "peer-identity.json").read_text())
                report = json.loads((root / "identity-diff.json").read_text())
                self.assertNotEqual(local, peer)
                self.assertEqual(report["status"], "failed")
                self.assertEqual(
                    report["differences"][0]["field"], category + "." + key
                )
                self.assertEqual(report["differences"][0]["peer"], value)
                self.assertIn(category + "." + key, str(error.exception))
                self.assertEqual(instance.report["identity_check"], report)

    def test_matching_identities_keep_preflight_enabled(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            instance = self.make_runner(root)
            with self.environment() as npu_smi:
                instance.preflight()
                npu_smi.assert_called_once()
            report = json.loads((root / "identity-diff.json").read_text())
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["differences"], [])

    def test_peer_disconnect_still_preserves_local_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            instance = self.make_runner(root, disconnect=True)
            with self.environment(), self.assertRaisesRegex(ConnectionError, "peer"):
                instance.preflight()
            self.assertTrue((root / "identity.json").is_file())
            self.assertIn("identity", instance.report)


class TestMinRunner(unittest.TestCase):
    def test_prefix_reference_accepts_four_real_decode_requests_through_runner(self):
        logs = service_logs(
            (127, 127, 127, 127), layers=78, indexer_layer_ids=verifier.PRODUCERS
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            stage = root / "reference"
            stage.mkdir()
            (stage / "prefill.log").write_text(logs["prefill"])
            peer = SimpleNamespace(
                barrier=lambda phase: None,
                check=lambda: None,
                send=lambda *args, **kwargs: None,
            )
            config = RouterConfig(
                "p", "d", "/model", prefix_pages=40, verify_prefix=True
            )
            args = SimpleNamespace(
                role="prefill",
                mode="prefix-acceptance",
                config=root / "config.json",
                run_id="cpu",
            )
            instance = runner.Runner(args, config, peer, root)
            instance.cases = [
                dict(
                    name="cold-prefix-" + name,
                    input_ids=[7] * length,
                    sampling_params=dict(
                        temperature=0,
                        top_k=1,
                        top_p=1,
                        max_new_tokens=128,
                        ignore_eos=True,
                    ),
                )
                for name, length in zip("abce", (4096, 5120, 4096, 4096))
            ]
            results = [
                dict(
                    output_ids=[1] * 128,
                    meta_info=dict(
                        prompt_tokens=len(case["input_ids"]), completion_tokens=128
                    ),
                )
                for case in instance.cases
            ]
            popen = runner.subprocess.Popen

            def launch(command, **kwargs):
                # Replace only the external NPU/router programs; keep real child ownership/cleanup.
                if command[0] == "bash" or "sglang_router.launch_router" in command:
                    command = [sys.executable, "-c", "import time; time.sleep(90)"]
                return popen(command, **kwargs)

            now = [0]

            def clock():
                now[0] += 100
                return now[0]

            with (
                patch.object(runner.subprocess, "Popen", side_effect=launch),
                patch.object(
                    runner, "wait_http", side_effect=lambda url, timeout, watch: watch()
                ),
                patch.object(runner, "request_json", side_effect=results),
                patch.object(
                    runner,
                    "receive_log",
                    side_effect=lambda peer, path: path.write_text(logs["decode"]),
                ),
                patch.object(
                    runner,
                    "time",
                    SimpleNamespace(monotonic=clock, sleep=runner.time.sleep),
                ),
            ):
                instance.stage(target=False)
            evidence = json.loads((stage / "evidence.json").read_text())
            self.assertEqual(evidence["decode_requests"], 4)
            self.assertEqual(evidence["zero_decode_requests"], 0)
            self.assertEqual(instance.reference, results)
            self.assertEqual(instance.report["stages"], ["reference"])

    def test_prefix_acceptance_has_effective_config_and_layout_based_memory_budget(
        self,
    ):
        config_path = (
            Path(__file__).resolve().parents[2] / "configs/mempool-min.example.json"
        )
        for role in ("prefill", "decode"):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(
                    runner.main(
                        [
                            "--role",
                            role,
                            "--mode",
                            "prefix-acceptance",
                            "--config",
                            str(config_path),
                            "--run-id",
                            "prefix-describe",
                            "--describe",
                        ]
                    ),
                    0,
                )
            data = json.loads(output.getvalue())
            self.assertEqual(data["config"]["prefix_pages"], 40)
            self.assertTrue(data["config"]["verify_prefix"])
            self.assertEqual(data["target_environment"]["SKIP_SERVER_WARMUP"], "0")
            self.assertTrue(
                data["target_environment"][CONFIG_ENV].endswith(
                    "/effective-config.json"
                )
            )
            budget = data["memory_budget"]
            self.assertEqual(
                budget["bm_bytes"], (208 if role == "prefill" else 16) * 2**30
            )
            self.assertEqual(
                budget["verification_bytes"], (32 if role == "prefill" else 0) * 2**30
            )
        self.assertEqual(
            runner.memory_budget(RouterConfig("p", "d", "/model"), "prefill")[
                "bm_bytes"
            ],
            176 * 2**30,
        )

    def test_paired_timeout_retains_quota_and_reaps_only_owned_children(self):
        now = [0.0]
        config = RouterConfig("p", "d", "/model", operation_timeout=0.1)
        main = MainRouter(
            config,
            "cpu",
            clock=lambda: now[0],
            hasher=lambda ids, page_size: [
                str(i) for i in range(len(ids) // page_size)
            ],
        )
        main.register(registration("prefill"))
        main.register(registration("decode"))
        status_reads = []
        left, right = socket.socketpair()
        with (
            tempfile.TemporaryDirectory() as folder,
            Peer(left, 5) as p,
            Peer(right, 5) as d,
        ):
            p_root, d_root = Path(folder) / "prefill", Path(folder) / "decode"
            with Processes(p_root) as p_jobs, Processes(d_root) as d_jobs:
                p_child = p_jobs.start(
                    "worker", [sys.executable, "-c", "import time; time.sleep(30)"]
                )
                d_child = d_jobs.start(
                    "worker", [sys.executable, "-c", "import time; time.sleep(30)"]
                )
                p_runner = runner.Runner(
                    SimpleNamespace(role="prefill", mode="acceptance"),
                    config,
                    p,
                    p_root,
                )
                d_runner = runner.Runner(
                    SimpleNamespace(role="decode", mode="acceptance"), config, d, d_root
                )
                p_runner.cases = [payload()]

                def http(url, *, payload=None, timeout=10):
                    if payload is not None:
                        try:
                            req = main.submit(payload)
                        except RequestRejected as exc:
                            raise urllib.error.HTTPError(
                                url, exc.status, str(exc), {}, None
                            )
                        command = main.outbox.popleft()
                        main.receive(
                            dict(
                                command,
                                kind="alloc_ack",
                                ranks=[
                                    dict(
                                        rank=i,
                                        slot=0,
                                        pages=[dict(page_size=128)],
                                        native=dict(row=1, pages=33, tokens=4224),
                                        message={},
                                    )
                                    for i in range(16)
                                ],
                            )
                        )
                        (d_root / "decode.log").write_text(
                            "\n".join(
                                f"mempool sr role=decode rank={i} event=alloc rid={req['rid']} room={req['room']} free=15"
                                for i in range(16)
                            )
                        )
                        raise TimeoutError("intentional client disconnect")
                    status_reads.append(True)
                    if len(status_reads) > 1:
                        now[0] = 1.0
                    main.check_timeouts()
                    return main.status()

                with (
                    patch.object(runner, "request_json", side_effect=http),
                    ThreadPoolExecutor(2) as workers,
                ):
                    tasks = [
                        workers.submit(
                            p_runner.timeout_case,
                            p_jobs,
                            p_child,
                            p_root,
                            "http://main",
                        ),
                        workers.submit(
                            d_runner.timeout_case,
                            d_jobs,
                            d_child,
                            d_root,
                            "http://main",
                        ),
                    ]
                    for task in tasks:
                        task.result(timeout=5)
                report = json.loads((p_root / "timeout.json").read_text())
                self.assertEqual(report["status"], "passed")
                self.assertEqual(report["retained_native_ranks"], 16)
                self.assertEqual(
                    report, json.loads((d_root / "timeout.json").read_text())
                )
                snapshots = json.loads((p_root / "timeout-snapshots.json").read_text())
                with self.assertRaisesRegex(ValueError, "lacks per-rank"):
                    verifier.check_timeout(snapshots["before"], snapshots["after"], "")
            self.assertIsNotNone(p_child.poll())
            self.assertIsNotNone(d_child.poll())
            with ThreadPoolExecutor(2) as workers:
                tasks = [
                    workers.submit(p_runner.final_timeout_evidence, p_root),
                    workers.submit(d_runner.final_timeout_evidence, d_root),
                ]
                for task in tasks:
                    task.result(timeout=5)
            self.assertTrue(
                json.loads((p_root / "timeout.json").read_text())[
                    "process_cleanup_verified"
                ]
            )
            snapshots = json.loads((p_root / "timeout-snapshots.json").read_text())
            room = snapshots["after"]["active"]["room"]
            final_log = (p_root / "decode-fault-final.log").read_text()
            with self.assertRaisesRegex(ValueError, "freed native/alive"):
                verifier.check_timeout(
                    snapshots["before"],
                    snapshots["after"],
                    final_log
                    + f"\nmempool native_free role=decode rank=0 room={room}\n",
                )

    def test_each_completed_forward_requires_exactly_one_resident_and_execution(self):
        verifier.check_step_counts([(1, 1, 1), (2, 1, 1)], 2)
        verifier.check_step_counts([], 0)
        for samples in ([(1, 1, 1)], [(1, 2, 1), (2, 1, 1)], [(1, 1, 1), (3, 1, 1)]):
            with self.assertRaisesRegex(ValueError, "per-step"):
                verifier.check_step_counts(samples, 2)

    def test_describe_is_cpu_only_and_reference_does_not_load_subrouter(self):
        config_path = (
            Path(__file__).resolve().parents[2] / "configs/mempool-min.example.json"
        )
        args = runner.parse_args(
            [
                "--role",
                "decode",
                "--mode",
                "acceptance",
                "--config",
                str(config_path),
                "--run-id",
                "cpu-describe",
                "--describe",
            ]
        )
        config = RouterConfig.read(str(config_path))
        target = runner.service_environment(
            config, args, Path("/tmp/logs"), target=True
        )
        reference = runner.service_environment(
            config, args, Path("/tmp/logs"), target=False
        )
        self.assertIn(CONFIG_ENV, target)
        self.assertNotIn(CONFIG_ENV, reference)
        self.assertEqual(target["SKIP_SERVER_WARMUP"], "0")
        self.assertEqual(reference["SKIP_SERVER_WARMUP"], "0")
        for key in (
            "MODEL_PATH",
            "PREFILL_CAPACITY",
            "DECODE_CAPACITY",
            "PAGE_SIZE",
            "CONTEXT_LENGTH",
            "MAX_TOTAL_TOKENS",
            "GRAPH_MODE",
        ):
            self.assertEqual(target[key], reference[key])
        self.assertEqual(target["PAGE_SIZE"], str(config.page_size))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = runner.main(
                [
                    "--role",
                    "prefill",
                    "--mode",
                    "serve",
                    "--config",
                    str(config_path),
                    "--run-id",
                    "cpu-describe",
                    "--describe",
                ]
            )
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["mode"], "serve")

    def test_worker_checks_actual_pool_allocator_transfer_and_dram_before_register(
        self,
    ):
        config = RouterConfig("p", "d", "/model")
        args = SimpleNamespace(
            page_size=128,
            skip_server_warmup=False,
            disable_radix_cache=True,
            chunked_prefill_size=-1,
            max_prefill_tokens=8192,
            mempool_prefill_capacity=8192,
            mempool_decode_capacity=128,
            disable_cuda_graph=True,
            model_path="/model",
            context_length=8448,
            dp_size=1,
            pp_size=1,
        )
        pool = SimpleNamespace(page_size=128, indexer_layer_ids=[], index_head_dim=128)
        allocator = SimpleNamespace(
            page_size=128, get_kvcache=lambda: pool, available_size=lambda: 16384
        )
        transfer = SimpleNamespace(page_size=128)
        runtime_config = SimpleNamespace(page_size=128)
        layout = SimpleNamespace(
            layers=78,
            dim=576,
            dtype="bfloat16",
            slots=16,
            prompt_tokens=8192,
            decode_tokens=128,
        )
        control = SimpleNamespace(
            local=SimpleNamespace(
                role="prefill",
                tp_rank=1,
                session="p",
                tp_size=16,
                layout=layout,
                transfer_layout=transfer,
            ),
            peer=SimpleNamespace(session="d"),
        )
        service = SimpleNamespace(
            scheduler=SimpleNamespace(
                server_args=args,
                max_new_tokens_limit=None,
                max_req_len=8447,
                token_to_kv_pool_allocator=allocator,
                req_to_token_pool=SimpleNamespace(available_size=lambda: 16),
                model_config=SimpleNamespace(vocab_size=100),
            ),
            control=control,
            runtime=SimpleNamespace(
                config=runtime_config,
                layout=layout,
                manager=SimpleNamespace(
                    numa_node=0,
                    layout=SimpleNamespace(
                        prompt=SimpleNamespace(tokens=8192),
                        decode=SimpleNamespace(tokens=128),
                    ),
                ),
            ),
            _effect=lambda *args: None,
            tick=SimpleNamespace(reply_to="d"),
        )
        with (
            patch.dict(
                "os.environ",
                {CONFIG_ENV: "/config", "SGLANG_NPU_MEMPOOL_ROUTER_RUN_ID": "cpu"},
            ),
            patch.object(RouterConfig, "read", return_value=config),
            patch(
                "ascend_mempool_pd.mempool_router_transport.model_digest",
                return_value="sha",
            ),
        ):
            sr = attach_subrouter(service, lambda value: [value] * 16)
            self.assertEqual(sr.shard.page_size, 128)
            self.assertEqual(sr.registration()["page_size"], 128)
            self.assertEqual(sr.registration()["hbm_page_size"], 128)
            for component in (args, pool, allocator, transfer, runtime_config):
                component.page_size = 64
                with (
                    self.subTest(component=component),
                    self.assertRaisesRegex(ValueError, "page_size"),
                ):
                    attach_subrouter(service, lambda value: [value] * 16)
                component.page_size = 128

    def test_npu_evidence_rejects_old_page_contract_and_budget(self):
        config = RouterConfig("p", "d", "/model")
        snapshot = dict(
            registrations={r: registration(r) for r in ("prefill", "decode")},
            ledger=dict(p_alive=dict(total=1024), d_alive=dict(total=16)),
            completed=[
                dict(
                    allocations={
                        r: [
                            dict(
                                rank=rank,
                                slot=0,
                                pages=[
                                    dict(logical_page=i, page_size=128)
                                    for i in range(count)
                                ],
                                native=dict(pages=33, tokens=4224),
                            )
                            for rank in range(16)
                        ]
                        for r, count in (("prefill", 64), ("decode", 1))
                    }
                )
            ],
        )

        def check():
            verifier.check_pages(
                snapshot,
                payload(),
                config.page_size,
                config.prompt_capacity,
                config.decode_capacity,
            )

        check()
        snapshot["registrations"]["decode"]["ranks"][15]["page_size"] = 64
        with self.assertRaisesRegex(ValueError, "mismatch"):
            check()
        snapshot["registrations"]["decode"]["ranks"][15]["page_size"] = 128
        snapshot["ledger"]["p_alive"]["total"] = 2048
        with self.assertRaisesRegex(ValueError, "ledger"):
            check()

    def test_acceptance_requires_full_ids_and_an_actual_early_stop(self):
        case = dict(
            input_ids=[7] * 64,
            sampling_params=dict(max_new_tokens=128, ignore_eos=False),
        )
        result = dict(
            output_ids=[1, 2],
            meta_info=dict(
                prompt_tokens=64, completion_tokens=2, finish_reason=dict(type="stop")
            ),
        )
        verifier.check_response(case, result)
        result["meta_info"]["finish_reason"]["type"] = "length"
        with self.assertRaisesRegex(ValueError, "early-eos"):
            verifier.check_response(case, result)
        case["sampling_params"]["ignore_eos"] = True
        with self.assertRaisesRegex(ValueError, "wrong length"):
            verifier.check_response(case, result)
        result.pop("output_ids")
        with self.assertRaisesRegex(ValueError, "full output"):
            verifier.check_response(case, result)
