"""Shared-pool experiments at the layout, workload and report interfaces."""

import unittest

from ascend_mempool.sharing import SharingPlan, group_sample, port_plan, writer_rows


class TestSharingPlan(unittest.TestCase):
    def test_nic_rank_offsets_do_not_collide_with_control_stores_or_hccl(self):
        for mode in ("independent", "shared"):
            for side in (0, 1):
                ports = [port_plan(19400, mode, side, member) for member in (0, 1)]
                actual = [p["nic_actual"] for p in ports]
                reserved = {19400, 19401, 19402, 19403, 19412}
                self.assertEqual(len(set(actual)), 2)
                self.assertFalse(set(actual) & reserved)
                self.assertTrue(all(19404 <= port < 19412 for port in actual))
        with self.assertRaises(ValueError):
            port_plan(65524, "shared", 1, 1)

    def test_tp4_ports_and_odd_row_partitions_cover_all_members(self):
        for mode in ("independent", "shared"):
            ports = [
                port_plan(19400, mode, side, m, 4) for side in (0, 1) for m in range(4)
            ]
            self.assertEqual({p["collective"] for p in ports}, {19438})
            reserved = {19400, 19401, 19402, 19403, 19404, 19405, 19438}
            actual = [p["nic_actual"] for p in ports]
            self.assertEqual(len(set(actual)), 8)
            self.assertFalse(set(actual) & reserved)
        with self.assertRaises(ValueError):
            port_plan(65498, "shared", 1, 3, 4)
        for count in (1, 17, 2049):
            partitions = [
                writer_rows("shared", "logical", count, m, 4) for m in range(4)
            ]
            self.assertEqual(sorted(sum(partitions, [])), list(range(count)))
            self.assertEqual(sum(map(len, partitions)), count)
        self.assertEqual(
            writer_rows("shared", "equal_bytes", 2049, 3, 4), list(range(6147, 8196))
        )

    def test_independent_and_shared_contributions_do_not_count_reserved_va(self):
        plan = SharingPlan(prompt_tokens=8192, decode_tokens=4096)
        self.assertEqual(plan.participants("shared"), [0, 1, 2, 3])
        self.assertEqual(plan.contributions("shared"), [1 << 30, 0, 1 << 30, 0])
        self.assertEqual(plan.contributions("independent"), [1 << 30] * 4)
        self.assertEqual(plan.owner("shared", side=1, member=1), 2)
        self.assertEqual(plan.owner("independent", side=1, member=1), 1)

    def test_two_writers_cover_an_odd_tail_once_and_one_token_has_one_writer(self):
        self.assertEqual(writer_rows("shared", "logical", 5, 0), [0, 2, 4])
        self.assertEqual(writer_rows("shared", "logical", 5, 1), [1, 3])
        self.assertEqual(writer_rows("shared", "logical", 1, 1), [])
        self.assertEqual(writer_rows("independent", "logical", 5, 1), [0, 1, 2, 3, 4])
        self.assertEqual(writer_rows("shared", "equal_bytes", 3, 0), [0, 1, 2])
        self.assertEqual(writer_rows("shared", "equal_bytes", 3, 1), [3, 4, 5])

    def test_group_bandwidth_uses_one_host_envelope_and_actual_payload(self):
        samples = [
            dict(
                member=0,
                start_ns=1000000,
                done_ns=5000000,
                device_ms=3.9,
                copy_bytes=1152,
                logical_bytes=3456,
                repeats=1,
            ),
            dict(
                member=1,
                start_ns=1100000,
                done_ns=6000000,
                device_ms=4.8,
                copy_bytes=2304,
                logical_bytes=3456,
                repeats=1,
            ),
        ]
        result = group_sample(samples)
        self.assertEqual(result["host_envelope_ms"], 5.0)
        self.assertEqual(result["launch_skew_ms"], 0.1)
        self.assertEqual(result["copy_bytes"], 3456)
        self.assertEqual(result["logical_bytes"], 3456)
        self.assertAlmostEqual(result["copy_payload_gbps"], 0.0006912)
        self.assertTrue(result["aligned_batches"])
        samples[1]["start_ns"] = 5100000
        samples[1]["done_ns"] = 10000000
        self.assertFalse(group_sample(samples)["aligned_batches"])


if __name__ == "__main__":
    unittest.main()
