"""Dual-write admission at the existing Main Router service boundary."""

import unittest

import msgspec
import test_mempool_router as fixtures
from ascend_mempool_pd.mempool_router import MainRouter


class TestDualWriteService(unittest.TestCase):
    def test_cold_request_reserves_candidates_and_decode_prompt_without_p_alive(self):
        fixture = fixtures.TestPrefixMainRouter()
        fixture.setUp(shared=True)
        config = msgspec.structs.replace(fixture.main.config, dual_write=True)
        main = MainRouter(config, "cpu", hasher=fixture.main.hasher)
        for registration in fixture.main.registrations.values():
            for rank in registration["ranks"]:
                rank["decode_capacity"] = config.decode_storage_capacity
                rank["dual_write"] = True
            main.register(registration)
        request = main.submit(fixtures.payload())
        self.assertEqual(main.ledger["p_alive"].outstanding, 0)
        self.assertEqual(main.ledger["d_alive"].outstanding, 33)
        self.assertEqual(main.ledger["p_prefix"].outstanding, 32)
        self.assertEqual(request["candidate_sources"], list(range(32)))
        self.assertEqual(request["prefix_sources"], [])


class TestDualWriteLifecycle(unittest.TestCase):
    def test_abce_publish_hit_reference_and_capacity_fallback(self):
        fixture = fixtures.TestPrefixMainRouter()
        fixture.setUp(shared=True)
        config = msgspec.structs.replace(fixture.main.config, dual_write=True)
        main = MainRouter(config, "cpu", hasher=fixture.main.hasher)
        for registration in fixture.main.registrations.values():
            for rank in registration["ranks"]:
                rank.update(
                    decode_capacity=config.decode_storage_capacity, dual_write=True
                )
            main.register(registration)
        fixture.main = main
        free = list(range(40))
        for tokens, hit, retained in (
            ([7] * 4096, 0, 32),
            ([7] * 4096 + [8] * 1024, 4096, 40),
            ([7] * 4096, 3968, 40),
            ([9] * 4096, 0, 40),
        ):
            data = fixtures.payload(len(tokens))
            data["input_ids"] = tokens
            request = main.submit(data)
            self.assertEqual(request["hit_len"], hit)
            size = main.active["needed"]["d_alive"]
            fixture.ack(
                main.outbox.popleft(),
                lambda rank: dict(
                    slot=3,
                    page_version=1,
                    pages=[dict(page_size=128, physical_page=i) for i in range(size)],
                    native=dict(
                        row=1, pages=len(tokens) // 128 + 1, tokens=len(tokens) + 128
                    ),
                ),
            )
            targets = free[: len(request["candidate_sources"])]
            del free[: len(targets)]
            fixture.ack(
                main.outbox.popleft(),
                lambda rank: dict(
                    slot=5, page_version=1, pages=[], candidate_pages=targets
                ),
            )
            d_dispatch = main.outbox.popleft()
            fixture.ack(d_dispatch)
            p_dispatch = main.outbox.popleft()
            fixture.ack(p_dispatch)
            if hit:
                load = main.outbox.popleft()
                self.assertEqual(load["kind"], "load_back")
                fixture.ack(
                    load,
                    lambda rank: dict(
                        readable=True,
                        tokens=hit,
                        slot=5,
                        source_pages=[
                            p["logical_page"] for p in load["sources"][rank]["pages"]
                        ],
                        main_layers=78,
                        index_layers=21,
                        prefix_indices=list(range(128, 128 + hit)),
                    ),
                )
                self.assertFalse(main.outbox)  # No fill command.
                self.assertTrue(
                    all(
                        request["rid"] in main.directory[h]["prefix"]["lock_ops"]
                        for h in main.active["hashes"][: hit // 128]
                    )
                )
            fixture.finish_started(request, p_dispatch, d_dispatch)
            admit = main.outbox.popleft()
            self.assertEqual(admit["target_pages"], targets)
            main.receive(fixture.admit_ack(admit, targets))
            for _ in range(2):
                fixture.ack(
                    main.outbox.popleft(),
                    lambda rank: dict(freed=True, native_freed=True),
                )
            self.assertIsNone(main.active)
            self.assertEqual(main.ledger["p_prefix"].outstanding, retained)
            self.assertEqual(main.ledger["p_alive"].outstanding, 0)
            self.assertEqual(main.ledger["d_alive"].outstanding, 0)
            self.assertTrue(
                all(
                    not node["prefix"]["lock_ops"]
                    for node in main.directory.values()
                    if node["prefix"] is not None
                )
            )


class TestDualWriteShard(unittest.TestCase):
    def test_real_sr_allocation_installs_empty_p_and_shared_candidate_routes(self):
        import test_mempool_subrouter
        import test_prefix_shared
        from ascend_mempool.dual_write import DualWrite

        system = test_mempool_subrouter.TestSubrouter()
        groups = tuple((r, r + 1) for r in range(0, 16, 2))
        system.setUp(main_groups=groups, decode_groups=groups)
        helper = test_prefix_shared.TestSharedPrefix()
        self.addCleanup(helper.doCleanups)
        states, _ = helper.group(page_size=128, capacity=8192)
        for role in system.ticks:
            for tick in system.ticks[role]:
                tick.shard.dual_write = True
        for tick, (store, manager, _, _) in zip(system.ticks["prefill"], states):
            manager.enable_page_tables("cpu")
            manager.dual_write = DualWrite(manager, "cpu")
            tick.shard.manager = manager
            tick.shard.prefix = store
        system.request.update(candidate_sources=[0], prefix_sources=[])
        d = system.command("decode", "alloc", request=system.request)
        p = system.command(
            "prefill", "alloc", request=system.request, bindings=d["ranks"]
        )
        self.assertTrue(all(len(r["pages"]) == 2 for r in d["ranks"]))
        self.assertTrue(
            all(r["pages"] == [] and r["candidate_pages"] == [0] for r in p["ranks"])
        )
        bound = system.command(
            "decode", "dispatch", request=system.request, bindings=p["ranks"]
        )
        system.command(
            "prefill", "dispatch", request=system.request, bindings=bound["ranks"]
        )
        self.assertTrue(
            all(tick.shard.fill_complete for tick in system.ticks["prefill"])
        )


if __name__ == "__main__":
    unittest.main()
