"""Check that the offline gate cannot pass with missing rank/release evidence."""

import importlib.util
import json
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

    def add_readback(self):
        """Append three complete requests with independent rank/layer KV evidence."""
        for path in (self.p, self.d):
            base = path.read_text()
            path.write_text(
                base
                + "\n"
                + base.replace("room=42", "room=43")
                + "\n"
                + base.replace("room=42", "room=44")
            )
        reports = []
        for room in (42, 43, 44):
            for rank in range(16):
                steps = 0 if room == 42 else 31
                report = {
                    "status": "passed" if steps else "zero_decode",
                    "layers": 78,
                    "start_layer": 0,
                    "forwards": steps,
                    "replay_forwards": steps,
                    "layer_checks": 78 * steps,
                    "prompt_kv": 100 * steps,
                    "decode_kv": 40 * steps,
                    "prompt_boundary_kv": 78 * steps,
                    "decode_first_kv": 78 * steps,
                    "min_valid_per_layer": 10 if steps else None,
                    "min_topk": 2048 if steps else None,
                    "max_topk": 2048 if steps else 0,
                    "written_kv": steps,
                    "cancelled": False,
                    "row": 16 if steps else None,
                    "prompt_slot": 2 if steps else None,
                    "decode_slot": 3 if steps else None,
                }
                reports.append(
                    f"mempool readback_result role=decode rank={rank} room={room} attempt=test-{room} data={json.dumps(report)}"
                )
        self.d.write_text(self.d.read_text() + "\n" + "\n".join(reports))

    def test_readback_gate_requires_every_rank_and_all_layer_checks(self):
        """Lifecycle success alone cannot satisfy the requested numerical gate."""
        with self.assertRaisesRegex(RuntimeError, "readback"):
            gate.check_logs([self.p], [self.d], 1, require_readback=True)
        self.add_readback()
        text = self.d.read_text()
        result = gate.check_logs([self.p], [self.d], 3, require_readback=True)
        self.assertEqual(result["status"], "shadow_readback_passed")
        self.assertEqual(len(result["readback"]["reports"]), 48)
        self.d.write_text(
            text.replace(
                "readback_result role=decode rank=15 room=44",
                "ignored role=decode rank=15 room=44",
            )
        )
        with self.assertRaisesRegex(RuntimeError, "readback.*rank=15.*room=44"):
            gate.check_logs([self.p], [self.d], 3, require_readback=True)
        self.d.write_text(
            text.replace('"layer_checks": 2418', '"layer_checks": 2417', 1)
        )
        with self.assertRaisesRegex(RuntimeError, "layer"):
            gate.check_logs([self.p], [self.d], 3, require_readback=True)

    def test_readback_gate_rejects_padding_only_and_missing_decode_source(self):
        """A shaped buffer or warmup alone proves neither remote nor local KV."""
        self.add_readback()
        text = self.d.read_text()
        for field, value in (
            ("min_valid_per_layer", 10),
            ("decode_kv", 1240),
            ("replay_forwards", 31),
            ("decode_first_kv", 2418),
        ):
            with self.subTest(field=field):
                self.d.write_text(
                    text.replace(f'"{field}": {value}', f'"{field}": 0', 1)
                )
                with self.assertRaises(RuntimeError):
                    gate.check_logs([self.p], [self.d], 3, require_readback=True)
        self.d.write_text(
            text + "\nmempool KV readback mismatch layer=1 row=1 position=0\n"
        )
        with self.assertRaisesRegex(RuntimeError, "failure"):
            gate.check_logs([self.p], [self.d], 3, require_readback=True)

    def test_readback_gate_requires_actual_attachment_reuse(self):
        """Two HTTP completions do not prove that the next attempt reused storage."""
        self.add_readback()
        text = self.d.read_text()
        for field, value in (
            ("row", 15),
            ("prompt_slot", 5),
            ("decode_slot", 6),
            ("row", None),
            ("decode_slot", -1),
            ("prompt_slot", 16),
            ("attempt", "test-43"),
        ):
            with self.subTest(field=field, value=value):
                lines = []
                for line in text.splitlines():
                    if "readback_result role=decode rank=15 room=44" in line:
                        prefix, raw = line.split(" data=", 1)
                        report = json.loads(raw)
                        if field == "attempt":
                            prefix = prefix.replace(
                                "attempt=test-44", "attempt=test-43"
                            )
                        else:
                            report[field] = value
                        line = prefix + " data=" + json.dumps(report)
                    lines.append(line)
                self.d.write_text("\n".join(lines))
                with self.assertRaisesRegex(
                    RuntimeError, "rank=15.*(reuse|attachment)"
                ):
                    gate.check_logs([self.p], [self.d], 3, require_readback=True)
        self.d.write_text(text)
        result = gate.check_logs([self.p], [self.d], 3, require_readback=True)
        evidence = result["readback"]["reuse"]
        self.assertEqual(len(evidence), 16)
        self.assertEqual(evidence[15]["rooms"], [43, 44])
        self.assertEqual(evidence[15]["row"], 16)
        self.assertEqual(evidence[15]["prompt_slot"], 2)
        self.assertEqual(evidence[15]["decode_slot"], 3)
