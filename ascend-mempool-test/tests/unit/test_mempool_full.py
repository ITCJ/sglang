"""Ticket04 public fixture/output and complete evidence contracts."""

import contextlib
import copy
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

import mempool_logical_cases as logical
import run_mempool_min
import test_mempool_prefix_evidence as evidence_fixtures
import verify_mempool_full
import verify_mempool_min
import verify_mempool_reuse
from test_mempool_router import payload, registration
from ticket10_processes import Peer

from ascend_mempool_pd.mempool_router import MainRouter, RequestRejected
from ascend_mempool_pd.mempool_router_protocol import RouterConfig


class CharacterTokenizer:
    """External tokenizer stand-in: every character is one observable token."""

    chat_template = "role-tag conversation"

    def encode(self, text, **kwargs):
        return list(map(ord, text))

    def decode(self, ids, **kwargs):
        return "".join(map(chr, ids))

    def apply_chat_template(self, messages, **kwargs):
        text = (
            "".join(f"<{m['role']}>{m['content']}<end>\n" for m in messages)
            + "<assistant>"
        )
        return self.encode(text) if kwargs.get("tokenize") else text


class TestLogicalFixtures(unittest.TestCase):
    def test_abce_uses_exact_whole_templates_and_suffix_changes_the_answer(self):
        tokenizer = CharacterTokenizer()
        cases = logical.make_cases(tokenizer, 128)
        a, b, c, e = cases
        self.assertEqual([len(x["input_ids"]) for x in cases], [4096, 5120, 4096, 4096])
        self.assertEqual(a["input_ids"], b["input_ids"][:4096])
        self.assertEqual(a["input_ids"], c["input_ids"])
        self.assertNotEqual(a["input_ids"][:128], e["input_ids"][:128])
        self.assertEqual(
            a["expected_answer"], dict(place="harbor", pencils=22, route="north")
        )
        self.assertEqual(
            b["expected_answer"], dict(place="harbor", pencils=25, route="south")
        )
        self.assertEqual(
            e["expected_answer"], dict(place="meadow", pencils=27, route="south")
        )
        for case in cases:
            self.assertEqual(
                case["input_ids"],
                tokenizer.apply_chat_template(case["messages"], tokenize=True),
            )
            self.assertTrue(case["rendered_input"].endswith("<assistant>"))
            self.assertFalse(case["sampling_params"]["ignore_eos"])
        self.assertGreater(a["rendered_input"].index("gave away"), 1400)
        self.assertGreater(a["rendered_input"].index("received 9"), 3800)

    def test_load_and_early_eos_have_separate_coverage(self):
        long, early = logical.make_controls(CharacterTokenizer(), 128)
        self.assertEqual([len(long["input_ids"]), len(early["input_ids"])], [4096, 128])
        self.assertTrue(long["sampling_params"]["ignore_eos"])
        self.assertEqual(long["kind"], "load-only")
        self.assertEqual(early["expected_answer"], dict(sum=5))
        self.assertFalse(early["sampling_params"]["ignore_eos"])


class TestLogicalAnswers(unittest.TestCase):
    def test_identical_cold_a_c_differences_are_a_separate_stability_anomaly(self):
        case = dict(
            input_ids=[7] * 4096,
            sampling_params=dict(max_new_tokens=128, ignore_eos=False),
        )
        result = dict(
            output_ids=[1, 2, 3],
            meta_info=dict(
                prompt_tokens=4096, completion_tokens=3, finish_reason=dict(type="stop")
            ),
        )
        cases, results = (
            [copy.deepcopy(case) for _ in range(3)],
            [copy.deepcopy(result) for _ in range(3)],
        )
        self.assertEqual(logical.check_stability(cases, results)["status"], "passed")
        results[2]["output_ids"] = [1, 4, 3]
        evidence = logical.check_stability(cases, results)
        self.assertEqual(evidence["status"], "anomaly")
        self.assertEqual(evidence["ids"]["first_difference"], 1)
        self.assertEqual(
            evidence["comparison"], "reference A versus reference C; same conditions"
        )

    def test_correct_answers_allow_different_ids_and_preserve_first_difference(self):
        case = dict(
            name="logic-a",
            input_ids=[7] * 4096,
            expected_answer=dict(place="harbor", pencils=22, route="north"),
            sampling_params=dict(max_new_tokens=128, ignore_eos=False),
        )
        reference = dict(
            text='{"place":"harbor","pencils":22,"route":"north"}',
            output_ids=[10, 11, 12],
            meta_info=dict(
                prompt_tokens=4096,
                completion_tokens=3,
                finish_reason=dict(type="stop"),
            ),
        )
        target = dict(reference, output_ids=[10, 13, 12])
        report = logical.compare_answers(case, reference, target)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(
            report["ids"],
            dict(equal=False, reference_length=3, target_length=3, first_difference=1),
        )
        self.assertEqual(report["reference"]["answer"], case["expected_answer"])
        self.assertEqual(report["target"]["answer"], case["expected_answer"])

    def test_duplicate_fields_are_invalid_even_when_the_last_value_is_correct(self):
        case = dict(
            name="logic-a",
            input_ids=[7],
            expected_answer=dict(pencils=1),
            sampling_params=dict(max_new_tokens=128, ignore_eos=False),
        )
        good = dict(
            text='{"pencils":1}',
            output_ids=[1, 2],
            meta_info=dict(
                prompt_tokens=1, completion_tokens=2, finish_reason=dict(type="stop")
            ),
        )
        for text in (
            '{"pencils":0,"pencils":1}',
            '{"pencils":true}',
            '{"pencils":1.0}',
            '{"pencils":1,"extra":0}',
            'answer: {"pencils":1}',
        ):
            with self.subTest(text=text):
                bad = dict(good, text=text)
                self.assertEqual(
                    logical.compare_answers(case, good, bad)["status"], "target-failed"
                )
                self.assertEqual(
                    logical.compare_answers(case, bad, good)["status"],
                    "baseline-failed",
                )
        bad = dict(good, meta_info=dict(prompt_tokens=2, completion_tokens=2))
        self.assertEqual(
            logical.compare_answers(case, good, bad)["target"]["category"], "structure"
        )
        for bad in (dict(good, output_ids=None), dict(good, meta_info=None)):
            self.assertEqual(
                logical.compare_answers(case, good, bad)["target"]["category"],
                "structure",
            )


class TestFullReuseEvidence(unittest.TestCase):
    def test_controls_after_e_keep_all_forty_pages_and_require_their_own_receipts(self):
        snapshots, cases, lines = evidence_fixtures.TestPrefixEvidence().evidence(
            reuse=True, full=True, controls=True
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "prefill.log"
            path.write_text("\n".join(lines))
            report = verify_mempool_reuse.check_reuse(path, snapshots, cases, full=True)
            self.assertEqual(report["hit_lengths"], [0, 4096, 3968, 0, 0, 0])
            self.assertEqual(report["pages_per_request"], [32, 40, 40, 40, 40, 40])
            path.write_text("\n".join(lines[:-1]))
            with self.assertRaises(ValueError):
                verify_mempool_reuse.check_reuse(path, snapshots, cases, full=True)

    def test_e_checks_full_store_preservation_after_actual_reuse(self):
        snapshots, cases, lines = evidence_fixtures.TestPrefixEvidence().evidence(
            reuse=True, full=True
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "prefill.log"
            path.write_text("\n".join(lines))
            result = verify_mempool_reuse.check_reuse(path, snapshots, cases, full=True)
            self.assertEqual(result["hit_lengths"], [0, 4096, 3968, 0])
            self.assertEqual(result["pages_per_request"], [32, 40, 40, 40])
            for defect in ("e_hit", "e_overwrite", "e_missing_rank"):
                data, records = copy.deepcopy(snapshots), list(lines)
                if defect == "e_hit":
                    data[-1]["completed"][-1]["hit_len"] = 128
                elif defect == "e_overwrite":
                    next(iter(data[-1]["directory"].values()))["prefix"] = {
                        "wrong": True
                    }
                else:
                    records.pop(
                        next(
                            i
                            for i in range(len(records) - 1, -1, -1)
                            if '"stage": "retained"' in records[i]
                        )
                    )
                path.write_text("\n".join(records))
                with self.subTest(defect=defect), self.assertRaises(ValueError):
                    verify_mempool_reuse.check_reuse(path, data, cases, full=True)


class TestFullEntry(unittest.TestCase):
    def test_length_rejection_is_independent_of_alignment_and_generation_limits(self):
        config = RouterConfig("p", "d", "/model")
        instance = run_mempool_min.Runner(
            SimpleNamespace(role="prefill", mode="full-acceptance"),
            config,
            None,
            Path("/unused"),
        )
        status = dict(active=None, ledger={}, directory={}, completed=[])

        def missing_length_guard(url, *, payload=None, timeout=10):
            if payload is None:
                return status
            if (
                "text" in payload
                or len(payload["input_ids"]) % 128
                or payload["sampling_params"]["max_new_tokens"] > 128
            ):
                raise urllib.error.HTTPError(url, 400, "invalid input", {}, None)
            return dict(accepted=True)

        with patch.object(
            run_mempool_min, "request_json", side_effect=missing_length_guard
        ):
            with self.assertRaisesRegex(ValueError, "was admitted"):
                instance.rejected_inputs("http://main")

    def test_complete_report_requires_the_fault_cleanup_and_adapter_receipts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            records = {
                "reference/evidence.json": dict(
                    status="formal_service_passed", decode_requests=6, ranks_per_side=16
                ),
                "reference/answers.json": [dict(status="passed")] * 6,
                "reference/stability.json": dict(status="passed"),
                "target/evidence.json": dict(
                    status="passed",
                    requests=6,
                    ranks_per_side=16,
                    graph=True,
                    prefix=dict(
                        status="passed",
                        ranks=16,
                        main_layers=78,
                        index_layers=21,
                        hit_lengths=[0, 4096, 3968, 0, 0, 0],
                        pages_per_request=[32, 40, 40, 40, 40, 40],
                    ),
                    request_evidence=[
                        dict(
                            case=name,
                            output_tokens=128 if i == 4 else 12,
                            decode=[
                                dict(
                                    rank=r,
                                    forwards=127 if i == 4 else 11,
                                    written_kv=127 if i == 4 else 11,
                                )
                                for r in range(16)
                            ],
                        )
                        for i, name in enumerate(verify_mempool_full.NAMES)
                    ],
                ),
                "target/answers.json": [
                    dict(case=name, status="passed", policy=logical.POLICY)
                    for name in verify_mempool_full.NAMES
                ],
                "target/input-rejections.json": dict(
                    status="passed",
                    ledger_unchanged=True,
                    names=["unaligned", "too-long", "generation-limit"],
                ),
                "capacity/evidence.json": dict(
                    status="passed", ledger_unchanged=True, worker_exit=1
                ),
                "timeout/timeout.json": dict(
                    status="passed", process_cleanup_verified=True, worker_exit=1
                ),
                "fetch/decode-report.json": dict(
                    status="passed",
                    checks=[dict(case=name) for name in ("p_miss", "d_miss", "mixed")],
                ),
            }
            records.update(
                {
                    f"{phase}/process-cleanup.json": dict(status="passed", paired=True)
                    for phase in ("reference", "target", "capacity", "timeout")
                }
            )
            for name, value in records.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(value))
            self.assertEqual(verify_mempool_full.summarize(root)["status"], "passed")
            for missing in (
                "capacity/evidence.json",
                "timeout/timeout.json",
                "fetch/decode-report.json",
                "target/process-cleanup.json",
                "reference/stability.json",
            ):
                path = root / missing
                original = path.read_text()
                path.unlink()
                with self.subTest(missing=missing):
                    self.assertEqual(
                        verify_mempool_full.summarize(root)["status"], "failed"
                    )
                path.write_text(original)
            (root / "reference/stability.json").write_text('{"status":"anomaly"}')
            self.assertEqual(verify_mempool_full.summarize(root)["status"], "failed")

    def test_isolated_timeout_starts_without_success_logs_and_retains_allocations(self):
        now = [0.0]
        config = RouterConfig(
            "p", "d", "/model", startup_timeout=5, operation_timeout=0.5
        )
        main = MainRouter(
            config,
            "cpu",
            clock=lambda: now[0],
            hasher=lambda ids, size: [str(i) for i in range(len(ids) // size)],
        )
        for role in ("prefill", "decode"):
            main.register(registration(role))
        left, right = socket.socketpair()
        with (
            tempfile.TemporaryDirectory() as folder,
            Peer(left, 3) as p,
            Peer(right, 3) as d,
        ):
            roots = [Path(folder) / role for role in ("prefill", "decode")]
            instances = [
                run_mempool_min.Runner(
                    SimpleNamespace(
                        role=role,
                        mode="full-acceptance",
                        config=Path(folder) / "config.json",
                        run_id="cpu",
                    ),
                    config,
                    peer,
                    root,
                )
                for role, peer, root in zip(("prefill", "decode"), (p, d), roots)
            ]
            instances[0].cases = [payload()]
            popen = run_mempool_min.subprocess.Popen
            release = roots[1] / "timeout/fault-observed"
            reads = []

            def launch(command, **kwargs):
                if (
                    command[0] == "bash"
                    or "sglang.srt.disaggregation.ascend.mempool_router" in command
                ):
                    code = "import time; time.sleep(30)"
                    if command[0] == "bash":
                        role = "decode" if "decode" in command else "prefill"
                        (Path(kwargs["env"]["LOG_DIR"]) / f"{role}.log").write_text("")
                        if role == "decode":
                            code = f"import time; from pathlib import Path\np = Path({str(release)!r})\nwhile not p.exists(): time.sleep(0.01)\nraise SystemExit(1)"
                    return popen([sys.executable, "-c", code], **kwargs)
                return popen(command, **kwargs)

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
                    (roots[1] / "timeout/decode.log").write_text(
                        "\n".join(
                            f"mempool sr role=decode rank={i} event=alloc rid={req['rid']} room={req['room']} free=15"
                            for i in range(16)
                        )
                    )
                    raise TimeoutError("client disconnected")
                reads.append(True)
                if len(reads) > 1:
                    now[0] = 1.0
                main.check_timeouts()
                if main.fault:
                    release.touch()
                return main.status()

            with (
                patch.object(run_mempool_min.subprocess, "Popen", side_effect=launch),
                patch.object(run_mempool_min, "request_json", side_effect=http),
                patch.object(
                    run_mempool_min,
                    "wait_http",
                    side_effect=lambda url, timeout, check: check(),
                ),
                ThreadPoolExecutor(2) as workers,
            ):
                futures = [
                    workers.submit(instance.stage, target=True, fault_only=True)
                    for instance in instances
                ]
                for future in futures:
                    future.result(timeout=10)
            for root in roots:
                report = json.loads((root / "timeout/timeout.json").read_text())
                self.assertEqual(report["worker_exit"], 1)
                self.assertTrue(report["process_cleanup_verified"])
                self.assertFalse((root / "target").exists())
            self.assertGreater(main.status()["ledger"]["d_hbm"]["outstanding"], 0)

    def test_paired_capacity_run_rejects_before_admission_and_cleans_owned_processes(
        self,
    ):
        config = RouterConfig(
            "p",
            "d",
            "/model",
            prefix_pages=40,
            verify_prefix=True,
            match_prefix=True,
            startup_timeout=5,
            operation_timeout=3,
        )
        main = MainRouter(config, "cpu")
        left, right = socket.socketpair()
        with (
            tempfile.TemporaryDirectory() as folder,
            Peer(left, 5) as p,
            Peer(right, 5) as d,
        ):
            roots = [Path(folder) / role for role in ("prefill", "decode")]
            instances = [
                run_mempool_min.Runner(
                    SimpleNamespace(
                        role=role,
                        mode="full-acceptance",
                        config=Path(folder) / "config.json",
                        run_id="cpu",
                    ),
                    config,
                    peer,
                    root,
                )
                for role, peer, root in zip(("prefill", "decode"), (p, d), roots)
            ]
            instances[0].cases = [payload()]
            popen = run_mempool_min.subprocess.Popen
            children = []

            def launch(command, **kwargs):
                if (
                    command[0] == "bash"
                    or "sglang.srt.disaggregation.ascend.mempool_router" in command
                ):
                    code = "import time; time.sleep(30)"
                    if command[0] == "bash" and "decode" in command:
                        self.assertEqual(
                            kwargs["env"]["SGLANG_NPU_MEMPOOL_TEST_CAPACITY"], "1"
                        )
                        row = registration("decode")
                        for rank in row["ranks"]:
                            rank.update(
                                replicas=config.replicas(),
                                prefix_pages=0,
                                local_dram_bytes=2**30,
                                verify_prefix=True,
                                match_prefix=True,
                            )
                        row["ranks"][15]["hbm_pages"] = 64
                        with self.assertRaisesRegex(ValueError, "native capacities"):
                            main.receive(dict(row, kind="register"))
                        (Path(kwargs["env"]["LOG_DIR"]) / "decode.log").write_text(
                            "mempool acceptance_capacity data="
                            + json.dumps(
                                dict(
                                    run_id="cpu",
                                    rank=15,
                                    actual_hbm_pages=128,
                                    advertised_hbm_pages=64,
                                    required_hbm_pages=65,
                                )
                            )
                        )
                        code = "raise SystemExit(1)"
                    child = popen([sys.executable, "-c", code], **kwargs)
                    children.append(child)
                    return child
                return popen(command, **kwargs)

            def http(url, *, payload=None, timeout=10):
                if payload is not None:
                    try:
                        main.submit(payload)
                    except RequestRejected as exc:
                        raise urllib.error.HTTPError(
                            url, exc.status, str(exc), {}, None
                        )
                    self.fail("capacity request was accepted")
                return main.status()

            with (
                patch.object(run_mempool_min.subprocess, "Popen", side_effect=launch),
                patch.object(run_mempool_min, "request_json", side_effect=http),
                patch.object(
                    run_mempool_min,
                    "wait_http",
                    side_effect=lambda url, timeout, check: check(),
                ),
                ThreadPoolExecutor(2) as workers,
            ):
                futures = [
                    workers.submit(instance.capacity_stage) for instance in instances
                ]
                for future in futures:
                    future.result(timeout=10)
            for root in roots:
                self.assertEqual(
                    json.loads((root / "capacity/evidence.json").read_text())[
                        "worker_exit"
                    ],
                    1,
                )
                self.assertTrue(
                    json.loads((root / "capacity/process-cleanup.json").read_text())[
                        "paired"
                    ]
                )
            self.assertTrue(all(child.poll() is not None for child in children))
            self.assertEqual(main.status()["ledger"], {})

    def test_capacity_rejection_requires_a_real_failure_exit_and_the_injected_rank(
        self,
    ):
        before = dict(
            ready=False, fault=None, active=None, ledger={}, directory={}, completed=[]
        )
        after = dict(
            before, fault="SR native capacities do not cover the configured limits"
        )
        log = "mempool acceptance_capacity data=" + json.dumps(
            dict(
                run_id="cpu",
                rank=15,
                actual_hbm_pages=128,
                advertised_hbm_pages=64,
                required_hbm_pages=65,
            )
        )
        result = verify_mempool_full.check_capacity_rejection(
            before, after, log, run_id="cpu", http_status=503, worker_exit=1
        )
        self.assertEqual(result["status"], "passed")
        for code in (None, 0):
            with self.subTest(code=code), self.assertRaises(ValueError):
                verify_mempool_full.check_capacity_rejection(
                    before, after, log, run_id="cpu", http_status=503, worker_exit=code
                )
        with self.assertRaises(ValueError):
            verify_mempool_full.check_capacity_rejection(
                before, after, "", run_id="cpu", http_status=503, worker_exit=1
            )

    def test_long_output_requires_actual_decode_forwards(self):
        case = dict(kind="load-only", sampling_params=dict(max_new_tokens=128))
        with self.assertRaisesRegex(ValueError, "decode forwards"):
            verify_mempool_min.check_output_steps(case, dict(output_ids=[1] * 128), 3)
        verify_mempool_min.check_output_steps(case, dict(output_ids=[1] * 128), 127)

    def test_missing_evidence_is_not_run_and_a_passed_flag_cannot_replace_rank_receipts(
        self,
    ):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            report = verify_mempool_full.summarize(root)
            self.assertEqual(report["status"], "failed")
            self.assertEqual(set(report["checks"]), {f"V{i}" for i in range(1, 11)})
            self.assertTrue(
                all(v["status"] == "not-run" for v in report["checks"].values())
            )
            (root / "target").mkdir()
            (root / "target/evidence.json").write_text('{"status":"passed"}')
            report = verify_mempool_full.summarize(root)
            self.assertEqual(report["checks"]["V2"]["status"], "failed")
            self.assertEqual(report["checks"]["V3"]["status"], "not-run")
            (root / "reference").mkdir()
            (root / "reference/answers.json").write_text(
                json.dumps(
                    [
                        dict(
                            status="failed",
                            category="structure",
                            error="truncated output",
                        )
                    ]
                )
            )
            report = verify_mempool_full.summarize(root)
            self.assertEqual(report["checks"]["V3"]["status"], "failed")
            self.assertEqual(
                report["checks"]["V3"]["failure_category"], "baseline-failed"
            )
            self.assertEqual(report["checks"]["V3"]["target_status"], "not-run")

    def test_full_mode_describes_independent_fault_runs_and_actual_policy(self):
        config = (
            Path(__file__).resolve().parents[2] / "configs/mempool-min.example.json"
        )
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            result = run_mempool_min.main(
                [
                    "--role",
                    "prefill",
                    "--mode",
                    "full-acceptance",
                    "--config",
                    str(config),
                    "--run-id",
                    "ticket04-cpu",
                    "--describe",
                ]
            )
        value = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertTrue(value["config"]["match_prefix"])
        self.assertEqual(value["config"]["prefix_pages"], 40)
        self.assertEqual(value["output_policy"], logical.POLICY)
        self.assertIn("isolated registration-capacity rejection", value["stages"])
        self.assertIn("isolated timeout/fail-stop and paired cleanup", value["stages"])


if __name__ == "__main__":
    unittest.main()
