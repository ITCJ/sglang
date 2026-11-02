"""K1 session acceptance keeps distinct checks with two service starts."""

import contextlib
import errno
import io
import json
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import msgspec
import run_mempool_min
import verify_mempool_session
from test_mempool_router import registration

from ascend_mempool_pd.mempool_router_protocol import RouterConfig


class TestSessionAcceptance(unittest.TestCase):
    def test_model_distributed_port_is_checked_at_each_stage_boundary(self):
        config = RouterConfig("p", "d", "/model")
        for role in ("prefill", "decode"):
            self.assertIn(10000, run_mempool_min.service_ports(config, role))

    def test_prefix_cli_enables_single_segments_and_reports_both_budgets(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.json"
            config.write_text(
                json.dumps(dict(prefill_host="p", decode_host="d", model_path="/model"))
            )
            for role, gib in (("prefill", 96), ("decode", 8)):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(
                        run_mempool_min.main(
                            [
                                "--role",
                                role,
                                "--mode",
                                "prefix-sharing-acceptance",
                                "--config",
                                str(config),
                                "--run-id",
                                "k1-08-cpu",
                                "--describe",
                            ]
                        ),
                        0,
                    )
                value = json.loads(output.getvalue())
                actual = msgspec.convert(value["config"], type=RouterConfig)
                self.assertEqual(
                    actual.replicas(),
                    dict(alive_main=8, alive_index=1, prefix_main=1, prefix_index=1),
                )
                self.assertEqual(actual.read_assignment, "by_segment")
                self.assertEqual(value["memory_budget"]["bm_bytes"], gib * 2**30)
                self.assertEqual(value["service_rounds"], 2)

    def test_index_cold_diagnosis_keeps_layout_but_disables_reuse(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.json"
            config.write_text(
                json.dumps(dict(prefill_host="p", decode_host="d", model_path="/model"))
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = run_mempool_min.main(
                    [
                        "--role",
                        "prefill",
                        "--mode",
                        "index-cold-diagnosis",
                        "--config",
                        str(config),
                        "--run-id",
                        "k1-07-cold",
                        "--replay-responses",
                        "/saved/responses.json",
                        "--describe",
                    ]
                )
            value = json.loads(output.getvalue())
            actual = msgspec.convert(value["config"], type=RouterConfig)
            self.assertEqual(result, 0)
            self.assertEqual(
                actual.replicas(),
                dict(alive_main=8, alive_index=1, prefix_main=16, prefix_index=16),
            )
            self.assertTrue(actual.decode_shared)
            self.assertFalse(actual.match_prefix)
            self.assertTrue(actual.verify_prefix)
            self.assertEqual(value["service_rounds"], 1)

    def test_index_experiment_preserves_the_full_acceptance_six_request_contract(self):
        from test_mempool_full import CharacterTokenizer

        config = RouterConfig(
            "p", "d", "/model", prefix_pages=40, prefill_index_shared=True
        )
        peer = SimpleNamespace(
            send=lambda *args, **kwargs: None, barrier=lambda *args: None
        )
        for mode, count in (
            ("full-acceptance", 6),
            ("index-sharing-acceptance", 9),
            ("prefix-sharing-acceptance", 6),
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                runner = run_mempool_min.Runner(
                    SimpleNamespace(role="prefill", mode=mode),
                    config,
                    peer,
                    Path(folder),
                )
                with patch(
                    "transformers.AutoTokenizer.from_pretrained",
                    return_value=CharacterTokenizer(),
                ):
                    runner.prepare_full_cases()
                self.assertEqual(len(runner.cases), count)

    def test_index_cli_enables_single_index_without_changing_prefix_replicas(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.json"
            config.write_text(
                json.dumps(dict(prefill_host="p", decode_host="d", model_path="/model"))
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = run_mempool_min.main(
                    [
                        "--role",
                        "prefill",
                        "--mode",
                        "index-sharing-acceptance",
                        "--config",
                        str(config),
                        "--run-id",
                        "k1-07-cpu",
                        "--describe",
                    ]
                )
            value = json.loads(output.getvalue())
            actual = msgspec.convert(value["config"], type=RouterConfig)
            self.assertEqual(result, 0)
            self.assertEqual(
                actual.replicas(),
                dict(alive_main=8, alive_index=1, prefix_main=16, prefix_index=16),
            )
            self.assertTrue(actual.decode_shared)
            self.assertEqual(value["service_rounds"], 2)
            self.assertEqual(value["memory_budget"]["bm_bytes"], 105 * 2**30)

    def test_session_cli_keeps_shared_decode_with_eight_allocations(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.json"
            config.write_text(
                json.dumps(
                    dict(
                        prefill_host="p",
                        decode_host="d",
                        model_path="/model",
                        prefill_shared=True,
                        decode_shared=True,
                    )
                )
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                run_mempool_min.main(
                    [
                        "--role",
                        "decode",
                        "--mode",
                        "session-acceptance",
                        "--config",
                        str(config),
                        "--run-id",
                        "k1-06-cpu",
                        "--describe",
                    ]
                )
            value = json.loads(output.getvalue())
            self.assertTrue(value["config"]["decode_shared"])
            self.assertTrue(value["config"]["prefill_shared"])
            self.assertEqual(value["service_rounds"], 2)
            self.assertEqual(value["memory_budget"]["bm_bytes"], 8 * 2**30)

    def test_shared_cli_describes_eight_prefill_replicas_and_two_model_starts(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.json"
            config.write_text(
                json.dumps(dict(prefill_host="p", decode_host="d", model_path="/model"))
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = run_mempool_min.main(
                    [
                        "--role",
                        "prefill",
                        "--mode",
                        "p-sharing-acceptance",
                        "--config",
                        str(config),
                        "--run-id",
                        "k1-05-cpu",
                        "--describe",
                    ]
                )
            value = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertTrue(value["config"]["prefill_shared"])
            self.assertEqual(value["service_rounds"], 2)
            self.assertEqual(value["memory_budget"]["bm_bytes"], 120 * 2**30)

    def test_cleanup_checks_actual_session_ports_not_reserved_range(self):
        config = RouterConfig(
            "p",
            "d",
            "/model",
            prefix_pages=40,
            prefill_shared=True,
            decode_shared=True,
            prefill_index_shared=True,
            prefix_shared=True,
        )
        # NPU002: 26088 belongs to an unrelated established connection to :443.
        decode = run_mempool_min.service_ports(config, "decode")
        self.assertNotIn(26088, decode)
        self.assertEqual(decode, {8001, *(25686 + 33 * rank for rank in range(16))})
        prefill = run_mempool_min.service_ports(config, "prefill")
        self.assertEqual(
            prefill,
            {
                8000,
                6699,
                16700,
                19000,
                *(25670 + 33 * rank for rank in range(16)),
            },
        )

    def test_service_ports_preserves_pair_and_shared_group_endpoints(self):
        for options in ({}, {"prefill_shared": True}, {"decode_shared": True}):
            config = RouterConfig("p", "d", "/model", **options)
            for side, role in enumerate(("prefill", "decode")):
                with self.subTest(options=options, role=role):
                    if options:
                        nic = {
                            25670 + 8 * group + 5 * member + 2 * side
                            for group in range(8)
                            for member in range(2)
                        }
                        stores = {19000 + 2 * group for group in range(8)}
                    else:
                        nic = {25670 + 2 * rank + side for rank in range(16)}
                        stores = set(range(19000, 19016))
                    expected = nic | (
                        {8000, 6699, 16700} | stores if role == "prefill" else {8001}
                    )
                    self.assertEqual(
                        run_mempool_min.service_ports(config, role), expected
                    )

    def test_time_wait_release_requires_successful_rebind_and_saves_evidence(self):
        clock = [0.0]
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "port.json"
            with (
                patch("verify_mempool_session.socket.socket") as socket_type,
                patch("verify_mempool_session.Path.read_text", autospec=True) as read,
                patch(
                    "verify_mempool_session.time.monotonic",
                    side_effect=lambda: clock[0],
                ),
                patch(
                    "verify_mempool_session.time.sleep",
                    side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay),
                ),
            ):
                read.side_effect = lambda path, **kw: (
                    "header\n0: 2048780A:65E2 00000000:0000 06 0:0 0:0 0 0 0 0\n"
                    if path.name == "tcp"
                    else "header\n"
                )
                probe = socket_type.return_value.__enter__.return_value
                probe.bind.side_effect = [OSError(errno.EADDRINUSE, "busy"), None]
                verify_mempool_session.released_port(
                    "10.120.72.32", 26082, timeout=5, evidence_path=report
                )
                self.assertEqual(probe.bind.call_count, 2)
                probe.listen.assert_called_once()
            evidence = json.loads(report.read_text())
            self.assertEqual(evidence["status"], "passed")
            self.assertEqual(evidence["first_blocked"][0]["state"], "TIME_WAIT")
            self.assertEqual(evidence["waited_seconds"], 1.0)

    def test_time_wait_policy_rejects_live_unknown_and_timed_out_ports(self):
        for states in (("0A",), ("01",), ("08",), ("06", "0A"), (), ("06",)):
            with self.subTest(states=states), tempfile.TemporaryDirectory() as folder:
                clock = [0.0]
                report = Path(folder) / "port.json"
                snapshot = "header\n" + "".join(
                    f"0: 2048780A:65E2 00000000:0000 {state} 0:0 0:0 0 0 0 0\n"
                    for state in states
                )
                with (
                    patch("verify_mempool_session.socket.socket") as socket_type,
                    patch(
                        "verify_mempool_session.Path.read_text",
                        autospec=True,
                        side_effect=lambda path, **kw: (
                            snapshot if path.name == "tcp" else "header\n"
                        ),
                    ),
                    patch(
                        "verify_mempool_session.time.monotonic",
                        side_effect=lambda: clock[0],
                    ),
                    patch(
                        "verify_mempool_session.time.sleep",
                        side_effect=lambda delay: clock.__setitem__(
                            0, clock[0] + delay
                        ),
                    ),
                ):
                    probe = socket_type.return_value.__enter__.return_value
                    probe.bind.side_effect = OSError(errno.EADDRINUSE, "busy")
                    with self.assertRaises(
                        TimeoutError if states == ("06",) else OSError
                    ):
                        verify_mempool_session.released_port(
                            "10.120.72.32", 26082, timeout=2, evidence_path=report
                        )
                    self.assertEqual(clock[0], 2 if states == ("06",) else 0)
                    probe.listen.assert_not_called()
                self.assertEqual(json.loads(report.read_text())["status"], "failed")

    def test_post_shutdown_port_probe_allows_time_wait_but_rejects_a_listener(self):
        with socket.socket() as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", 0))
            server.listen()
            address = server.getsockname()
            with socket.create_connection(address) as client:
                accepted, _ = server.accept()
                accepted.close()
                self.assertEqual(client.recv(1), b"")
            with self.assertRaises(OSError):
                verify_mempool_session.released_port(*address)
        verify_mempool_session.released_port(*address)

    def test_accounting_requires_completed_prompt_and_every_reader(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "snapshots.json").write_text(
                json.dumps([dict(completed=[dict(rid="r1")])])
            )
            (root / "responses.json").write_text(
                json.dumps(dict(cases=[dict(input_ids=[7] * 256)]))
            )
            free = "\n".join(
                "mempool free_accounting data="
                + json.dumps(dict(rid="r1", rank=r, readers_remaining=0))
                for r in range(16)
            )
            for role in ("prefill", "decode"):
                (root / f"{role}.log").write_text(free + "\n")
            ready = root / "prefill.log"
            ready.write_text(
                ready.read_text()
                + 'mempool ready_accounting data={"rid":"r1","ready_tokens":0,"counter_tokens":0}\n'
            )
            with self.assertRaisesRegex(ValueError, "complete prompt"):
                verify_mempool_session.accounting(root)
            ready.write_text(
                ready.read_text()
                + 'mempool ready_accounting data={"rid":"r1","ready_tokens":256,"counter_tokens":256}\n'
            )
            self.assertEqual(verify_mempool_session.accounting(root)["requests"], 1)
            (root / "decode.log").write_text(free.replace('"rank": 15', '"rank": 14'))
            with self.assertRaisesRegex(ValueError, "every replica"):
                verify_mempool_session.accounting(root)

    def test_cli_exposes_two_service_starts_without_formal_reference(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.json"
            config.write_text(
                json.dumps(dict(prefill_host="p", decode_host="d", model_path="/model"))
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = run_mempool_min.main(
                    [
                        "--role",
                        "prefill",
                        "--mode",
                        "session-acceptance",
                        "--config",
                        str(config),
                        "--run-id",
                        "k1-ticket02-001",
                        "--describe",
                    ]
                )
            description = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(description["service_rounds"], 2)
            self.assertTrue(description["config"]["match_prefix"])
            self.assertIn("fail-stop", description["stages"][1])
            self.assertIn("restart", description["stages"][2])
            self.assertEqual(
                description["target_environment"]["SGLANG_NPU_MEMPOOL_ROUTER_RUN_ID"],
                "k1-ticket02-001",
            )

    def test_actual_registration_replay_rejects_one_under_capacity_rank(self):
        config = RouterConfig("p", "d", "/model")
        actual = {r: registration(r) for r in ("prefill", "decode")}
        before = json.dumps(actual, sort_keys=True)
        result = verify_mempool_session.capacity_check(config, "cpu", actual)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["execution"], "CPU replay of actual NPU registration")
        self.assertEqual(json.dumps(actual, sort_keys=True), before)


if __name__ == "__main__":
    unittest.main()
