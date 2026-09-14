import unittest

from kv_layout import (
    K_DIM,
    LAYERS,
    PAGE_BYTES,
    PAGE_SIZE,
    ROPE_DIM,
    page_keys,
    page_payload,
    split_page_payload,
)


class LayoutTest(unittest.TestCase):
    def test_split_page_preserves_all_kv_and_rope_bytes(self):
        packed = page_payload(258)
        split = split_page_payload(258)
        self.assertEqual(len(split), PAGE_BYTES)
        tokens = LAYERS * PAGE_SIZE
        k_bytes, rope_bytes = K_DIM * 2, ROPE_DIM * 2
        rope_base = tokens * k_bytes
        restored = b"".join(
            split[i * k_bytes : (i + 1) * k_bytes]
            + split[rope_base + i * rope_bytes : rope_base + (i + 1) * rope_bytes]
            for i in range(tokens)
        )
        self.assertEqual(restored, packed)

    def test_page_layout(self):
        payload = page_payload(258)
        self.assertEqual(len(payload), PAGE_BYTES)
        token_bytes = (K_DIM + ROPE_DIM) * 2
        for layer, token in ((0, 0), (3, 17), (LAYERS - 1, PAGE_SIZE - 1)):
            start = (layer * PAGE_SIZE + token) * token_bytes
            code = (258 * 11 + layer * 7 + token * 3) % 256
            self.assertEqual(
                payload[start : start + K_DIM * 2], bytes((code, 0x3F)) * K_DIM
            )
            self.assertEqual(
                payload[start + K_DIM * 2 : start + token_bytes],
                bytes(((code + 91 + 53) % 256, 0x3F)) * ROPE_DIM,
            )

    def test_page_keys(self):
        self.assertEqual(
            page_keys("test", 3), ["test/page-0", "test/page-1", "test/page-2"]
        )
        self.assertNotEqual(page_payload(0)[:2], page_payload(1)[:2])
        self.assertNotEqual(page_payload(0)[:1152], page_payload(256)[:1152])


if __name__ == "__main__":
    unittest.main()
