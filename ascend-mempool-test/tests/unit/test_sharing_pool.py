"""Four-participant address and membership behavior at the BM SDK boundary."""

import unittest
from pathlib import Path
from unittest.mock import patch

from test_pool import FakeBM, FakeHandle

from ascend_mempool.sharing import SharingPlan
from ascend_mempool.sharing_pool import SharingPool, check_numa_placement, numa_evidence


class TestNumaEvidence(unittest.TestCase):
    def test_run1_driver_mapping_can_be_measured_but_remains_unverified(self):
        # Exact /proc records from run-1 on npu1-31, not anonymous CPU memory.
        mapping = "280040000000-2800c0000000 rw-s 00000000 00:3c 105                        /dev/davinci_manager"
        numa_map = "280040000000 default file=/dev/davinci_manager"
        with patch.object(Path, "read_text", side_effect=[mapping, numa_map]):
            evidence = numa_evidence(0x280040000000, 0)
        self.assertFalse(evidence["placement_verified"])
        self.assertEqual(evidence["status"], "unobservable_driver_mapping")
        self.assertFalse(check_numa_placement(evidence))

    def test_observed_wrong_or_mixed_nodes_still_fail(self):
        mapping = (
            "280040000000-2800c0000000 rw-s 00000000 00:3c 105 /dev/davinci_manager"
        )
        for counts in ("N1=1024", "N0=512 N1=512"):
            with self.subTest(counts=counts):
                with patch.object(
                    Path,
                    "read_text",
                    side_effect=[mapping, "280040000000 default " + counts],
                ):
                    evidence = numa_evidence(0x280040000000, 0)
                self.assertEqual(evidence["status"], "mismatch")
                with self.assertRaisesRegex(RuntimeError, "NUMA placement mismatch"):
                    check_numa_placement(evidence)

    def test_observed_requested_node_passes(self):
        with patch.object(
            Path,
            "read_text",
            side_effect=[
                "280040000000-2800c0000000 rw-s 00000000 00:3c 105 /dev/davinci_manager",
                "280040000000 default N0=1024",
            ],
        ):
            evidence = numa_evidence(0x280040000000, 0)
        self.assertTrue(check_numa_placement(evidence))

    def test_missing_or_anonymous_mapping_is_not_a_driver_exception(self):
        for mapping in ("", "280040000000-2800c0000000 rw-p 00000000 00:00 0"):
            with self.subTest(mapping=mapping):
                with patch.object(Path, "read_text", side_effect=[mapping, ""]):
                    evidence = numa_evidence(0x280040000000, 0)
                with self.assertRaises(RuntimeError):
                    check_numa_placement(evidence)

    def test_driver_exception_requires_matching_numa_record(self):
        mapping = (
            "280040000000-2800c0000000 rw-s 00000000 00:3c 105 /dev/davinci_manager"
        )
        for row in (
            "",
            "2800c0000000 default file=/dev/davinci_manager",
            "280040000000 default file=/dev/other",
        ):
            with self.subTest(row=row):
                with patch.object(Path, "read_text", side_effect=[mapping, row]):
                    evidence = numa_evidence(0x280040000000, 0)
                with self.assertRaises(RuntimeError):
                    check_numa_placement(evidence)


class GroupHandle(FakeHandle):
    def set_group_event_handler(self, callback):
        self.callback = callback
        return 0

    def gva_to_va(self, gva, mem_type):
        return 0x5000000000 + gva - 0x1000000000


class GroupSDK(FakeBM):
    def create2(self, **options):
        self.handle = GroupHandle(self.rank, options)
        return self.handle


class TestSharingPool(unittest.TestCase):
    def pool(self, side, member, tp_size=2):
        sdk = GroupSDK(side * tp_size + member)
        with patch.object(Path, "read_text", return_value="0-3"):
            pool = SharingPool(
                SharingPlan(tp_size=tp_size), "shared", side, member, sdk, numa_node=2
            )
        self.addCleanup(pool.close, drain=lambda: None)
        pool.join(timeout=0.01)
        return pool, sdk

    def test_both_members_resolve_p_and_d_to_their_physical_owner(self):
        contributions = []
        for side in (0, 1):
            for member in (0, 1):
                pool, sdk = self.pool(side, member)
                contributions.append(sdk.handle.local_mem_size("host"))
                self.assertEqual(sdk.handle.options["flags"], 0x82)
                self.assertEqual(pool.view(0, 0).gva_base, 0x1000000000)
                self.assertEqual(pool.view(1, 0).gva_base, 0x1080000000)
                self.assertEqual(pool.view(1, 0).device_base, 0x5080000000)
                if member == 1:
                    pool.rejoin_follower(timeout=0.01)
        self.assertEqual(contributions, [1 << 30, 0, 1 << 30, 0])

    def test_tp4_eight_members_map_two_owners_and_six_followers_rejoin(self):
        contributions = []
        for side in (0, 1):
            for member in range(4):
                pool, sdk = self.pool(side, member, tp_size=4)
                contributions.append(pool.contribution)
                self.assertEqual(pool.world_size, 8)
                self.assertEqual(pool.layout.probe_bytes, 512)
                self.assertEqual(pool.view(0, 0).gva_base, 0x1000000000)
                self.assertEqual(pool.view(1, 0).gva_base, 0x1100000000)
                if member:
                    pool.rejoin_follower(timeout=0.01)
                else:
                    with self.assertRaises(ValueError):
                        pool.rejoin_follower(timeout=0.01)
        self.assertEqual(contributions, [1 << 30, 0, 0, 0] * 2)

    def test_old_or_out_of_order_join_cannot_satisfy_follower_rejoin(self):
        pool, sdk = self.pool(0, 0)
        callback = sdk.handle.callback
        callback(1, "JOIN_EVENT")
        offset = len(pool.events)
        callback(1, "LEAVE_EVENT")
        with self.assertRaises(TimeoutError):
            pool.wait_membership(1, 0, since=offset)
        callback(1, "JOIN_EVENT")
        pool.wait_membership(1, 0, since=offset)
        offset = len(pool.events)
        callback(1, "JOIN_EVENT")
        callback(1, "LEAVE_EVENT")
        with self.assertRaises(TimeoutError):
            pool.wait_membership(1, 0, since=offset)


if __name__ == "__main__":
    unittest.main()
