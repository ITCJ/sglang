"""Observe full native reservations and cleanup through the allocator seam."""

import unittest
from array import array
from types import SimpleNamespace

import torch

from ascend_mempool_pd.mempool_native_reservation import NativeReservations


class Rows:
    def __init__(self):
        self.free_slots = [1]
        self.req_to_token = torch.zeros((2, 8320), dtype=torch.int32)

    def alloc_rows(self, n):
        if n > len(self.free_slots):
            return None
        rows, self.free_slots = self.free_slots[:n], self.free_slots[n:]
        return rows

    def free_rows(self, rows):
        self.free_slots.extend(rows)

    def free(self, req):
        self.free_rows([req.kv.req_pool_idx])
        req.kv.req_pool_idx = None

    def write(self, indices, values):
        self.req_to_token[indices] = values


class Pages:
    page_size = 128

    def __init__(self):
        self.pages = list(range(1, 68, 2))

    def available_size(self):
        return len(self.pages) * 128

    def alloc(self, tokens):
        n = tokens // 128
        if n > len(self.pages):
            return None
        pages, self.pages = self.pages[:n], self.pages[n:]
        return (torch.tensor(pages)[:, None] * 128 + torch.arange(128)).flatten()

    def free(self, indices):
        pages = (indices // 128).unique().tolist()
        if set(pages) & set(self.pages):
            raise AssertionError("double page free")
        self.pages += pages


def request(room=1):
    kv = SimpleNamespace(req_pool_idx=None, kv_committed_len=0, kv_allocated_len=0)
    kv.mark_kv_released = lambda: None
    return SimpleNamespace(
        bootstrap_room=room,
        kv=kv,
        origin_input_ids=array("q", [7] * 4096),
        output_ids=array("q"),
        set_extend_range=lambda start, end: None,
    )


class TestNativeReservations(unittest.TestCase):
    def test_first_token_only_returns_unused_reserved_page_and_row(self):
        rows, pages = Rows(), Pages()
        native = NativeReservations(rows, pages)
        before = pages.available_size()
        result = native.reserve(1, 128, 1)
        self.assertEqual(result["pages"], 2)
        req = request()
        req.origin_input_ids = [7] * 128
        native.attach(req, 128)
        self.assertEqual(req.kv.kv_allocated_len, 128)
        native.release(req)  # P's first token finishes; no D forward.
        self.assertEqual(pages.available_size(), before)
        self.assertEqual(rows.free_slots, [1])
        self.assertEqual(rows.req_to_token.count_nonzero().item(), 0)

    def test_page_exhaustion_does_not_leave_an_unpublished_row_reserved(self):
        rows, pages = Rows(), Pages()
        pages.pages.clear()
        native = NativeReservations(rows, pages)
        with self.assertRaisesRegex(RuntimeError, "page capacity"):
            native.reserve(1, 4096, 128)
        self.assertEqual(rows.free_slots, [1])
        self.assertFalse(native.reservations)

    def test_reserve_33_discontiguous_pages_then_early_eos_returns_all_once(self):
        rows, pages = Rows(), Pages()
        native = NativeReservations(rows, pages)
        native.reserve(1, 4096, 128)
        self.assertEqual(rows.free_slots, [])
        self.assertEqual(pages.available_size(), 128)
        req = request()
        prompt = native.attach(req, 4096)
        self.assertEqual(prompt[:2].tolist(), [128, 129])
        self.assertEqual(prompt[128:130].tolist(), [384, 385])
        self.assertEqual(req.kv.kv_allocated_len, 4096)
        self.assertTrue(native.can_decode([req]))
        batch = SimpleNamespace(reqs=[req], seq_lens_cpu=torch.tensor([4096]))
        self.assertEqual(native.decode(batch, 1).tolist(), [8320])
        self.assertEqual(req.kv.kv_committed_len, 4097)
        native.release(req)
        native.release(req)
        self.assertEqual(len(set(pages.pages)), 34)
        self.assertEqual(rows.free_slots, [1])
        self.assertIsNone(req.kv.req_pool_idx)
