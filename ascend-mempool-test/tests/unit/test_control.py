import socket
import unittest

from ascend_mempool.control import TestChannel


class TestControlFraming(unittest.TestCase):
    def test_consecutive_frames_preserve_all_metadata(self):
        sender_socket, receiver_socket = socket.socketpair()
        sender = TestChannel(sender_socket)
        receiver = TestChannel(receiver_socket)
        try:
            sender.send("HELLO", rank=1, contribution_bytes=1073741824)
            sender.send("DRAINED", success=True, checks=20)
            self.assertEqual(
                receiver.expect("HELLO"),
                {"rank": 1, "contribution_bytes": 1073741824},
            )
            self.assertEqual(
                receiver.expect("DRAINED"), {"success": True, "checks": 20}
            )
        finally:
            sender.close()
            receiver.close()


if __name__ == "__main__":
    unittest.main()
