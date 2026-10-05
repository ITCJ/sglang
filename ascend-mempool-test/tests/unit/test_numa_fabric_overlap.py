"""Check address reports against captured topology and sparse sysfs fixtures."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import probe_numa_fabric_overlap as probe

GIB = 1 << 30
# Definitions pasted from npu1-31's installed 26.1.1 driver on 2026-10-06.
HEADER = """
#define DEVMM_S2S_HOST_NODE_NUM 4
#ifdef EMU_ST
#define DEVMM_S2S_HOST_NODE_MEM_SIZE 0x40000000 /* 1G */
#else
#define DEVMM_S2S_HOST_NODE_MEM_SIZE 0xaa80000000 /* 682G */
#endif
static inline u64 devmm_get_host_node_local_addr(u32 node_id)
{
    u64 mem_node_start[DEVMM_S2S_HOST_NODE_NUM] = {
        0x29580000000, /* node 0 map addr */
        0xa9580000000,
        0x129580000000,
        0x1a9580000000
    };
    return mem_node_start[node_id];
}
"""


class TestNumaFabricOverlap(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.sysfs = self.root / "system"
        (self.sysfs / "memory").mkdir(parents=True)
        (self.sysfs / "node").mkdir()
        (self.sysfs / "memory/block_size_bytes").write_text("40000000\n")
        self.header = self.root / "devmm_common.h"
        self.header.write_text(HEADER)

    def add_block(self, node, index, state="online"):
        block = self.sysfs / "memory" / f"memory{index}"
        block.mkdir(exist_ok=True)
        (block / "state").write_text(state + "\n")
        (block / "phys_index").write_text(f"{index:08x}\n")
        node_dir = self.sysfs / "node" / f"node{node}"
        node_dir.mkdir(exist_ok=True)
        (node_dir / block.name).symlink_to(block, target_is_directory=True)

    def run_cli(self, *args):
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "probe_numa_fabric_overlap.py"),
                "--sysfs-root",
                str(self.sysfs),
                "--driver-header",
                str(self.header),
                *map(str, args),
            ],
            capture_output=True,
            text=True,
        )

    def test_host31_snapshot_reports_170_gib_on_each_even_node(self):
        ranges = {
            0: [(0x28000000000, 0x2C000000000)],
            1: [(0x40000000, 0x80000000), (0x24080000000, 0x28000000000)],
            2: [(0xA8000000000, 0xAC000000000)],
            3: [(0xA4000000000, 0xA8000000000)],
            4: [(0x128000000000, 0x12C000000000)],
            5: [(0x124000000000, 0x128000000000)],
            6: [(0x1A8000000000, 0x1AC000000000)],
            7: [(0x1A4000000000, 0x1A8000000000)],
        }
        for node, intervals in ranges.items():
            for start, end in intervals:
                for index in range(start // GIB, end // GIB):
                    self.add_block(node, index)
        output = self.root / "report.json"
        result = self.run_cli("--details", "--report", output)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(report["total_coverage_bytes"], 2047 * GIB)
        self.assertEqual(report["total_overlap_bytes"], 680 * GIB)
        self.assertEqual(
            [node["overlap_bytes"] for node in report["nodes"]],
            [170 * GIB, 0, 170 * GIB, 0, 170 * GIB, 0, 170 * GIB, 0],
        )
        self.assertEqual(report["nodes"][1]["ranges"], [list(r) for r in ranges[1]])
        self.assertIn("overlap: 680.00 GiB", result.stdout)
        self.assertIn("[0x29580000000, 0x2c000000000)", result.stdout)

    def test_holes_offline_blocks_and_overlapping_windows_do_not_inflate_capacity(self):
        # Two overlapping 5 GiB windows, separated online blocks, a CPU-only node.
        self.header.write_text(
            HEADER.replace("NODE_NUM 4", "NODE_NUM 2")
            .replace("0xaa80000000", "0x140000000ULL")
            .replace(
                "0x29580000000, /* node 0 map addr */\n        0xa9580000000,\n"
                "        0x129580000000,\n        0x1a9580000000",
                "0x200000000ULL, 0x240000000ULL",
            )
        )
        (self.sysfs / "node/node0").mkdir()
        for index in (8, 9, 12):
            self.add_block(3, index)
        self.add_block(3, 13, "offline")
        report = probe.inspect_overlap(self.sysfs, self.header)
        self.assertEqual(report["nodes"][0]["coverage_bytes"], 0)
        node = report["nodes"][1]
        self.assertEqual(node["ranges"], [(8 * GIB, 10 * GIB), (12 * GIB, 13 * GIB)])
        self.assertEqual(node["offline_blocks"], 1)
        self.assertEqual(node["overlap_bytes"], 3 * GIB)
        self.assertEqual(node["outside_bytes"], 0)

    def test_unrecognized_header_is_an_error_instead_of_a_hardcoded_fallback(self):
        for content in (
            HEADER.replace("0xaa80000000", "OTHER_PLATFORM_WINDOW_SIZE"),
            HEADER.replace("NODE_NUM 4", "NODE_NUM 8"),
            HEADER.replace("#ifdef EMU_ST", "#ifdef UNKNOWN_PLATFORM"),
        ):
            with self.subTest(content=content):
                self.header.write_text(content)
                result = self.run_cli()
                self.assertEqual(result.returncode, 2)
                self.assertIn("inspection failed:", result.stderr)
                self.assertNotIn("Overlap GiB", result.stdout)

    def test_block_shared_by_nodes_cannot_be_counted_twice(self):
        self.add_block(0, 2646)
        self.add_block(1, 2646)
        result = self.run_cli()
        self.assertEqual(result.returncode, 2)
        self.assertIn("belongs to node0 and node1", result.stderr)

    def test_hotplug_in_progress_and_missing_source_do_not_report_zero_overlap(self):
        self.add_block(0, 2646, "going-offline")
        result = self.run_cli()
        self.assertEqual(result.returncode, 2)
        self.assertIn("retry after memory hotplug", result.stderr)
        self.header.unlink()
        result = self.run_cli()
        self.assertEqual(result.returncode, 2)
        self.assertIn("devmm_common.h", result.stderr)


if __name__ == "__main__":
    unittest.main()
