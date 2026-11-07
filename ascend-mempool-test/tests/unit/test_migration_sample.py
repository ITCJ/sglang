"""Normal one-request timing and isolated source selection, without NPU use."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import diagnose_target_startup as diagnostic
import prepare_migration_sample as preparation
from run_mempool_min import RouterConfig, Runner


class TestMigrationSample(unittest.TestCase):
    def test_one_request_without_observe_or_trace_then_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            payload = preparation.sampling_input([dict(input_ids=list(range(4096)))])
            request = root / "request.json"
            request.write_text(json.dumps(payload))
            args = SimpleNamespace(
                role="prefill",
                mode="serve",
                run_id="sample",
                config=root / "config.json",
                sample_json=request,
                without_index_sync=True,
            )
            peer = Mock()
            peer.expect.side_effect = lambda kind, **kwargs: (
                {"payload": payload} if kind == "sample-input" else {}
            )
            runner = Runner(
                args,
                RouterConfig("p", "d", "/model", observe=False, observe_trace=False),
                peer,
                root,
            )
            jobs = Mock()
            jobs.__enter__ = Mock(return_value=jobs)
            jobs.__exit__ = Mock(return_value=False)
            calls = []

            def http(url, **kwargs):
                calls.append(url)
                jobs.__exit__.assert_not_called()
                if url.endswith("/generate"):
                    self.assertEqual(kwargs["payload"], payload)
                    return {
                        "output_ids": list(range(32)),
                        "performance": {"router_ttft_ms": 100, "tpot_ms": 20},
                    }
                return {"observation_flushed": ["decode", "prefill"]}

            with (
                patch.object(diagnostic, "Processes", return_value=jobs),
                patch.object(diagnostic, "wait_http"),
                patch.object(diagnostic, "service_ports", return_value=set()),
                patch.object(diagnostic, "request_json", side_effect=http),
            ):
                diagnostic.startup(runner)
            self.assertEqual([u.rsplit("/", 1)[-1] for u in calls], ["generate"])
            jobs.__exit__.assert_called_once()
            self.assertEqual(runner.report["status"], "performance_measured")
            self.assertFalse(runner.report["sample"]["trace"])
            self.assertFalse(runner.report["sample"]["observe"])
            self.assertTrue((root / "performance.json").exists())
            self.assertFalse((root / "observe").exists())
            self.assertIn(
                "measure_mempool_router.py", jobs.start.call_args_list[0].args[1][2]
            )

    def test_prepare_preserves_production_versions_and_same_request(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo = root / "repo"
            repo.mkdir()

            def git(*args):
                return (
                    subprocess.check_output(
                        ["git", *args], cwd=repo, stderr=subprocess.DEVNULL
                    )
                    .decode()
                    .strip()
                )

            git("init")
            git("config", "user.email", "test@example.invalid")
            git("config", "user.name", "Test")
            for name, value in {
                "python/source.py": "baseline",
                "ascend-mempool-test/scripts/tool.py": "old",
                "ascend-mempool-test/src/helper.py": "old",
            }.items():
                p = repo / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(value)
            git("add", ".")
            git("commit", "-m", "baseline")
            baseline = git("rev-parse", "HEAD")
            (repo / "python/source.py").write_text("phase2")
            (repo / "ascend-mempool-test/scripts/tool.py").write_text("current")
            git("add", ".")
            git("commit", "-m", "current")
            config = root / "config.json"
            config.write_text(
                json.dumps(dict(observe=True, observe_trace=True, verify_prefix=True))
            )
            cases = root / "cases.json"
            cases.write_text(json.dumps([{"input_ids": list(range(4096))}]))
            hashes = []
            with (
                patch.object(preparation, "REPO", repo),
                patch.object(preparation, "BASELINE", baseline),
            ):
                for variant in ("baseline", "phase2"):
                    dest = root / variant
                    result = preparation.prepare(variant, dest, config, cases)
                    hashes.append(result["request_sha256"])
                    self.assertEqual(
                        (dest / "source/python/source.py").read_text(), variant
                    )
                    self.assertEqual(
                        (
                            dest / "source/ascend-mempool-test/scripts/tool.py"
                        ).read_text(),
                        "current",
                    )
                    self.assertFalse(
                        json.loads((dest / "config.json").read_text())["observe_trace"]
                    )
            self.assertEqual(len(set(hashes)), 1)
