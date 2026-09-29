"""Exercise mempool messages at the existing PD ZMQ multipart boundary."""

import unittest
from dataclasses import replace

from ascend_mempool_pd.mempool_control import MempoolFrameRouter, MempoolPDControl
from ascend_mempool_pd.mempool_protocol import (
    MempoolMessage,
    MessageType,
    PoolDescriptor,
    PoolPeer,
    RequestIdentity,
    SlotLease,
    decode_message,
    encode_message,
    is_mempool_message,
    validate_peer,
)


class TestMempoolProtocol(unittest.TestCase):
    """Keep peer compatibility and request identity stable across wire encoding."""

    def setUp(self):
        """Create one compatible P/D pair with asymmetric DRAM contributions."""
        layout = PoolDescriptor(
            layers=2,
            slots=16,
            prompt_tokens=8192,
            decode_tokens=16384,
            heads=1,
            dim=576,
            dtype="bfloat16",
            prompt_bytes=1073741824,
            decode_bytes=2147483648,
            stride_bytes=2147483648,
        )
        self.p = PoolPeer("p-boot", "prefill", 3, 16, 1, 7, layout)
        self.d = PoolPeer("d-boot", "decode", 3, 16, 1, 7, layout)
        self.request = RequestIdentity(27, "attempt-a", "p-boot", "d-boot")

    def test_handshake_and_acquire_roundtrip(self):
        """Carry exact typed identity and slots over a tagged multipart frame."""
        validate_peer(self.p, self.d)
        hello = MempoolMessage(
            MessageType.POOL_HELLO, peer=self.d, reply_to="tcp://d:4351"
        )
        acquire = MempoolMessage(
            MessageType.ACQUIRE,
            request=self.request,
            d_slot=SlotLease(5, 9),
            reply_to="tcp://d:4351",
            prompt_tokens=2048,
            decode_tokens=512,
        )
        for message in (hello, acquire):
            with self.subTest(message=message.kind):
                frames = encode_message(message)
                self.assertTrue(is_mempool_message(frames))
                self.assertEqual(decode_message(frames), message)
        self.assertFalse(is_mempool_message([b"27", b"existing PD message"]))

    def test_pool_ids_match_service_startup_namespace(self):
        """Accept the hardware gate IDs and exclude TransferEngine entities."""
        for pool_id in (0, 64, 101, 102, 255):
            validate_peer(
                replace(self.p, pool_id=pool_id), replace(self.d, pool_id=pool_id)
            )
        for pool_id in (-1, True, 256):
            with self.subTest(pool_id=pool_id), self.assertRaises(ValueError):
                replace(self.p, pool_id=pool_id)

    def test_rejects_mismatched_peer_and_malformed_wire(self):
        """Reject incompatible layouts and unknown or truncated control frames."""
        wrong = PoolPeer("d-boot", "decode", 4, 16, 1, 7, self.d.layout)
        with self.assertRaisesRegex(ValueError, "TP rank"):
            validate_peer(self.p, wrong)
        with self.assertRaises(ValueError):
            decode_message([b"ASCEND_MEMPOOL_V1", b'{"kind":"ACQUIRE"}'])
        with self.assertRaises(ValueError):
            decode_message([b"ASCEND_MEMPOOL_V1", b"not json"])

    def test_tagged_frames_enter_control_inbox_without_consuming_pd_frames(self):
        """The existing socket reader dispatches only its mempool-tagged traffic."""
        control = MempoolPDControl(self.p)
        ordinary = [b"27", b"existing PD message"]
        self.assertFalse(control.enqueue_frames(ordinary))
        self.assertEqual(control.drain_inbox(), [])

        hello = MempoolMessage(
            MessageType.POOL_HELLO, peer=self.d, reply_to="tcp://d:4351"
        )
        self.assertTrue(control.enqueue_frames(encode_message(hello)))
        self.assertEqual(control.drain_inbox(), [hello])

    def test_early_control_frame_survives_late_manager_attachment(self):
        """A receive thread may start before the mapped control is attached."""
        hello = MempoolMessage(
            MessageType.POOL_HELLO, peer=self.d, reply_to="tcp://d:4351"
        )
        ordinary = [b"27", b"existing PD message"]
        incoming = iter((encode_message(hello), ordinary))
        router = MempoolFrameRouter()
        receive = router.wrap_receive(lambda: next(incoming))

        self.assertIsNone(receive())
        self.assertEqual(receive(), ordinary)
        control = MempoolPDControl(self.p)
        router.attach(control)
        self.assertEqual(control.drain_inbox(), [hello])

    def test_malformed_control_frame_does_not_kill_ordinary_pd_reader(self):
        """A bad tagged frame faults mempool admission but preserves PD traffic."""
        ordinary = [b"27", b"existing PD message"]
        incoming = iter(([b"ASCEND_MEMPOOL_V1", b"not json"], ordinary))
        router = MempoolFrameRouter()
        receive = router.wrap_receive(lambda: next(incoming))

        self.assertIsNone(receive())
        self.assertEqual(receive(), ordinary)
        control = MempoolPDControl(self.d)
        router.attach(control)
        with self.assertRaisesRegex(RuntimeError, "invalid mempool JSON"):
            control.begin_handshake("tcp://d:4351")

    def test_bad_frame_after_attachment_faults_scheduler_only(self):
        """A live control receives the fault without raising on the socket reader."""
        router = MempoolFrameRouter()
        control = MempoolPDControl(self.p)
        router.attach(control)
        self.assertTrue(router.route([b"ASCEND_MEMPOOL_V1", b"not json"]))
        self.assertFalse(router.route([b"27", b"existing PD message"]))
        with self.assertRaisesRegex(RuntimeError, "invalid mempool JSON"):
            control.drain_inbox()

    def test_early_control_buffer_overflow_fails_closed(self):
        """An unattached receiver cannot accumulate unlimited control frames."""
        router = MempoolFrameRouter(max_pending=1)
        hello = MempoolMessage(
            MessageType.POOL_HELLO, peer=self.d, reply_to="tcp://d:4351"
        )
        self.assertTrue(router.route(encode_message(hello)))
        self.assertTrue(router.route(encode_message(hello)))
        control = MempoolPDControl(self.p)
        router.attach(control)
        with self.assertRaisesRegex(RuntimeError, "buffer.*full"):
            control.drain_inbox()

    def test_rejects_layout_underallocation_and_noninteger_protocol_version(self):
        """A peer cannot advertise less storage or a JSON bool as version one."""
        layout = self.p.layout
        with self.assertRaisesRegex(ValueError, "16 slots"):
            PoolDescriptor(
                layout.layers,
                8,
                layout.prompt_tokens,
                layout.decode_tokens,
                layout.heads,
                layout.dim,
                layout.dtype,
                layout.prompt_bytes,
                layout.decode_bytes,
                layout.stride_bytes,
            )
        with self.assertRaisesRegex(ValueError, "contribution"):
            PoolDescriptor(
                layout.layers,
                layout.slots,
                layout.prompt_tokens,
                layout.decode_tokens,
                layout.heads,
                layout.dim,
                layout.dtype,
                1024,
                layout.decode_bytes,
                layout.stride_bytes,
            )
        with self.assertRaisesRegex(ValueError, "version"):
            decode_message(
                [
                    b"ASCEND_MEMPOOL_V1",
                    b'{"version":true,"kind":"POOL_HELLO"}',
                ]
            )
        with self.assertRaisesRegex(ValueError, "slot proof"):
            SlotLease(5, 1, 0)


if __name__ == "__main__":
    unittest.main()
