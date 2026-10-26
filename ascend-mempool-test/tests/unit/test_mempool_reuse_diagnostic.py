"""Replay the saved ABC requests without weakening content or output checks."""

import contextlib
import copy
import io
import json
import socket
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import msgspec
import run_mempool_min as runner
import test_mempool_prefix_evidence as fixtures
from ticket10_processes import Peer

from ascend_mempool_pd.mempool_router_protocol import RouterConfig


class TestReuseDiagnostic(unittest.TestCase):
    def test_describe_keeps_verification_and_independent_controls(self):
        config = (
            Path(__file__).resolve().parents[2] / "configs/mempool-min.example.json"
        )
        for noncontiguous, delayed in (
            (False, False),
            (True, False),
            (False, True),
            (True, True),
        ):
            command = [
                "--role",
                "prefill",
                "--mode",
                "reuse-diagnostic",
                "--config",
                str(config),
                "--run-id",
                "ticket03-005-normal-abc",
                "--replay-run",
                "ticket03-004",
                "--describe",
            ]
            if noncontiguous:
                command.append("--force-noncontiguous-restore")
            if delayed:
                command.append("--delay-prefix-fill")
            with (
                self.subTest(noncontiguous=noncontiguous, delayed=delayed),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(runner.main(command), 0)
            report = json.loads(output.getvalue())
            effective = report["config"]
            self.assertTrue(effective["verify_prefix"])
            self.assertTrue(effective["match_prefix"])
            self.assertEqual(effective["prefix_pages"], 40)
            self.assertEqual(effective["force_noncontiguous_restore"], noncontiguous)
            self.assertEqual(effective["delay_prefix_fill"], delayed)
            self.assertTrue(report["diagnose_reuse"])
            self.assertEqual(report["replay_run"], "ticket03-004")
            self.assertEqual(
                report["purpose"], "diagnostic comparison; not full acceptance"
            )

    def source(self, root):
        config = msgspec.to_builtins(
            RouterConfig(
                "p",
                "d",
                "/model",
                prefix_pages=40,
                verify_prefix=True,
                match_prefix=True,
            )
        )
        config.pop("force_noncontiguous_restore", None)
        config.pop("delay_prefix_fill", None)
        config = json.loads(json.dumps(config))
        identity = dict(
            run_id=root.name,
            mode="reuse-acceptance",
            diagnose_reuse=True,
            config=config,
            model={"manifest": {"config.json": "same-model"}},
            versions={"torch": "same-version"},
            git_sha="old-code",
        )
        a = [123456] * 4096  # Real payloads exceed the bounded peer frame size.
        cases = [
            dict(
                name="reuse-prefix-" + name,
                input_ids=ids,
                rendered_input="saved template",
                sampling_params=dict(
                    temperature=0, top_k=1, top_p=1, max_new_tokens=128, ignore_eos=True
                ),
            )
            for name, ids in (("a", a), ("b", a + [8] * 1024), ("c", a.copy()))
        ]
        for role in ("prefill", "decode"):
            folder = root / role
            folder.mkdir(parents=True)
            (folder / "identity.json").write_text(json.dumps(identity))
        folder = root / "prefill"
        (folder / "cases.json").write_text(json.dumps(cases))
        for stage in ("reference", "target"):
            (folder / stage).mkdir()
            (folder / stage / "responses.json").write_text(
                json.dumps(dict(cases=cases))
            )
        return identity, cases

    def test_source_preserves_payloads_and_rejects_changed_saved_requests(self):
        import replay_mempool_reuse as replay

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "ticket03-004"
            identity, cases = self.source(root)
            loaded = replay.load_source(root, "prefill")
            self.assertEqual(loaded["cases"], cases)
            self.assertEqual(loaded["identity"], identity)
            self.assertIsNone(replay.load_source(root, "decode")["cases"])
            changed = copy.deepcopy(cases)
            changed[1]["input_ids"][-1] = 9
            (root / "prefill/cases.json").write_text(json.dumps(changed))
            with self.assertRaisesRegex(ValueError, "saved requests"):
                replay.load_source(root, "prefill")

    def test_replay_rejects_sampling_drift_and_identity_drift(self):
        import replay_mempool_reuse as replay

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "ticket03-004"
            identity, cases = self.source(root)
            current = copy.deepcopy(identity)
            current.update(
                run_id="ticket03-005-normal-abc",
                mode="reuse-diagnostic",
                git_sha="new-code",
            )
            current["config"].update(
                force_noncontiguous_restore=False, delay_prefix_fill=False
            )
            replay.validate_identity(identity, current)
            for key in ("model", "versions", "config"):
                bad = copy.deepcopy(current)
                if key == "config":
                    bad[key]["max_total_tokens"] += 128
                else:
                    bad[key] = {"changed": True}
                with self.subTest(key=key), self.assertRaises(ValueError):
                    replay.validate_identity(identity, bad)
            for key, value in (("ignore_eos", False), ("max_new_tokens", 127)):
                bad_cases = copy.deepcopy(cases)
                bad_cases[2]["sampling_params"][key] = value
                with self.subTest(key=key), self.assertRaises(ValueError):
                    replay.validate_cases(bad_cases)

    def test_source_is_transferred_in_bounded_frames_to_decode(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            identity, cases = self.source(base / "ticket03-004")
            current = copy.deepcopy(identity)
            current.update(run_id="ticket03-005-normal-abc", mode="reuse-diagnostic")
            current["config"].update(
                force_noncontiguous_restore=False, delay_prefix_fill=False
            )
            config = msgspec.convert(current["config"], RouterConfig)
            left, right = socket.socketpair()
            with (
                Peer(left, 5) as p,
                Peer(right, 5) as d,
                ThreadPoolExecutor(2) as workers,
            ):
                instances = []
                for role, peer in (("prefill", p), ("decode", d)):
                    root = base / current["run_id"] / role
                    root.mkdir(parents=True)
                    args = SimpleNamespace(
                        role=role,
                        mode="reuse-diagnostic",
                        log_root=base,
                        replay_run="ticket03-004",
                    )
                    instances.append(runner.Runner(args, config, peer, root))
                tasks = [
                    workers.submit(instance.replay_source, current)
                    for instance in instances
                ]
                for task in tasks:
                    task.result(timeout=10)
                for instance in instances:
                    self.assertEqual(instance.cases, cases)
                    self.assertEqual(
                        json.loads((instance.root / "cases.json").read_text()), cases
                    )
                self.assertEqual(
                    instances[0].report["replay_source"]["payload_sha256"],
                    instances[1].report["replay_source"]["payload_sha256"],
                )

    def test_paired_diagnostic_collects_all_abc_and_cleans_up_before_verdict(self):
        for failure in (None, "output", "probe"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as folder:
                base = Path(folder)
                snapshots, cases, lines = fixtures.TestPrefixEvidence().evidence(
                    reuse=True, noncontiguous=False, delayed=False
                )
                for case in cases:
                    case["sampling_params"] = dict(
                        temperature=0,
                        top_k=1,
                        top_p=1,
                        max_new_tokens=128,
                        ignore_eos=True,
                    )
                reference = [
                    dict(
                        output_ids=[1] * 128,
                        meta_info=dict(
                            prompt_tokens=len(c["input_ids"]), completion_tokens=128
                        ),
                    )
                    for c in cases
                ]
                actual = copy.deepcopy(reference)
                if failure == "output":
                    actual[0]["output_ids"][15] = (
                        2  # Continue B and C after an A mismatch.
                    )
                responses = iter(
                    value for pair in zip(actual, snapshots) for value in pair
                )
                requests, children = [], []
                config = RouterConfig(
                    "p",
                    "d",
                    "/model",
                    prefix_pages=40,
                    verify_prefix=True,
                    match_prefix=True,
                )
                left, right = socket.socketpair()
                popen = runner.subprocess.Popen

                def launch(command, **kwargs):
                    if (
                        command[0] == "bash"
                        or "sglang.srt.disaggregation.ascend.mempool_router" in command
                    ):
                        child = popen(
                            [sys.executable, "-c", "import time; time.sleep(90)"],
                            **kwargs,
                        )
                        children.append(child)
                        return child
                    return popen(command, **kwargs)

                def http(url, **kwargs):
                    if "payload" in kwargs:
                        requests.append(kwargs["payload"])
                    return next(responses)

                diagnosis = dict(
                    complete=failure != "probe",
                    ranks=[
                        dict(
                            identity_ok=True,
                            metadata_ok=True,
                            nonfinite=0,
                            operators=dict(checkpoints={}, replays={}),
                        )
                        for _ in range(16)
                    ],
                )
                with Peer(left, 10) as p, Peer(right, 10) as d:
                    instances = []
                    for role, peer in (("prefill", p), ("decode", d)):
                        root = base / role
                        (root / "target").mkdir(parents=True)
                        (root / "target" / (role + ".log")).write_text(
                            "\n".join(lines) if role == "prefill" else ""
                        )
                        args = SimpleNamespace(
                            role=role,
                            mode="reuse-diagnostic",
                            config=root / "config.json",
                            run_id="cpu",
                            diagnose_reuse=True,
                        )
                        instance = runner.Runner(args, config, peer, root)
                        instance.cases, instance.reference = cases, reference
                        instances.append(instance)
                    with (
                        patch.object(runner.subprocess, "Popen", side_effect=launch),
                        patch.object(
                            runner,
                            "wait_http",
                            side_effect=lambda url, timeout, watch: watch(),
                        ),
                        patch.object(runner, "request_json", side_effect=http),
                        patch.object(
                            runner.Runner,
                            "rejected_inputs",
                            return_value=dict(status="passed"),
                        ),
                        patch.object(
                            runner.Runner,
                            "timeout_case",
                            side_effect=AssertionError(
                                "no timeout injection in diagnostic"
                            ),
                        ),
                        patch.object(
                            runner.verify_mempool_min, "check_target", return_value={}
                        ),
                        patch.object(
                            runner.summarize_mempool_reuse,
                            "summarize",
                            return_value=diagnosis,
                        ),
                        ThreadPoolExecutor(2) as workers,
                        contextlib.redirect_stdout(io.StringIO()),
                    ):
                        tasks = [
                            workers.submit(instance.stage, target=True)
                            for instance in instances
                        ]
                        for task in tasks:
                            if failure:
                                with self.assertRaisesRegex(
                                    ValueError, "diagnostic comparison failed"
                                ):
                                    task.result(timeout=15)
                            else:
                                task.result(timeout=15)
                    self.assertEqual(len(requests), 3)
                    self.assertEqual(
                        requests,
                        [
                            {k: c[k] for k in ("input_ids", "sampling_params")}
                            for c in cases
                        ],
                    )
                    self.assertTrue(children)
                    self.assertTrue(all(child.poll() is not None for child in children))
                    for instance in instances:
                        report = json.loads(
                            (
                                instance.root / "target/diagnostic-comparison.json"
                            ).read_text()
                        )
                        self.assertEqual(
                            report["status"], "failed" if failure else "passed"
                        )
                        self.assertFalse(report["full_acceptance"])
                        self.assertEqual(
                            report["target_evidence"]["prefix"]["hit_lengths"],
                            [0, 4096, 3968],
                        )
                        self.assertEqual(len(report["output_comparison"]), 3)
                    self.assertTrue(
                        (instances[0].root / "target/evidence.json").exists()
                    )


if __name__ == "__main__":
    unittest.main()
