"""Main's public admission/ack seam, without network or device claims."""

import hashlib
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ascend_mempool_pd.mempool_router import MainRouter, RequestRejected
from ascend_mempool_pd.mempool_router_protocol import RouterConfig, page_hashes


def registration(role):
    return {
        "role": role,
        "run_id": "cpu",
        "ranks": [
            dict(
                rank=rank,
                session=f"{role}-{rank}",
                peer_session=f"{'decode' if role == 'prefill' else 'prefill'}-{rank}",
                model="model-sha",
                vocab_size=100,
                layers=78,
                dim=576,
                indexer_layers=[0, 1, 2] + list(range(6, 75, 4)),
                index_dim=128,
                dtype="bfloat16",
                tp_size=16,
                dp_size=1,
                pp_size=1,
                slots=16,
                prompt_capacity=8192,
                decode_capacity=128,
                page_size=128,
                hbm_page_size=128,
                hbm_pages=128,
                request_rows=16,
                prompt_max=8192,
                numa_node=rank // 2,
                replicas={
                    "alive_main": 16,
                    "alive_index": 0,
                    "prefix_main": 0,
                    "prefix_index": 0,
                },
                graph=role == "decode",
                prefix_read_assignment="round_robin",
            )
            for rank in range(16)
        ],
    }


def payload(tokens=4096, new_tokens=128):
    return {
        "input_ids": [7] * tokens,
        "sampling_params": {"temperature": 0, "max_new_tokens": new_tokens},
    }


class TestMainRouter(unittest.TestCase):
    def setUp(self):
        self.main = MainRouter(
            RouterConfig("p", "d", "/model"),
            "cpu",
            hasher=lambda ids, page_size: [
                f"hash-{i}" for i in range(len(ids) // page_size)
            ],
        )
        self.main.register(registration("prefill"))
        self.main.register(registration("decode"))

    def test_reject_is_atomic_and_accept_reserves_whole_slots(self):
        before = deepcopy(self.main.status())
        for data in (
            payload(64),
            payload(65),
            payload(192),
            payload(8256),
            payload(new_tokens=129),
            {"text": "no"},
        ):
            with self.assertRaises(RequestRejected):
                self.main.submit(data)
            self.assertEqual(self.main.status(), before)
        request = self.main.submit(payload())
        status = self.main.status()
        self.assertEqual(status["ledger"]["p_alive"]["outstanding"], 64)
        self.assertEqual(status["ledger"]["d_alive"]["outstanding"], 1)
        self.assertEqual(status["ledger"]["d_hbm"]["outstanding"], 33)
        self.assertEqual(status["ledger"]["d_rows"]["outstanding"], 1)
        self.assertEqual(request["hit_len"], 0)
        self.assertEqual(request["page_size"], 128)
        self.assertEqual(len(status["directory"]), 32)
        with self.assertRaises(RequestRejected) as rejected:
            self.main.submit(payload())
        self.assertEqual(rejected.exception.status, 429)
        self.assertEqual(self.main.status(), status)

    def test_backend_support_is_explicit_and_registration_requires_equality(self):
        for size in (0, -1, 1, 64, 127, 129, 256, True, 128.0):
            with (
                self.subTest(size=size),
                self.assertRaisesRegex(ValueError, "page_size"),
            ):
                RouterConfig("p", "d", "/model", page_size=size)
        for field in ("page_size", "hbm_page_size"):
            for role in ("prefill", "decode"):
                main = MainRouter(self.main.config, "cpu")
                bad = registration(role)
                bad["ranks"][15][field] = 64
                with self.assertRaisesRegex(ValueError, "incompatible"):
                    main.register(bad)
                self.assertFalse(main.ready)
                self.assertFalse(main.ledger)

    def test_hash_boundary_is_passed_to_storage_and_partial_pages_are_rejected(self):
        # The Linux native hash extension cannot run on Mac. Check its public
        # storage seam, including namespace and B; hardware uses the real hash.
        storage = SimpleNamespace(
            get_hash_str=Mock(return_value=["h"] * 32),
            storage_namespace_seed=Mock(return_value="namespace"),
        )
        with patch.dict("sys.modules", {"sglang.srt.mem_cache.utils": storage}):
            tokens = [7] * 4096
            self.assertEqual(len(page_hashes(tokens, 128)), 32)
            storage.get_hash_str.assert_called_once_with(
                tokens, "namespace", page_size=128
            )
            storage.storage_namespace_seed.assert_called_once_with("", "")
            with self.assertRaisesRegex(ValueError, "complete token pages"):
                page_hashes([7] * 4032, 128)

    def test_5120_prompt_uses_40_hashes_and_41_native_pages(self):
        self.main.submit(payload(5120))
        self.assertEqual(len(self.main.directory), 40)
        self.assertEqual(self.main.ledger["d_hbm"].outstanding, 41)
        self.assertEqual(self.main.ledger["p_alive"].total, 1024)
        self.assertEqual(self.main.ledger["d_alive"].total, 16)

    def test_mismatched_allocation_page_size_retains_reservation(self):
        self.main.submit(payload())
        before = self.main.status()["ledger"]
        with self.assertRaisesRegex(ValueError, "allocation differs"):
            self.ack(
                self.main.outbox.popleft(),
                slot=0,
                pages=[dict(page_size=64)],
                native=dict(row=1, pages=33, tokens=4224),
            )
        self.assertEqual(self.main.status()["ledger"], before)

    def ack(self, command, **extra):
        result = dict(
            command,
            kind=command["kind"] + "_ack",
            ranks=[dict(rank=i, **extra) for i in range(16)],
        )
        self.main.receive(result)
        return result

    def test_partial_rank_ack_fail_stops_without_returning_reserved_quota(self):
        self.main.submit(payload())
        command = self.main.outbox.popleft()
        before = deepcopy(self.main.status()["ledger"])
        with self.assertRaisesRegex(ValueError, "missing ranks"):
            self.main.receive(dict(command, kind="alloc_ack", ranks=[dict(rank=0)]))
        self.assertIsNotNone(self.main.fault)
        self.assertEqual(self.main.status()["ledger"], before)
        self.assertFalse(self.main.outbox)
        with self.assertRaises(RequestRejected) as error:
            self.main.submit(payload())
        self.assertEqual(error.exception.status, 503)

    def test_operation_timeout_retains_all_resources(self):
        self.main.submit(payload())
        before = deepcopy(self.main.status()["ledger"])
        self.main.clock = lambda: self.main.started + 601
        self.main.check_timeouts()
        self.assertIsNotNone(self.main.fault)
        self.assertEqual(self.main.status()["ledger"], before)
        self.assertFalse(self.main.outbox)

    def test_capacity_reject_does_not_publish_or_change_ledger(self):
        self.main.ledger["d_hbm"].change(100)
        before = self.main.status()
        with self.assertRaises(RequestRejected) as error:
            self.main.submit(payload())
        self.assertEqual(error.exception.status, 429)
        self.assertEqual(self.main.status(), before)

    def test_incomplete_registration_never_becomes_ready(self):
        main = MainRouter(self.main.config, "cpu")
        partial = registration("prefill")
        partial["ranks"].pop()
        with self.assertRaises(ValueError):
            main.register(partial)
        self.assertFalse(main.ready)
        self.assertEqual(main.status()["ledger"], {})

    def test_native_finish_does_not_return_alive_until_both_free_acks(self):
        request = self.main.submit(payload())
        d_alloc = self.main.outbox.popleft()
        self.ack(
            d_alloc,
            message={"opaque": "d"},
            pages=[dict(page_size=128)],
            slot=3,
            native={"pages": 33, "tokens": 4224, "row": 1},
        )
        p_alloc = self.main.outbox.popleft()
        self.ack(
            p_alloc, message={"opaque": "p"}, pages=[dict(page_size=128)] * 64, slot=5
        )
        d_dispatch = self.main.outbox.popleft()
        self.ack(d_dispatch, message={"opaque": "bound"})
        p_dispatch = self.main.outbox.popleft()
        self.ack(p_dispatch)
        self.assertEqual(self.main.launches.popleft(), request)
        ready = dict(
            p_dispatch,
            kind="ready_tokens",
            ranks=[dict(rank=i, message={"opaque": "ready"}) for i in range(16)],
        )
        self.main.receive(ready)
        self.ack(self.main.outbox.popleft())
        self.main.receive(
            dict(
                d_dispatch,
                kind="finished",
                ranks=[
                    dict(rank=i, native_freed=True, drained=True) for i in range(16)
                ],
            )
        )
        status = self.main.status()
        self.assertEqual(status["ledger"]["d_hbm"]["outstanding"], 0)
        self.assertEqual(status["ledger"]["p_alive"]["outstanding"], 64)
        frees = [self.main.outbox.popleft(), self.main.outbox.popleft()]
        response = {"output_ids": [2, 3]}
        self.main.backend_result(request["rid"], response)
        first = self.ack(frees[0], native_freed=True, freed=True)
        self.main.receive(first)
        self.assertIsNotNone(self.main.status()["active"])
        self.ack(frees[1], native_freed=True, freed=True)
        self.assertIsNone(self.main.status()["active"])
        self.assertTrue(
            all(v["outstanding"] == 0 for v in self.main.status()["ledger"].values())
        )
        self.assertEqual(self.main.history[-1]["result"], response)
        self.assertEqual(self.main.status()["directory"], {})


class TestPrefixMainRouter(unittest.TestCase):
    def test_invalid_restore_and_early_ready_fail_stop_without_unlocking_sources(self):
        from msgspec.structs import replace

        for defect in (
            "missing_rank",
            "not_readable",
            "page_numbers",
            "wrong_slot",
            "early_ready",
        ):
            with self.subTest(defect=defect):
                self.setUp()
                self.finish_inference()
                save = self.main.outbox.popleft()
                self.main.receive(self.admit_ack(save, list(range(32))))
                self.free()
                self.main.config = replace(self.main.config, match_prefix=True)
                self.main.launches.clear()
                _, dispatch, _ = self.start_inference([7] * 4096)
                load = self.main.outbox.popleft()
                before = self.main.status()
                ack = dict(
                    load,
                    kind="load_back_ack",
                    ranks=[
                        dict(
                            rank=r,
                            readable=True,
                            slot=5,
                            tokens=3968,
                            source_pages=list(range(31)),
                            main_layers=78,
                            index_layers=21,
                            prefix_indices=list(range(128, 4096)),
                        )
                        for r in range(16)
                    ],
                )
                if defect == "missing_rank":
                    ack["ranks"].pop()
                elif defect == "not_readable":
                    ack["ranks"][-1]["readable"] = False
                elif defect == "page_numbers":
                    ack["ranks"][-1]["prefix_indices"] = list(range(1, 32))
                elif defect == "wrong_slot":
                    ack["ranks"][-1]["slot"] = 4
                else:
                    ack = dict(
                        dispatch,
                        kind="ready_tokens",
                        ranks=[dict(rank=r) for r in range(16)],
                    )
                with self.assertRaises(ValueError):
                    self.main.receive(ack)
                self.assertIsNotNone(self.main.fault)
                after = self.main.status()
                self.assertEqual(after["ledger"], before["ledger"])
                self.assertEqual(after["directory"], before["directory"])
                self.assertFalse(self.main.outbox)
                self.assertFalse(self.main.launches)

    def test_match_restores_then_fills_with_separate_locks_and_full_budget(self):
        self.finish_inference()
        save = self.main.outbox.popleft()
        self.main.receive(self.admit_ack(save, list(range(32))))
        self.free()
        from msgspec.structs import replace

        self.main.config = replace(self.main.config, match_prefix=True)
        self.main.launches.clear()
        for tokens, expected in (([7] * 4096 + [8] * 1024, 4096), ([7] * 4096, 3968)):
            req, p_dispatch, d_dispatch = self.start_inference(tokens)
            self.assertEqual(req["hit_len"], expected)
            self.assertEqual(self.main.ledger["p_alive"].outstanding, 64)
            self.assertFalse(self.main.launches)
            load = self.main.outbox.popleft()
            self.assertEqual(load["kind"], "load_back")
            hashes = list(self.main.directory)[: expected // 128]
            self.assertTrue(
                all(
                    load["op_id"] in self.main.directory[h]["prefix"]["lock_ops"]
                    for h in hashes
                )
            )
            receipt = lambda rank: dict(
                slot=5,
                readable=True,
                tokens=expected,
                source_pages=list(range(expected // 128)),
                main_layers=78,
                index_layers=21,
                prefix_indices=list(range(128, expected + 128)),
            )
            ack = self.ack(load, receipt)
            self.main.receive(ack)
            fill = self.main.outbox.popleft()
            self.assertEqual(fill["kind"], "fill")
            self.assertNotEqual(fill["op_id"], load["op_id"])
            self.assertTrue(
                all(
                    self.main.directory[h]["prefix"]["lock_ops"] == [fill["op_id"]]
                    for h in hashes
                )
            )
            self.assertEqual(self.main.launches.pop()["rid"], req["rid"])
            self.ack(
                fill,
                lambda rank: {
                    k: v for k, v in receipt(rank).items() if k != "prefix_indices"
                },
            )
            self.assertTrue(
                all(not self.main.directory[h]["prefix"]["lock_ops"] for h in hashes)
            )
            self.finish_started(req, p_dispatch, d_dispatch)
            if len(tokens) == 5120:
                save = self.main.outbox.popleft()
                self.assertEqual(save["source_pages"], list(range(32, 40)))
                self.main.receive(self.admit_ack(save, list(range(32, 40))))
            self.free()
            self.assertEqual(self.main.ledger["p_prefix"].outstanding, 40)

    def setUp(self):
        self.main = MainRouter(
            RouterConfig("p", "d", "/model", prefix_pages=40),
            "cpu",
            hasher=lambda ids, size: [
                hashlib.sha256(bytes(ids[:end])).hexdigest()
                for end in range(size, len(ids) + 1, size)
            ],
        )
        for role in ("prefill", "decode"):
            value = registration(role)
            for rank in value["ranks"]:
                rank.update(
                    prefix_pages=40 if role == "prefill" else 0,
                    local_dram_bytes=(13 if role == "prefill" else 1) * 2**30,
                    verify_prefix=False,
                )
                rank["replicas"] = dict(
                    alive_main=16, alive_index=16, prefix_main=16, prefix_index=16
                )
            self.main.register(value)

    def ack(self, command, rank_value=lambda rank: {}):
        message = dict(
            command,
            kind=command["kind"] + "_ack",
            ranks=[dict(rank=rank, **rank_value(rank)) for rank in range(16)],
        )
        self.main.receive(message)
        return message

    def start_inference(self, tokens=None):
        data = payload()
        if tokens is not None:
            data["input_ids"] = tokens
        request = self.main.submit(data)
        count = (len(data["input_ids"]) + 128) // 128
        self.ack(
            self.main.outbox.popleft(),
            lambda rank: dict(
                slot=3,
                pages=[dict(page_size=128)],
                native=dict(row=1, pages=count, tokens=count * 128),
            ),
        )
        self.ack(
            self.main.outbox.popleft(),
            lambda rank: dict(
                slot=5,
                pages=[
                    dict(
                        pool="alive_main",
                        region=f"prefill-{rank}",
                        replica=rank,
                        logical_page=5 * 64 + page,
                        page_size=128,
                    )
                    for page in range(64)
                ],
            ),
        )
        d_dispatch = self.main.outbox.popleft()
        self.ack(d_dispatch)
        p_dispatch = self.main.outbox.popleft()
        self.ack(p_dispatch)
        return request, p_dispatch, d_dispatch

    def finish_inference(self, tokens=None, *, native_first=True):
        request, p_dispatch, d_dispatch = self.start_inference(tokens)
        self.finish_started(request, p_dispatch, d_dispatch, native_first=native_first)
        return request

    def finish_started(self, request, p_dispatch, d_dispatch, *, native_first=True):
        self.main.receive(
            dict(
                p_dispatch, kind="ready_tokens", ranks=[dict(rank=i) for i in range(16)]
            )
        )
        self.ack(self.main.outbox.popleft())
        native = dict(
            p_dispatch,
            kind="native_freed",
            ranks=[dict(rank=i, native_freed=True) for i in range(16)],
        )
        done = dict(
            d_dispatch,
            kind="finished",
            ranks=[dict(rank=i, native_freed=True, drained=True) for i in range(16)],
        )
        self.main.receive(native if native_first else done)
        self.assertFalse(self.main.outbox)
        self.main.receive(done if native_first else native)
        self.main.backend_result(request["rid"], dict(output_ids=[1, 2]))
        return request

    def admit_ack(self, command, targets):
        return dict(
            command,
            kind="admit_ack",
            ranks=[
                dict(
                    rank=rank,
                    readable=True,
                    slot=5,
                    source_pages=command["source_pages"],
                    target_pages=targets,
                    main_layers=78,
                    index_layers=21,
                    pages=[
                        dict(
                            pool="prefix",
                            region=f"prefill-{rank}",
                            replica=rank,
                            logical_page=page,
                            page_size=128,
                        )
                        for page in targets
                    ],
                )
                for rank in range(16)
            ],
        )

    def free(self):
        frees = [self.main.outbox.popleft(), self.main.outbox.popleft()]
        for command in frees:
            self.ack(command, lambda rank: dict(freed=True, native_freed=True))
        self.assertIsNone(self.main.status()["active"])

    def test_cold_save_publishes_after_all_ranks_then_retains_prefix_after_free(self):
        self.finish_inference(native_first=False)
        command = self.main.outbox.popleft()
        self.assertEqual(command["kind"], "admit")
        self.assertEqual(command["source_pages"], list(range(32)))
        self.assertTrue(
            all(
                node["lock_ref"] == 1 and node["owner"] is None
                for node in self.main.directory.values()
            )
        )
        self.assertTrue(
            all(
                node["prefix"]["state"] == "writing"
                for node in self.main.directory.values()
            )
        )
        with self.assertRaises(RequestRejected):
            self.main.submit(payload())
        ack = self.admit_ack(command, list(range(32)))
        self.main.receive(ack)
        self.main.receive(ack)
        self.assertEqual(len(self.main.outbox), 2)
        self.free()
        self.assertEqual(self.main.ledger["p_prefix"].outstanding, 32)
        self.assertEqual(len(self.main.directory), 32)
        for node in self.main.directory.values():
            self.assertEqual(node["state"], "freed")
            self.assertEqual(node["locations"], [])
            self.assertEqual(node["lock_ref"], 0)
            self.assertEqual(node["prefix"]["state"], "ready")

    def test_cold_abce_preserves_directory_and_skips_existing_or_full_pages(self):
        for tokens, added, retained in (
            ([7] * 4096, 32, 32),
            ([7] * 4096 + [8] * 1024, 8, 40),
            ([7] * 4096, 0, 40),
            ([9] * 4096, 0, 40),
        ):
            prior = deepcopy(
                {key: value["prefix"] for key, value in self.main.directory.items()}
            )
            request = self.finish_inference(tokens)
            self.assertEqual(request["hit_len"], 0)
            if added:
                command = self.main.outbox.popleft()
                self.assertEqual(
                    command["source_pages"], list(range(retained - added, retained))
                )
                for key in prior:
                    self.assertEqual(
                        self.main.directory[key]["prefix"]["lock_ops"],
                        [command["op_id"]],
                    )
                self.main.receive(
                    self.admit_ack(command, list(range(retained - added, retained)))
                )
            else:
                self.assertTrue(
                    all(command["kind"] == "free" for command in self.main.outbox)
                )
            self.free()
            self.assertEqual(self.main.ledger["p_prefix"].outstanding, retained)
            self.assertEqual(len(self.main.directory), retained)
            for key, value in prior.items():
                self.assertEqual(self.main.directory[key]["prefix"], value)
            nodes = list(self.main.directory.values())
            self.assertEqual(
                [node["child_count"] for node in nodes], [1] * (retained - 1) + [0]
            )

    def test_partial_capacity_saves_only_the_publishable_front(self):
        self.finish_inference([7] * 8192)
        command = self.main.outbox.popleft()
        self.assertEqual(command["source_pages"], list(range(40)))
        self.main.receive(self.admit_ack(command, list(range(40))))
        self.free()
        self.assertEqual(len(self.main.directory), 40)
        self.assertEqual(
            self.main.ledger["p_prefix"].snapshot(),
            dict(total=40, outstanding=40, free=0),
        )

    def test_invalid_admit_receipt_fails_without_freeing_protected_source(self):
        for defect in (
            "late_rank",
            "source_slot",
            "source_pages",
            "target_alias",
            "layers",
            "region",
        ):
            with self.subTest(defect=defect):
                self.setUp()
                self.finish_inference()
                command = self.main.outbox.popleft()
                ack = self.admit_ack(command, list(range(32)))
                rank = ack["ranks"][15]
                if defect == "late_rank":
                    rank["readable"] = False
                elif defect == "source_slot":
                    rank["slot"] = 4
                elif defect == "source_pages":
                    rank["source_pages"] = list(range(1, 33))
                elif defect == "target_alias":
                    rank["target_pages"] = [0] * 32
                elif defect == "layers":
                    rank["index_layers"] = 20
                else:
                    rank["pages"][0]["region"] = "old-session"
                with self.assertRaises(ValueError):
                    self.main.receive(ack)
                self.assertIsNotNone(self.main.fault)
                self.assertFalse(self.main.outbox)
                self.assertEqual(self.main.ledger["p_alive"].outstanding, 64)
                self.assertTrue(
                    all(node["lock_ref"] == 1 for node in self.main.directory.values())
                )

    def test_timeout_retains_writing_targets_and_parent_protection(self):
        self.finish_inference()
        command = self.main.outbox.popleft()
        self.main.receive(self.admit_ack(command, list(range(32))))
        self.free()
        self.finish_inference([7] * 4096 + [8] * 1024)
        command = self.main.outbox.popleft()
        self.main.clock = lambda: self.main.started + 601
        self.main.check_timeouts()
        self.assertIsNotNone(self.main.fault)
        self.assertFalse(self.main.outbox)
        self.assertEqual(self.main.ledger["p_prefix"].outstanding, 40)
        self.assertEqual(
            sum(node["lock_ref"] for node in self.main.directory.values()), 8
        )
        self.assertEqual(
            sum(
                bool(node["prefix"]["lock_ops"])
                for node in self.main.directory.values()
            ),
            32,
        )
