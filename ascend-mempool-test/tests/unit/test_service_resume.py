"""Resume a failed capacity stage without repeating completed device checks."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import run_mempool_min

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
