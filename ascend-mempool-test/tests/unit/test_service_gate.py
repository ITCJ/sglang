"""The formal server gate must reject incomplete or contradictory evidence."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import verify_service


def service_logs():
    logs = {"prefill": [], "decode": []}
    for role, lines in logs.items():
        for rank in range(16):
            prefix = f"mempool role={role} rank={rank}"
            lines.append(f"mempool mapping_ready role={role} rank={rank}")
            resources = dict(
                mode=f"pd_{role}_mempool",
                host_kv_bytes=0,
                staging_bytes=0,
                transport_staging=False,
                native_kv_bytes=100 if role == "prefill" else 0,
                sparse_cache_bytes=100 if role == "decode" else 0,
                index_k_bytes=100,
                registered_index_k_entries=2,
                registered_main_kv_entries=0,
            )
            lines.append(
                f"mempool resources role={role} rank={rank} data={json.dumps(resources)}"
            )
            if role == "decode":
                lines += [
                    f"mempool graph_captured device=npu:{rank} shape=16",
                    f"mempool graph_replay device=npu:{rank} real_requests=1",
                ]
            else:
                for kind in ("index_k", "aux"):
                    lines.append(
                        f"mempool native_copy role={role} rank={rank} "
                        f"kind={kind} bytes=100 main_kv_bytes=0"
                    )
            for room in range(1, 4):
                identity = (
                    f"room={room} attempt=attempt{room} p_slot=0 "
                    f"p_generation={room} d_slot=1 d_generation={room}"
                )
                if role == "prefill":
                    events = (
                        "BOUND_ACK",
                        "start_prefill",
                        "ready",
                    )
                else:
                    events = (
                        "acquire_decode",
                        "ACQUIRED",
                        "KV_READY",
                        "transfer",
                        "start_decode",
                    )
                for event in events:
                    lines.append(f"{prefix} {identity} event={event} free=15")
                if role == "decode":
                    steps = 0 if room == 1 else 3
                    report = dict(
                        status="completed" if steps else "zero_decode",
                        cancelled=False,
                        drained=True,
                        row=room if steps else None,
                        prompt_slot=0 if steps else None,
                        decode_slot=1 if steps else None,
                        forwards=steps,
                        replay_forwards=steps,
                        submitted_kv=steps,
                        written_kv=steps,
                        layers=2,
                        layer_checks=2 * steps,
                        selected_kv=8 * steps,
                        cache_hits=4 * steps,
                        prompt_misses=2 * steps,
                        decode_misses=2 * steps,
                    )
                    lines.append(
                        f"mempool fetch_result role={role} rank={rank} {identity} "
                        f"data={json.dumps(report)}"
                    )
                    if steps:
                        lines.append(
                            f"mempool row_detach role={role} rank={rank} {identity}"
                        )
                    lines.append(
                        f"mempool native_free role={role} rank={rank} {identity}"
                    )
                    for event in ("release", "RELEASE_ACK"):
                        lines.append(f"{prefix} {identity} event={event} free=16")
                else:
                    for effect in ("row_detach", "native_free"):
                        lines.append(
                            f"mempool {effect} role={role} rank={rank} {identity}"
                        )
                    lines.append(f"{prefix} {identity} event=native_release free=15")
                    lines.append(f"{prefix} {identity} event=DONE free=16")
    return {role: "\n".join(lines) for role, lines in logs.items()}


class TestServiceGate(unittest.TestCase):
    def check(self, logs):
        with tempfile.TemporaryDirectory() as directory:
            paths = {}
            for role, content in logs.items():
                paths[role] = Path(directory) / f"{role}.log"
                paths[role].write_text(content)
            return verify_service.check_logs(
                [paths["prefill"]], [paths["decode"]], requests=3, layers=2
            )

    def test_accepts_complete_formal_graph_and_reused_slots(self):
        report = self.check(service_logs())
        self.assertEqual(report["status"], "formal_service_passed")
        self.assertEqual(report["zero_decode_requests"], 1)
        self.assertEqual(report["decode_requests"], 2)
        self.assertEqual(report["accuracy"], "pending user curl check")
        self.assertEqual(report["performance"], "pending measured acceptance")

    def test_rejects_incomplete_or_contradictory_server_evidence(self):
        cases = (
            ("decode", '"host_kv_bytes": 0', '"host_kv_bytes": 10'),
            (
                "prefill",
                '"registered_main_kv_entries": 0',
                '"registered_main_kv_entries": 1',
            ),
            ("decode", "graph_captured device=npu:15", "ignored device=npu:15"),
            ("decode", '"replay_forwards": 3', '"replay_forwards": 0'),
            ("decode", '"written_kv": 3', '"written_kv": 2'),
            ("decode", '"drained": true', '"drained": false'),
            ("decode", '"cancelled": false', '"cancelled": true'),
            ("decode", "event=RELEASE_ACK", "event=OTHER"),
            ("decode", "free=16", "free=15"),
            ("decode", "attempt=attempt2", "attempt=wrong2"),
            ("decode", "d_generation=3", "d_generation=2"),
            ("prefill", "kind=index_k bytes=100", "kind=index_k bytes=0"),
            ("decode", '"selected_kv": 24', '"selected_kv": 23'),
            ("decode", '"prompt_slot": 0', '"prompt_slot": 5'),
            ("prefill", "mempool row_detach ", "mempool ignored "),
            ("prefill", "mempool native_free ", "mempool ignored "),
        )
        for role, before, after in cases:
            with self.subTest(before=before):
                logs = service_logs()
                logs[role] = logs[role].replace(before, after)
                with self.assertRaises(RuntimeError):
                    self.check(logs)

    def test_rejects_misordered_prefill_binding_and_release(self):
        for first, second in (
            ("event=BOUND_ACK ", "event=start_prefill "),
            ("mempool native_free ", "event=DONE "),
            ("mempool row_detach ", "mempool native_free "),
        ):
            with self.subTest(first=first, second=second):
                logs = service_logs()
                lines = logs["prefill"].splitlines()
                i = next(i for i, line in enumerate(lines) if first in line)
                j = next(i for i, line in enumerate(lines) if second in line)
                lines[i], lines[j] = lines[j], lines[i]
                logs["prefill"] = "\n".join(lines)
                with self.assertRaises(RuntimeError):
                    self.check(logs)

    def test_prefill_native_release_can_precede_ready_publication(self):
        logs = service_logs()
        lines = logs["prefill"].splitlines()
        ready_indices = [i for i, line in enumerate(lines) if "event=ready " in line]
        for index in ready_indices:
            # Keep detach -> free -> release; publish KV_READY after them.
            lines[index : index + 4] = (
                lines[index + 1 : index + 4] + lines[index : index + 1]
            )
        logs["prefill"] = "\n".join(lines)
        self.assertEqual(self.check(logs)["status"], "formal_service_passed")

    def test_native_transfer_can_finish_before_bm_ready(self):
        logs = service_logs()
        lines = logs["decode"].splitlines()
        ready_indices = [i for i, line in enumerate(lines) if "event=KV_READY " in line]
        for index in ready_indices:
            lines[index], lines[index + 1] = lines[index + 1], lines[index]
        logs["decode"] = "\n".join(lines)
        self.assertEqual(self.check(logs)["status"], "formal_service_passed")

    def test_each_rank_must_finish_and_supply_real_graph_and_layer_evidence(self):
        for before, after in (
            (
                "rank=15 room=3 attempt=attempt3 p_slot=0 p_generation=3 d_slot=1 d_generation=3 event=RELEASE_ACK",
                "ignored_ack",
            ),
            ("graph_replay device=npu:15", "ignored_replay device=npu:15"),
            (
                "mempool fetch_result role=decode rank=15 room=3",
                "ignored_result role=decode rank=15 room=3",
            ),
            ('"layer_checks": 6', '"layer_checks": 5'),
        ):
            with self.subTest(before=before):
                logs = service_logs()
                logs["decode"] = logs["decode"].replace(before, after, 1)
                with self.assertRaises(RuntimeError):
                    self.check(logs)

    def test_fault_or_partial_new_request_cannot_hide_behind_passes(self):
        for extra in (
            "mempool KV fetch invalid selection layer=5 row=1 position=5 invalid_kv=1",
            "mempool TP tick failed: unsafe release",
            "mempool role=decode rank=0 room=4 attempt=four event=acquire_decode free=15",
            "Traceback (most recent call last)",
        ):
            logs = service_logs()
            logs["decode"] += "\n" + extra
            with self.assertRaises(RuntimeError):
                self.check(logs)


if __name__ == "__main__":
    unittest.main()
