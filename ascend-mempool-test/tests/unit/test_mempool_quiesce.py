"""Real Main/SR protocol over TCP: planned shutdown versus a lost worker."""

import asyncio
import contextlib
import unittest

import test_mempool_subrouter as sr_fixture
import zmq
from test_mempool_router import registration
from zmq.asyncio import Context

from ascend_mempool_pd.mempool_router import MainRouter
from ascend_mempool_pd.mempool_router_protocol import RouterConfig
from ascend_mempool_pd.mempool_router_transport import MainServer, SRConnection


class TestQuiesceTransport(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.context = Context()
        self.main = MainRouter(RouterConfig("p", "d", "/model"), "cpu")
        self.server = MainServer(self.main)
        self.server.socket = self.context.socket(zmq.ROUTER)
        self.server.socket.setsockopt(zmq.LINGER, 0)
        self.server.socket.setsockopt(zmq.ROUTER_MANDATORY, 1)
        port = self.server.socket.bind_to_random_port("tcp://127.0.0.1")
        self.system = sr_fixture.TestSubrouter()
        self.system.setUp()
        self.peers = {}
        for role in ("prefill", "decode"):
            peer = SRConnection(f"tcp://127.0.0.1:{port}", role, "cpu")
            self.peers[role] = peer
            for rank, tick in enumerate(self.system.ticks[role]):
                tick.receive = peer.receive
                tick.send = peer.send
                tick.registration = lambda r=role, i=rank: registration(r)["ranks"][i]
        self.pump = asyncio.create_task(self.server.pump())
        self.addAsyncCleanup(self.cleanup)
        await self.drive_until(lambda: self.main.ready)

    async def cleanup(self):
        self.pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.pump
        for peer in self.peers.values():
            peer.socket.close()
        self.server.socket.close()
        self.context.term()
        self.system.doCleanups()

    async def drive_until(self, condition):
        async with asyncio.timeout(5):
            while not condition():
                for role in self.peers:
                    await asyncio.to_thread(self.system.advance, role)
                await asyncio.sleep(0.01)

    async def test_confirmed_quiesce_allows_decode_exit_without_faulting_prefill(self):
        self.main.quiesce()
        await self.drive_until(lambda: self.main.quiesced)
        self.assertFalse(self.main.ready)
        self.peers["decode"].socket.close()
        # Cross a real heartbeat period after the D connection disappears.
        await asyncio.sleep(1.1)
        self.assertIsNone(self.main.fault)
        for tick in self.system.ticks["prefill"]:
            tick.clock = lambda: 3600
        await asyncio.to_thread(self.system.advance, "prefill")
        self.assertTrue(all(t.quiesced for t in self.system.ticks["prefill"]))
        self.assertTrue(all(t.fault is None for t in self.system.ticks["prefill"]))

    async def test_unannounced_decode_exit_still_fail_stops_prefill(self):
        self.peers["decode"].socket.close()
        async with asyncio.timeout(5):
            while self.main.fault is None:
                await asyncio.sleep(0.01)
        self.assertEqual(self.main.fault, "Host unreachable")
        async with asyncio.timeout(5):
            with self.assertRaisesRegex(RuntimeError, "SR fail-stop: Host unreachable"):
                while True:
                    await asyncio.to_thread(self.system.advance, "prefill")
                    await asyncio.sleep(0.01)


if __name__ == "__main__":
    unittest.main()
