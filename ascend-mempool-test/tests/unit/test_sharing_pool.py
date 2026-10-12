"""Four-participant address and membership behavior at the BM SDK boundary."""

import unittest
from pathlib import Path
from unittest.mock import patch

from test_pool import FakeBM, FakeHandle

from ascend_mempool.sharing import SharingPlan
from ascend_mempool.sharing_pool import SharingPool


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
    def pool(self, side, member):
        sdk = GroupSDK(side * 2 + member)
        with patch.object(Path, "read_text", return_value="0-3"):
            pool = SharingPool(SharingPlan(), "shared", side, member, sdk, numa_node=2)
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
