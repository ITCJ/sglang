"""Resume a failed capacity stage without repeating completed device checks."""

import copy
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import run_mempool_min
import mempool_service_resume

from ascend_mempool_pd.mempool_router_protocol import RouterConfig


class TestServiceResume(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.previous = Path(self.folder.name) / "old" / "prefill"
        self.current = Path(self.folder.name) / "new" / "prefill"
        self.current.mkdir(parents=True)
        self.identity = dict(
            run_id="new",
            git_sha="phase1-base",
            config={"page_size": 64, "decode_capacity": 512},
            model={"config_sha256": "model"},
            versions={"torch": "version"},
            untracked_sha256={"python/kernel.cpp": "kernel"},
        )
        self.cases = [{"name": "logic-a", "input_ids": [1], "sampling_params": {}}]
        self.write("identity.json", dict(self.identity, run_id="old"))
        self.write("summary.json", {"stages": ["reference", "target"]})
        self.write("cases.json", self.cases)
        for stage in ("reference", "target"):
            self.write(f"{stage}/process-cleanup.json", {"status": "passed"})
            self.write(
                f"{stage}/evidence.json",
                {
                    "status": "formal_service_passed"
                    if stage == "reference"
                    else "passed"
                },
            )
        for stage in ("page-first-copy", "fetch"):
            self.write(f"{stage}/report.json", {"status": "passed"})
        self.write("capacity/evidence.json", {"status": "failed"})
        self.runtime_patch = "diff --git a/python/copy.py b/python/copy.py\n+copy()\n"
        (self.previous / "code.patch").write_text(self.runtime_patch)
        (self.current / "code.patch").write_text(
            self.runtime_patch
            + "diff --git a/ascend-mempool-test/scripts/run.py b/ascend-mempool-test/scripts/run.py\n+watch()\n"
        )

    def write(self, name, value):
        path = self.previous / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def test_run_full_resumes_with_capacity_and_keeps_original_evidence(self):
        runner = run_mempool_min.Runner(
            SimpleNamespace(
                role="prefill",
                mode="full-acceptance",
                run_id="new",
                resume_after_target=self.previous,
            ),
            RouterConfig("p", "d", "/model"),
            Mock(),
            self.current,
        )
        runner.report["identity"] = self.identity
        order = []
        with (
            patch.object(runner, "prepare_full_cases") as fixtures,
            patch.object(runner, "components") as components,
            patch.object(
                runner, "capacity_stage", side_effect=lambda: order.append("capacity")
            ),
            patch.object(runner, "stage", side_effect=lambda **kw: order.append(kw)),
            patch.object(
                run_mempool_min.verify_mempool_full,
                "summarize",
                return_value={"status": "passed"},
            ),
        ):
            runner.run_full()
        self.assertEqual(order, ["capacity", {"target": True, "fault_only": True}])
        fixtures.assert_not_called()
        components.assert_not_called()
        self.assertEqual(runner.cases, self.cases)
        self.assertFalse((self.current / "capacity").exists())
        self.assertEqual(
            (self.current / "target/evidence.json").read_bytes(),
            (self.previous / "target/evidence.json").read_bytes(),
        )
        receipt = json.loads((self.current / "service-reused-from.json").read_text())
        self.assertEqual(receipt["run_id"], "old")
        self.assertEqual(
            json.loads((self.previous / "capacity/evidence.json").read_text())[
                "status"
            ],
            "failed",
        )

    def test_resume_rejects_incomplete_evidence_or_changed_runtime(self):
        from mempool_service_resume import reuse_completed_stages

        for key in ("config", "model", "versions", "untracked_sha256"):
            with self.subTest(key=key):
                changed = copy.deepcopy(self.identity)
                changed[key][
                    "python/kernel.cpp" if key == "untracked_sha256" else "changed"
                ] = True
                with self.assertRaisesRegex(ValueError, "identity"):
                    reuse_completed_stages(
                        self.previous, self.current, changed, "prefill"
                    )
        self.write("summary.json", {"stages": ["reference"]})
        with self.assertRaisesRegex(ValueError, "completed"):
            reuse_completed_stages(
                self.previous, self.current, self.identity, "prefill"
            )
        self.write("summary.json", {"stages": ["reference", "target"]})
        self.write("target/evidence.json", {"status": "failed"})
        with self.assertRaisesRegex(ValueError, "passed"):
            reuse_completed_stages(
                self.previous, self.current, self.identity, "prefill"
            )
        self.write("target/evidence.json", {"status": "passed"})
        (self.current / "code.patch").write_text(self.runtime_patch + "+changed()\n")
        with self.assertRaisesRegex(ValueError, "identity"):
            reuse_completed_stages(
                self.previous, self.current, self.identity, "prefill"
            )

    def test_single_service_cli_forces_page_controls_and_rejects_two_rounds(self):
        import contextlib
        import io

        for option in (["--reference-only"], ["--reference-service-report", "old"]):
            argv = [
                "--role",
                "prefill",
                "--mode",
                "full-acceptance",
                "--config",
                "config.json",
                "--run-id",
                "new",
                *option,
            ]
            self.assertTrue(run_mempool_min.parse_args(argv).page_allocation)
            with (
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                run_mempool_min.parse_args([*argv, "--observation-rounds", "2"])

    def test_single_service_answer_gate_rejects_mismatch_and_unstable_reference(self):
        root = self.previous / "reference"
        for target, answers, stability, error in (
            (False, [{"status": "passed"}], "passed", None),
            (False, [{"status": "passed"}], "anomaly", "stability"),
            (True, [{"status": "passed", "ids": {"equal": True}}], "passed", None),
            (True, [{"status": "passed", "ids": {"equal": False}}], "passed", "IDs"),
            (
                True,
                [{"status": "target-failed", "ids": {"equal": True}}],
                "passed",
                "answers",
            ),
            (False, [], "passed", "answers"),
        ):
            self.write("reference/answers.json", answers)
            self.write("reference/stability.json", {"status": stability})
            if error:
                with self.assertRaisesRegex(ValueError, error):
                    mempool_service_resume.check_service_answers(
                        root, target=target, case_count=1
                    )
            else:
                mempool_service_resume.check_service_answers(
                    root, target=target, case_count=1
                )

    def test_reference_only_runs_once_without_components_or_target(self):
        for role in ("prefill", "decode"):
            args = run_mempool_min.parse_args(
                [
                    "--role",
                    role,
                    "--mode",
                    "full-acceptance",
                    "--config",
                    "config.json",
                    "--run-id",
                    "new",
                    "--reference-only",
                    "--page-allocation",
                ]
            )
            runner = run_mempool_min.Runner(
                args, RouterConfig("p", "d", "/model"), Mock(), self.current
            )
            with (
                patch.object(runner, "prepare_full_cases") as fixtures,
                patch.object(runner, "components") as components,
                patch.object(runner, "stage") as stage,
                patch.object(runner, "capacity_stage") as capacity,
            ):
                runner.run_full()
            fixtures.assert_called_once_with()
            stage.assert_called_once_with(target=False)
            components.assert_not_called()
            capacity.assert_not_called()
            self.assertEqual(runner.report["stopped_after"], "reference")
            self.assertEqual(runner.report["status"], "passed_execution_scope")
            self.assertTrue(runner.report["components"].startswith("not-run"))

    def test_reference_service_resume_without_component_files(self):
        self._target_resume(service_only=True)

    def test_reference_service_resume_rejects_unsuccessful_reference(self):
        self.write(
            "summary.json",
            {"status": "failed", "stages": ["reference"], "stopped_after": "reference"},
        )
        with self.assertRaisesRegex(ValueError, "successful reference-only"):
            mempool_service_resume.reuse_completed_stages(
                self.previous,
                self.current,
                self.identity,
                "prefill",
                through="reference",
                include_components=False,
            )

    def test_target_resume_restores_reference_without_repeating_it_on_either_side(self):
        self._target_resume()

    def _target_resume(self, service_only=False):
        self.write(
            "summary.json",
            {
                "stages": ["reference"],
                "status": "passed_execution_scope" if service_only else "failed",
                "stopped_after": "reference" if service_only else None,
            },
        )
        if service_only:
            for name in ("page-first-copy", "fetch"):
                shutil.rmtree(self.previous / name)
        self.write("target/evidence.json", {"status": "failed"})
        self.cases[0]["sampling_params"] = {"max_new_tokens": 1, "ignore_eos": True}
        self.write("cases.json", self.cases)
        results = [
            {
                "output_ids": [17],
                "meta_info": {"prompt_tokens": 1, "completion_tokens": 1},
            }
        ]
        self.write(
            "reference/responses.json", {"cases": self.cases, "results": results}
        )
        self.write("reference/stability.json", {"status": "passed"})
        for role in ("prefill", "decode"):
            with self.subTest(role=role):
                current = self.current.parent / ("retry-" + role) / role
                current.mkdir(parents=True)
                (current / "code.patch").write_text(self.runtime_patch)
                previous = self.previous
                if role == "decode":
                    previous = self.previous.with_name("decode")
                    shutil.copytree(self.previous, previous)
                    (previous / "reference/responses.json").unlink()
                runner = run_mempool_min.Runner(
                    SimpleNamespace(
                        role=role,
                        mode="full-acceptance",
                        run_id="new",
                        resume_after_reference=None if service_only else previous,
                        reference_service_report=previous if service_only else None,
                        stop_after_target=not service_only,
                    ),
                    RouterConfig("p", "d", "/model"),
                    Mock(),
                    current,
                )
                runner.report["identity"] = self.identity
                order = []
                tokenizer = Mock()

                def stage(**kwargs):
                    order.append(kwargs)
                    if role == "prefill":
                        self.assertEqual(runner.reference, results)
                        self.assertIs(runner.tokenizer, tokenizer)

                with (
                    patch.dict(
                        "sys.modules",
                        {
                            "transformers": SimpleNamespace(
                                AutoTokenizer=Mock(
                                    from_pretrained=Mock(return_value=tokenizer)
                                )
                            )
                        },
                    ),
                    patch.object(runner, "prepare_full_cases") as fixtures,
                    patch.object(runner, "components") as components,
                    patch.object(runner, "stage", side_effect=stage),
                    patch.object(
                        runner,
                        "capacity_stage",
                        side_effect=lambda: order.append("capacity"),
                    ),
                    patch.object(
                        run_mempool_min.verify_mempool_full,
                        "summarize",
                        return_value={"status": "passed"},
                    ),
                ):
                    runner.run_full()
                self.assertEqual(order, [{"target": True}])
                self.assertEqual(runner.report["status"], "passed_execution_scope")
                self.assertEqual(
                    runner.report["remaining_service_stages"], ["capacity", "timeout"]
                )
                fixtures.assert_not_called()
                components.assert_not_called()
                self.assertEqual(runner.cases, self.cases)
                receipt = json.loads((current / "service-reused-from.json").read_text())
                self.assertEqual(receipt["stages"], ["reference"])
                self.assertEqual(receipt["next_stage"], "target")
                self.assertFalse((current / "target").exists())
                if service_only:
                    self.assertEqual(receipt["components"], [])
                    self.assertFalse((current / "fetch").exists())
                    self.assertTrue(runner.report["components"].startswith("not-run"))

    def test_reference_resume_accepts_runner_only_commits_but_rejects_runtime_changes(
        self,
    ):
        repo = Path(self.folder.name) / "repo"
        repo.mkdir()

        def git(*args):
            return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()

        git("init", "-q")
        git("config", "user.name", "Resume Test")
        git("config", "user.email", "resume@example.invalid")
        (repo / "python").mkdir()
        (repo / "python/runtime.py").write_text("old runtime\n")
        git("add", ".")
        git("-c", "core.hooksPath=/dev/null", "commit", "-qm", "runtime")
        old = git("rev-parse", "HEAD")
        (repo / "runner.py").write_text("new runner\n")
        git("add", ".")
        git("-c", "core.hooksPath=/dev/null", "commit", "-qm", "runner only")
        new = git("rev-parse", "HEAD")
        prior = dict(self.identity, git_sha=old, run_id="old")
        self.write("identity.json", prior)
        self.write("summary.json", {"stages": ["reference"]})
        with patch.object(mempool_service_resume, "REPO", repo):
            (repo / "python/runtime.py").write_text("changed runtime\n")
            git("add", ".")
            git("-c", "core.hooksPath=/dev/null", "commit", "-qm", "runtime changed")
            with self.assertRaisesRegex(ValueError, "identity"):
                mempool_service_resume.reuse_completed_stages(
                    self.previous,
                    self.current,
                    dict(self.identity, git_sha=git("rev-parse", "HEAD")),
                    "prefill",
                    through="reference",
                )
            mempool_service_resume.reuse_completed_stages(
                self.previous,
                self.current,
                dict(self.identity, git_sha=new),
                "prefill",
                through="reference",
            )
