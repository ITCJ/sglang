"""Archived version evidence: independently built SDKs need explicit provenance."""

import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from bandwidth_summary import validate_versions


class TestBandwidthVersions(unittest.TestCase):
    def environments(self):
        base = dict(
            kernel_binaries={"kernel.so": "same"},
            sdk_binaries={"sdk.so": "p"},
            torch="2",
            torch_npu="2",
            unidex_schema="schema",
            memfabric="1.1.4",
        )
        d = dict(base, sdk_binaries={"sdk.so": "d"})
        return {
            "rank-0/environment.json": base,
            "rank-1/environment.json": copy.deepcopy(base),
            "peer/rank-0/environment.json": d,
            "peer/rank-1/environment.json": copy.deepcopy(d),
        }

    def test_confirmed_source_allows_cross_host_builds_and_keeps_hashes(self):
        env = self.environments()
        evidence = validate_versions(env, "user confirmed same SDK source HEAD")
        self.assertEqual(evidence["status"], "source_confirmed_separate_builds")
        self.assertEqual(evidence["builds"]["local"], {"sdk.so": "p"})
        self.assertEqual(evidence["builds"]["peer"], {"sdk.so": "d"})

    def test_unconfirmed_or_within_host_differences_still_fail(self):
        env = self.environments()
        with self.assertRaisesRegex(ValueError, "sdk_binaries"):
            validate_versions(env, "")
        env["rank-1/environment.json"]["sdk_binaries"] = {"sdk.so": "third"}
        with self.assertRaisesRegex(ValueError, "within host"):
            validate_versions(env, "same source")

    def test_offline_rejects_incomplete_run_without_touching_original(self):
        from analyze_bandwidth import analyze
        import json

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "runner.json").write_text(
                json.dumps(dict(status="failed", cleanup="passed"))
            )
            original = b'{"status":"failed","original":true}'
            (root / "report.json").write_bytes(original)
            with self.assertRaisesRegex(ValueError, "measured"):
                analyze(root, root / "analysis", "same source", None)
            self.assertEqual((root / "report.json").read_bytes(), original)
