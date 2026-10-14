"""Exercise NUMA configuration and sharing plans without allocating a pool."""

import json
import tempfile
import unittest
from pathlib import Path

from ascend_mempool.layout import KVLayout, PoolLayout
from ascend_mempool.sharing_plan import HostTopology, MempoolNumaConfig, RankNuma


class TestMempoolNumaPlan(unittest.TestCase):
    def config(self, nodes, *, share="1", hosts=None):
        records = [
            {
                "host": (hosts or ["p-host"] * len(nodes))[rank],
                "rank": rank,
                "node": node,
            }
            for rank, node in enumerate(nodes)
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "map.json"
            path.write_text(json.dumps(records))
            return MempoolNumaConfig.from_environment(
                rank_numa_map=str(path), environ={"SGLANG_NPU_MEMPOOL_SHARE": share}
            )

    def plan(self, config, *, hosts=None, **overrides):
        options = dict(
            hosts=hosts
            if hosts is not None
            else (HostTopology("p-host", (0, 1, 2, 3), "0,2"),),
            tp_size=4,
            role="prefill",
            instance_id="run-one",
            kv_identity="glm52-bf16-w4a8",
            layout=PoolLayout(KVLayout(2, 16, 8, 1, 576), KVLayout(2, 16, 4, 1, 576)),
        )
        options.update(overrides)
        return config.plan(**options)

    def test_share_switch_is_explicit_and_strict(self):
        """Old launches stay independent; malformed switches never enable sharing."""
        self.assertFalse(MempoolNumaConfig.from_environment(environ={}).mempool_share)
        for value, expected in (("0", False), ("1", True)):
            config = MempoolNumaConfig.from_environment(
                environ={"SGLANG_NPU_MEMPOOL_SHARE": value}
            )
            self.assertEqual(config.mempool_share, expected)
        for value in ("", "true", "false", "2", "-1", "01", " 1 "):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "SHARE"):
                MempoolNumaConfig.from_environment(
                    environ={"SGLANG_NPU_MEMPOOL_SHARE": value}
                )

    def test_legacy_rotation_and_sharing_are_independent(self):
        """The old 0,2 list groups nonadjacent ranks without changing placement."""
        for share, groups in (("0", [(0,), (1,), (2,), (3,)]), ("1", [(0, 2), (1, 3)])):
            with self.subTest(share=share):
                config = MempoolNumaConfig.from_environment(
                    environ={
                        "SGLANG_NPU_MEMPOOL_SHARE": share,
                        "SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE": "0,2",
                    }
                )
                plan = self.plan(config)
                self.assertEqual([a.numa_node for a in plan.assignments], [0, 2, 0, 2])
                self.assertEqual([g.members for g in plan.groups], groups)
                self.assertEqual(
                    [g.owner_tp_rank for g in plan.groups], [g[0] for g in groups]
                )

    def test_explicit_map_supports_variable_groups_without_crossing_hosts(self):
        """One, two, four and uneven member counts use the same grouping rule."""
        for nodes, expected in (
            ([0, 2, 4, 6], [(0,), (1,), (2,), (3,)]),
            ([0, 0, 2, 2], [(0, 1), (2, 3)]),
            ([0, 0, 0, 0], [(0, 1, 2, 3)]),
            ([0, 2, 2, 2], [(0,), (1, 2, 3)]),
        ):
            with self.subTest(nodes=nodes):
                plan = self.plan(
                    self.config(nodes),
                    hosts=(HostTopology("p-host", (3, 1, 0, 2), "0-7"),),
                )
                self.assertEqual([g.members for g in plan.groups], expected)
                self.assertEqual([a.numa_node for a in plan.assignments], nodes)
        plan = self.plan(
            self.config([0, 0, 0, 0], hosts=["a", "a", "b", "b"]),
            hosts=(HostTopology("b", (2, 3), "0"), HostTopology("a", (0, 1), "0")),
        )
        self.assertEqual([g.members for g in plan.groups], [(0, 1), (2, 3)])
        self.assertEqual([g.host for g in plan.groups], ["a", "b"])
        independent = self.plan(self.config([0, 0, 0, 0], share="0"))
        self.assertEqual(
            [g.members for g in independent.groups], [(0,), (1,), (2,), (3,)]
        )

    def test_incomplete_or_ambiguous_assignments_fail_before_a_plan_is_returned(self):
        """Every rank must occur once, on its declared host and an online node."""
        valid = self.config([0, 0, 2, 2]).rank_map
        cases = (
            valid[:-1],
            valid + (valid[0],),
            valid[:-1] + (RankNuma("other-host", 3, 2),),
            valid[:-1] + (RankNuma("p-host", 4, 2),),
            valid[:-1] + (RankNuma("p-host", 3, 6),),
            valid[:-1] + (RankNuma("p-host", 3, -1),),
            valid[:-1] + (RankNuma("p-host", True, 2),),
            valid[:-1] + (RankNuma("p-host", 3, True),),
        )
        for share in (False, True):
            for assignments in cases:
                with (
                    self.subTest(share=share, assignments=assignments),
                    self.assertRaises(ValueError),
                ):
                    self.plan(
                        MempoolNumaConfig(mempool_share=share, rank_map=assignments)
                    )
        for hosts in (
            (),
            (HostTopology("p-host", (0, 1, 2), "0,2"),),
            (HostTopology("p-host", (0, 1, 2, 3, 3), "0,2"),),
            (HostTopology("a", (0, 1), "0,2"), HostTopology("b", (1, 2, 3), "0,2")),
            (
                HostTopology("p-host", (0, 1), "0,2"),
                HostTopology("p-host", (2, 3), "0,2"),
            ),
        ):
            with self.subTest(hosts=hosts), self.assertRaises(ValueError):
                self.plan(self.config([0, 0, 2, 2]), hosts=hosts)

    def test_map_file_rejects_invalid_schema_and_conflicting_inputs(self):
        """Explicit placement has no fallback, including when sharing is off."""
        cases = (
            "not json",
            "{}",
            "[]",
            '[{"host":"p-host","rank":0,"node":0,"typo":1}]',
            '[{"host":"p-host","rank":0,"node":"0"}]',
            '[{"host":"p-host","rank":false,"node":0}]',
            '[{"host":"","rank":0,"node":0}]',
            '[{"host":"p-host","rank":0,"node":127}]',
            '[{"host":"p-host","rank":0,"node":0,"node":2}]',
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "map.json"
            for value in cases:
                path.write_text(value)
                with self.subTest(value=value), self.assertRaises(ValueError):
                    MempoolNumaConfig.from_environment(
                        rank_numa_map=str(path), environ={}
                    )
            path.write_text('[{"host":"p-host","rank":0,"node":0}]')
            with self.assertRaisesRegex(ValueError, "conflicts"):
                MempoolNumaConfig.from_environment(
                    rank_numa_map=str(path),
                    environ={"SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE": ""},
                )

    def test_legacy_fallback_is_preserved_only_for_independent_pools(self):
        """An unknown node cannot become a shared group key."""
        for nodes, online in (
            (None, None),
            ("", "0-7"),
            ("0,0", "0-7"),
            ("0,8", "0-7"),
            ("-1", "0-7"),
            ("127", "0-127"),
            ("0,2", None),
            ("0,2", "3-1"),
            ("0,2", "0,,2"),
        ):
            env = {} if nodes is None else {"SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE": nodes}
            hosts = (HostTopology("p-host", (0, 1, 2, 3), online),)
            with self.subTest(nodes=nodes, online=online):
                plan = self.plan(
                    MempoolNumaConfig.from_environment(environ=env), hosts=hosts
                )
                self.assertEqual([a.numa_node for a in plan.assignments], [-1] * 4)
                self.assertEqual(len(plan.warnings), int(nodes is not None))
                with self.assertRaises(ValueError):
                    self.plan(
                        MempoolNumaConfig.from_environment(
                            environ={**env, "SGLANG_NPU_MEMPOOL_SHARE": "1"}
                        ),
                        hosts=hosts,
                    )

    def test_group_report_separates_owners_participants_and_physical_contribution(self):
        """Planning preserves capacity and gives each member a private probe lane."""
        layout = PoolLayout(
            KVLayout(2, 16, 8, 1, 576), KVLayout(2, 16, 4, 1, 576), alignment_bytes=64
        )
        plan = self.plan(self.config([0, 0, 0, 0]), layout=layout)
        rows = plan.report()["ranks"]
        self.assertEqual([r["participant"] for r in rows], [0, 1, 2, 3])
        self.assertEqual([r["owner_participant"] for r in rows], [0, 0, 0, 0])
        self.assertEqual([r["contribution_bytes"] for r in rows], [295168, 0, 0, 0])
        self.assertEqual(
            [r["probe_offset"] for r in rows], [294912, 294976, 295040, 295104]
        )
        self.assertEqual({r["pool_identity"] for r in rows}, {rows[0]["pool_identity"]})
        self.assertEqual(plan.layout.prompt.tokens, 8)
        self.assertEqual(plan.layout.decode.tokens, 4)
        self.assertEqual(plan.report()["allocation_status"], "planned")
        decode = self.plan(self.config([0, 0, 0, 0]), role="decode", layout=layout)
        d_rows = decode.report()["ranks"]
        self.assertEqual([r["participant"] for r in d_rows], [4, 5, 6, 7])
        self.assertEqual([r["owner_participant"] for r in d_rows], [4, 4, 4, 4])
        self.assertEqual([r["contribution_bytes"] for r in d_rows], [147712, 0, 0, 0])
        self.assertEqual(
            [r["pool_identity"] for r in d_rows], [r["pool_identity"] for r in rows]
        )
        all_ports = [r["nic_port"] for r in rows + d_rows]
        self.assertEqual(len(set(all_ports)), 8)

    def test_ports_remain_disjoint_for_nonadjacent_groups_and_independent_mode(self):
        """MF adds participant to NIC base; separate member ranges avoid collisions."""
        config = MempoolNumaConfig.from_environment(
            environ={
                "SGLANG_NPU_MEMPOOL_SHARE": "1",
                "SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE": "0,2",
            }
        )
        p = self.plan(config).report()["ranks"]
        d = self.plan(config, role="decode").report()["ranks"]
        self.assertEqual([r["store_port"] for r in p], [19000, 19001, 19000, 19001])
        self.assertEqual([r["nic_port"] for r in p], [25670, 25678, 25675, 25683])
        self.assertEqual([r["nic_port"] for r in d], [25672, 25680, 25677, 25685])
        original = self.plan(MempoolNumaConfig(), role="decode").report()["ranks"]
        self.assertEqual(
            [r["nic_base_port"] for r in original], [25670, 25672, 25674, 25676]
        )
        self.assertEqual(
            [r["store_port"] for r in original], [19000, 19001, 19002, 19003]
        )
        for overrides in (
            {"nic_port": 65530},
            {"base_port": 65535},
            {"nic_port": 19000},
            {"pool_id": 256},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.plan(config, **overrides)

    def test_peer_nic_collisions_are_checked_per_physical_host(self):
        """Different NIC bases can still produce the same MF listener endpoint."""
        config = self.config([0, 0, 2, 2])
        prefill = self.plan(config)
        collision = self.plan(config, role="decode", nic_port=25668)
        self.assertEqual(
            [r["nic_port"] for r in prefill.report()["ranks"]],
            [r["nic_port"] for r in collision.report()["ranks"]],
        )
        with self.assertRaisesRegex(ValueError, "NIC.*collid"):
            prefill.validate_peer(collision)
        with self.assertRaisesRegex(ValueError, "NIC.*collid"):
            collision.validate_peer(prefill)
        prefill.validate_peer(self.plan(config, role="decode"))
        prefill.validate_peer(self.plan(config, role="decode", nic_port=26000))
        other_host = self.plan(
            self.config([0, 0, 2, 2], hosts=["d-host"] * 4),
            hosts=(HostTopology("d-host", (0, 1, 2, 3), "0,2"),),
            role="decode",
            nic_port=25668,
        )
        prefill.validate_peer(other_host)

    def test_peer_matching_uses_logical_members_and_kv_identity(self):
        """P/D may use different host/node IDs, but must share one logical plan."""
        prefill = self.plan(self.config([0, 0, 2, 2]))
        decode_config = self.config([4, 4, 6, 6], hosts=["d-host"] * 4)
        decode_hosts = (HostTopology("d-host", (0, 1, 2, 3), "4,6"),)
        decode = self.plan(
            decode_config, hosts=decode_hosts, role="decode", nic_port=26000
        )
        prefill.validate_peer(decode)
        decode.validate_peer(prefill)
        self.assertEqual(
            [r["pool_identity"] for r in prefill.report()["ranks"]],
            [r["pool_identity"] for r in decode.report()["ranks"]],
        )
        for overrides in (
            {"role": "prefill"},
            {"instance_id": "other-run"},
            {"kv_identity": "different-quantization"},
            {"pool_id": 105},
            {"base_port": 19500},
            {
                "layout": PoolLayout(
                    KVLayout(3, 16, 8, 1, 576), KVLayout(3, 16, 4, 1, 576)
                )
            },
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                prefill.validate_peer(
                    self.plan(
                        decode_config,
                        hosts=decode_hosts,
                        **({"role": "decode"} | overrides),
                    )
                )
        for config in (self.config([0, 2, 0, 2]), self.config([0, 0, 2, 2], share="0")):
            with self.subTest(config=config), self.assertRaises(ValueError):
                prefill.validate_peer(self.plan(config, role="decode"))

    def test_tp16_default_pairs_reduce_planned_replicas_without_resizing_kv(self):
        """The deployment default has eight owners; the planner has no eight-group limit."""
        hosts = (HostTopology("p-host", tuple(range(16)), "0-15"),)
        independent = self.plan(
            self.config(
                [0, 0, 2, 2, 4, 4, 6, 6, 8, 8, 10, 10, 12, 12, 14, 14], share="0"
            ),
            hosts=hosts,
            tp_size=16,
        )
        shared = self.plan(
            self.config([0, 0, 2, 2, 4, 4, 6, 6, 8, 8, 10, 10, 12, 12, 14, 14]),
            hosts=hosts,
            tp_size=16,
        )
        self.assertEqual(independent.report()["group_count"], 16)
        self.assertEqual(shared.report()["group_count"], 8)
        self.assertEqual(
            independent.report()["planned_contribution_bytes"], 16 * 1024**3
        )
        self.assertEqual(shared.report()["planned_contribution_bytes"], 8 * 1024**3)
        self.assertEqual(independent.layout, shared.layout)

    def test_global_rank_rotation_does_not_restart_on_the_next_host(self):
        config = MempoolNumaConfig.from_environment(
            environ={
                "SGLANG_NPU_MEMPOOL_SHARE": "1",
                "SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE": "6,0,2",
            }
        )
        hosts = (HostTopology("a", (0, 1), "0-7"), HostTopology("b", (2, 3), "0-7"))
        self.assertEqual(
            [a.numa_node for a in self.plan(config, hosts=hosts).assignments],
            [6, 0, 2, 6],
        )


if __name__ == "__main__":
    unittest.main()
