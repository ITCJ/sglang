"""Check that the offline gate cannot pass with missing rank/release evidence."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "verify_shadow_service",
    Path(__file__).resolve().parents[2] / "scripts/verify_shadow_service.py",
)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class TestShadowLogGate(unittest.TestCase):
    """Treat logs as the external reporting boundary, including missing evidence."""

    def setUp(self):
        """Create representative lifecycle evidence for all independent rank pairs."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.p = Path(directory.name) / "p.log"
        self.d = Path(directory.name) / "d.log"
        for role, path, events in (
            (
                "prefill",
                self.p,
                ("BOUND_ACK", "start_prefill", "ready", "native_release", "DONE"),
            ),
            (
                "decode",
                self.d,
                (
                    "acquire_decode",
                    "ACQUIRED",
                    "start_decode",
                    "release",
                    "RELEASE_ACK",
                ),
            ),
        ):
            lines = []
            for rank in range(16):
                lines.append(f"mempool mapping_ready role={role} rank={rank}")
                if role == "decode":
                    lines.extend(
                        (
                            f"mempool graph_captured device=npu:{rank}",
                            f"mempool graph_replay real_requests=1 device=npu:{rank}",
                        )
                    )
                for event in events:
                    lines.append(
                        f"mempool role={role} rank={rank} event={event} room=42 free=16"
                    )
            path.write_text("\n".join(lines))

    def test_requires_every_rank_and_actual_replay(self):
        """One missing worker ACK is failure even if all others returned storage."""
        result = gate.check_logs([self.p], [self.d], 1)
        self.assertEqual(result["status"], "shadow_lifecycle_passed")
        self.d.write_text(
            self.d.read_text().replace(
                "rank=15 event=RELEASE_ACK", "rank=15 event=NONE"
            )
        )
        with self.assertRaisesRegex(RuntimeError, "RELEASE_ACK"):
            gate.check_logs([self.p], [self.d], 1)

    def test_missing_graph_or_protocol_fault_is_failure(self):
        """An eager-only run or fatal worker cannot be called a passed graph gate."""
        text = self.d.read_text()
        self.d.write_text(
            text.replace(
                "graph_replay real_requests=1 device=npu:15", "not_replay device=npu:15"
            )
        )
        with self.assertRaisesRegex(RuntimeError, "Graph evidence"):
            gate.check_logs([self.p], [self.d], 1)
        self.d.write_text(text + "\nmempool TP tick failed: unsafe release\n")
        with self.assertRaisesRegex(RuntimeError, "failure"):
            gate.check_logs([self.p], [self.d], 1)
