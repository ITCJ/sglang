"""Exercise the public ticket26 entry with external jobs replaced at Popen."""

import json
from itertools import product
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import run_ticket26
import run_k1_full_acceptance
import msgspec

from ascend_mempool_pd.mempool_router_protocol import RouterConfig


class TestTicket26Resume(unittest.TestCase):
    def test_matrix_forwards_target_only_and_reports_partial_success(self):
        for role in ("prefill", "decode"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = root / "config.json"
                config.write_bytes(
                    msgspec.json.encode(RouterConfig("p", "d", "/model"))
                )
                command = [
                    "run_k1_full_acceptance.py",
                    "--role",
                    role,
                    "--config",
                    str(config),
                    "--run-id",
                    "new",
                    "--sdk-build-id",
                    "sdk",
                    "--start-from",
                    "b64-sd512",
                    "--resume-after-reference",
                    str(root / "old" / role),
                    "--stop-after-target",
                ]

                def launch(args):
                    self.assertIn("--stop-after-target", args)
                    self.assertEqual(
                        args[args.index("--resume-after-reference") + 1],
                        str(root / "old" / role),
                    )
                    evidence = root / "new/new-b64-sd512" / role / "summary.json"
                    evidence.parent.mkdir(parents=True)
                    evidence.write_text(
                        json.dumps(
                            {
                                "status": "passed_execution_scope",
                                "stopped_after": "target",
                            }
                        )
                    )
                    return Mock(wait=Mock(return_value=0))

                with (
                    patch.object(run_k1_full_acceptance.sys, "argv", command),
                    patch.object(
                        run_k1_full_acceptance,
                        "Path",
                        side_effect=lambda value: (
                            root
                            if value == "/home/cryang/log/mempool-min"
                            else Path(value)
                        ),
                    ),
                    patch.object(run_k1_full_acceptance.signal, "signal"),
                    patch.object(
                        run_k1_full_acceptance.subprocess, "Popen", side_effect=launch
                    ) as popen,
                ):
                    self.assertEqual(run_k1_full_acceptance.main(), 0)
                popen.assert_called_once()
                report = json.loads((root / "new" / f"matrix-{role}.json").read_text())
                self.assertEqual(report["status"], "passed_execution_scope")
                self.assertFalse(report["user_accepted"])

    def test_phase2_resume_skips_phase1_and_passes_prior_component_reports(self):
        for role, reference_only in product(("prefill", "decode"), (False, True)):
            with (
                self.subTest(role=role, reference_only=reference_only),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                config = root / "config.json"
                config.write_text("{}")
                command = [
                    "run_ticket26.py",
                    "--role",
                    role,
                    "--config",
                    str(config),
                    "--run-id",
                    "new",
                    "--sdk-build-id",
                    "sdk",
                    "--prefill-host",
                    "p",
                    "--decode-host",
                    "d",
                    "--stage" if reference_only else "--start-from",
                    "phase2",
                    "--phase2-reference-run-id"
                    if reference_only
                    else "--phase2-component-run-id",
                    "old",
                ]

                if reference_only:
                    command.append("--stop-after-target")

                def local_path(value):
                    return root if value == "/home/cryang/log/k2" else Path(value)

                with (
                    patch.object(run_ticket26.sys, "argv", command),
                    patch.object(run_ticket26, "Path", side_effect=local_path),
                    patch.object(run_ticket26.signal, "signal"),
                    patch.object(run_ticket26.subprocess, "Popen") as popen,
                    patch.object(
                        run_ticket26,
                        "cache_eviction",
                        return_value={"status": "passed"},
                    ),
                ):
                    popen.return_value = Mock(wait=Mock(return_value=0))
                    self.assertEqual(run_ticket26.main(), 0)
                commands = [call.args[0] for call in popen.call_args_list]
                self.assertFalse(any(c[0] == "git" for c in commands))
                services = [
                    c
                    for c in commands
                    if "ascend-mempool-test/scripts/run_k1_full_acceptance.py" in c
                ]
                self.assertEqual(len(services), 1)
                service = services[0]
                self.assertEqual(service[service.index("--run-id") + 1], "new-phase2")
                previous = f"/home/cryang/log/mempool-min/old-phase2/old-phase2-b64-sd512/{role}"
                if reference_only:
                    offset = service.index("--resume-after-reference")
                    self.assertEqual(service[offset + 1], previous)
                    self.assertIn("--stop-after-target", service)
                    self.assertNotIn("--component-reports", service)
                else:
                    offset = service.index("--component-reports")
                    self.assertEqual(
                        service[offset + 1 : offset + 3],
                        [
                            previous + "/page-first-copy/report.json",
                            previous + "/fetch/report.json",
                        ],
                    )
                benchmarks = [
                    c
                    for c in commands
                    if "ascend-mempool-test/scripts/run_bandwidth.py" in c
                ]
                self.assertEqual(len(benchmarks), 0 if reference_only else 2)
                report = json.loads(
                    (
                        root
                        / "new"
                        / f"ticket26-{role}-{'phase2' if reference_only else 'all'}.json"
                    ).read_text()
                )
                self.assertEqual(
                    report["stages"],
                    {"phase2": "passed_execution_scope"}
                    if reference_only
                    else {"phase2": "passed", "benchmark": "passed"},
                )
                self.assertEqual(
                    report[
                        "phase2_reference_run_id"
                        if reference_only
                        else "phase2_component_run_id"
                    ],
                    "old",
                )
