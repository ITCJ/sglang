"""Exercise the public ticket26 entry with external jobs replaced at Popen."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import run_ticket26


class TestTicket26Resume(unittest.TestCase):
    def test_phase2_resume_skips_phase1_and_passes_prior_component_reports(self):
        for role in ("prefill", "decode"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as directory:
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
                    "--sgl-source",
                    str(root),
                    "--prefill-host",
                    "p",
                    "--decode-host",
                    "d",
                    "--start-from",
                    "phase2",
                    "--phase2-component-run-id",
                    "old",
                ]

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
                offset = service.index("--component-reports")
                previous = f"/home/cryang/log/mempool-min/old-phase2/old-phase2-b64-sd512/{role}"
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
                self.assertEqual(len(benchmarks), 2)
                report = json.loads(
                    (root / "new" / f"ticket26-{role}-all.json").read_text()
                )
                self.assertEqual(
                    report["stages"], {"phase2": "passed", "benchmark": "passed"}
                )
                self.assertEqual(report["phase2_component_run_id"], "old")
