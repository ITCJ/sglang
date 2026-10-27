"""Calculate the explanatory deployment examples with the production planner."""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
sys.path.insert(0, str(REPO / "python/sglang/srt/hardware_backend/npu"))

from mempool.config import MempoolConfig
from mempool.sharing_plan import HostTopology, MempoolNumaConfig


LAYOUT = MempoolConfig(
    prefill_capacity=4096, decode_capacity=8192
).make_mla_layout(num_layers=78, kv_lora_rank=512, qk_rope_head_dim=64)


def make_plan(name, host, role, tp_size, nodes, instance, share=True, **ports):
    topology = [dict(host=host, ranks=list(range(tp_size)), online_nodes="0-7")]
    records = [dict(host=host, rank=rank, node=node) for rank, node in enumerate(nodes)]
    (HERE / f"{name}-topology.json").write_text(json.dumps(topology, indent=2) + "\n")
    mapping = HERE / f"{name}-map.json"
    mapping.write_text(json.dumps(records, indent=2) + "\n")
    config = MempoolNumaConfig.from_environment(
        rank_numa_map=str(mapping),
        environ={"SGLANG_NPU_MEMPOOL_SHARE": "1" if share else "0"},
    )
    plan = config.plan(
        hosts=(HostTopology(host, tuple(range(tp_size)), "0-7"),),
        tp_size=tp_size,
        role=role,
        instance_id=instance,
        kv_identity="example-glm52-bf16-compact576",
        layout=LAYOUT,
        **ports,
    )
    (HERE / f"{name}-report.json").write_text(json.dumps(plan.report(), indent=2) + "\n")
    return plan


def main():
    summary = {
        "scope": "CPU planning examples; no BM or NPU allocation",
        "assumptions": "example 1: 1P/1D; example 2: 2P/2D, one TP32 per host",
        "layout": LAYOUT.signature(),
        "examples": {},
    }
    for case, tp_size, group_size, pairs in (
        ("case1", 16, 2, 1),
        ("case2", 32, 4, 2),
    ):
        nodes = [rank // group_size for rank in range(tp_size)]
        independent_bytes = shared_bytes = 0
        for pair in range(pairs):
            shared = {}
            for role in ("prefill", "decode"):
                side = "p" if role == "prefill" else "d"
                host = f"{case}-{side}{pair}"
                instance = f"{case}-pair-{pair}"
                for share in (False, True):
                    label = f"{host}-{'shared' if share else 'independent'}"
                    plan = make_plan(label, host, role, tp_size, nodes, instance, share)
                    if share:
                        shared[role] = plan
                        shared_bytes += plan.report()["planned_contribution_bytes"]
                    else:
                        independent_bytes += plan.report()["planned_contribution_bytes"]
            shared["prefill"].validate_peer(shared["decode"])
            shared["decode"].validate_peer(shared["prefill"])
            assert [g.members for g in shared["prefill"].groups] == [
                tuple(range(first, first + group_size))
                for first in range(0, tp_size, group_size)
            ]
        summary["examples"][case] = dict(
            machines=2 * pairs,
            tp_size=tp_size,
            group_size=group_size,
            paired_bm_groups=pairs * 8,
            positive_contributors=2 * pairs * 8,
            participants=2 * pairs * tp_size,
            independent_GiB=independent_bytes // 2**30,
            shared_GiB=shared_bytes // 2**30,
        )
        assert independent_bytes == group_size * shared_bytes

    # Illustrative future DP isolation: four separate TP8 logical instances.
    # This does not implement or validate the real DP serving path.
    dp_digests = set()
    for replica in range(4):
        nodes = [2 * replica + rank // 4 for rank in range(8)]
        pair = []
        for role in ("prefill", "decode"):
            side = "p" if role == "prefill" else "d"
            pair.append(make_plan(
                f"dp-example-{side}-replica{replica}", f"dp-example-{side}", role,
                8, nodes, f"future-dp-pair-0-replica-{replica}",
                base_port=19000 + 100 * replica,
                nic_port=25670 + 100 * replica,
            ))
        pair[0].validate_peer(pair[1])
        assert len(pair[0].groups) == 2
        dp_digests.add(pair[0].group_identity(pair[0].groups[0]))
    assert len(dp_digests) == 4

    (HERE / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
