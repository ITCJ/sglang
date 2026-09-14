import argparse
import io
import signal
import socket
import subprocess
import unittest
from unittest.mock import MagicMock, patch

import check


class DirectCheckTests(unittest.TestCase):
    def test_max_scatter_targets_cover_each_page_once(self):
        from kv_transfer_bench import make_batches
        count = 1024
        k_width = check.PAGE_SIZE * check.K_DIM * 2
        rope_width = check.PAGE_SIZE * check.ROPE_DIM * 2
        k_total = check.LAYERS * (count + 1) * k_width
        rope_total = check.LAYERS * (count + 1) * rope_width
        for layout in ("contiguous", "scattered"):
            slots = []
            for batch in make_batches(count, 8, layout):
                self.assertLessEqual(len(batch["pages"]), 8)
                for page, slot in zip(batch["pages"], batch["slots"]):
                    slots.append(slot)
                    src, dst, sizes = check.transfer_plan(page * check.PAGE_BYTES, 0, k_total, count, slot)
                    self.assertEqual(sum(sizes), check.PAGE_BYTES)
                    for i, (start, target, size) in enumerate(zip(src, dst, sizes)):
                        self.assertGreaterEqual(start, page * check.PAGE_BYTES)
                        self.assertLessEqual(start + size, (page + 1) * check.PAGE_BYTES)
                        layer = i // 2
                        expected = ((layer * (count + 1) + slot) * k_width if i % 2 == 0
                                    else k_total + (layer * (count + 1) + slot) * rope_width)
                        self.assertEqual(target, expected)
                        self.assertLessEqual(target + size, k_total if i % 2 == 0 else k_total + rope_total)
            self.assertEqual(sorted(slots), list(range(1, count + 1)))

    def test_page_scatter_preserves_reserved_slots(self):
        source = check.split_page_payload(0)
        k_size = check.LAYERS * check.PAGE_SIZE * check.K_DIM * 2
        rope_size = check.PAGE_BYTES - k_size
        k, rope = bytearray(k_size * 2), bytearray(rope_size * 2)
        sources, targets, sizes = check.transfer_plan(0, 0, len(k))
        self.assertEqual(len(sizes), 2 * check.LAYERS)
        self.assertEqual(sum(sizes), check.PAGE_BYTES)
        coverage = sorted(zip(sources, sizes))
        self.assertEqual(coverage[0][0], 0)
        for (start, length), (following, _) in zip(coverage, coverage[1:]):
            self.assertEqual(start + length, following)
        self.assertEqual(sum(coverage[-1]), check.PAGE_BYTES)
        for src, dst, size in zip(sources, targets, sizes):
            buf, offset = (k, dst) if dst < len(k) else (rope, dst - len(k))
            buf[offset:offset + size] = source[src:src + size]
        for buf, packed, width in ((k, source[:k_size], k_size // check.LAYERS),
                                   (rope, source[k_size:], rope_size // check.LAYERS)):
            for layer in range(check.LAYERS):
                start = layer * 2 * width
                self.assertEqual(buf[start:start + width], bytes(width))
                self.assertEqual(buf[start + width:start + 2 * width],
                                 packed[layer * width:(layer + 1) * width])

    def test_control_round_trip(self):
        left, right = socket.socketpair()
        with left, right, right.makefile("rb") as reader:
            check.send(left, "READY", page_bytes=check.PAGE_BYTES)
            self.assertEqual(check.receive(reader, "READY")["page_bytes"], check.PAGE_BYTES)

    def test_control_rejects_invalid_or_peer_failure(self):
        for raw in (b"", b"{}", b"x" * 8193, b'{"event":"ERROR","code":"F3"}\n'):
            with self.subTest(raw=raw[:30]), self.assertRaises(RuntimeError):
                check.receive(io.BytesIO(raw), "READY")

    def test_timeout_kills_only_owned_group(self):
        process = MagicMock(pid=12345)
        process.wait.side_effect = [subprocess.TimeoutExpired("worker", 1), 0]
        with patch.object(check.subprocess, "Popen", return_value=process), \
             patch.object(check.os, "killpg") as kill, \
             patch.object(check.Path, "write_text"), patch("builtins.print") as output:
            self.assertEqual(check.supervise(argparse.Namespace(role="client", timeout=1)), 1)
            kill.assert_called_once_with(12345, signal.SIGKILL)
            output.assert_called_once_with("FT", flush=True)


if __name__ == "__main__":
    unittest.main()
