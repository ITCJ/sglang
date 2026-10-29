"""K1-01: two explicit mappings, complete stage0 functional acceptance."""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import msgspec
from run_mempool_min import Runner, write_json
from run_ticket10 import receive_log, send_log

if TYPE_CHECKING:
    from sglang.srt.disaggregation.ascend.mempool_router_protocol import RouterConfig
    from sglang.srt.hardware_backend.npu.mempool.config import MempoolConfig
    from sglang.srt.hardware_backend.npu.mempool.sharing_plan import (
        HostTopology,
        MempoolNumaConfig,
        RankNuma,
    )
else:
    from ascend_mempool.config import MempoolConfig
    from ascend_mempool.sharing_plan import HostTopology, MempoolNumaConfig, RankNuma
    from ascend_mempool_pd.mempool_router_protocol import RouterConfig


def placement_report(
    config: RouterConfig, maps: dict[str, Any], registrations: dict[str, Any]
) -> dict[str, Any]:
    """Compare all owners with the map; residency has its own independent gate."""
    layout = MempoolConfig(
        prefill_capacity=config.prompt_capacity,
        decode_capacity=config.decode_capacity,
        page_size=config.page_size,
    ).make_mla_layout(
        num_layers=78,
        kv_lora_rank=512,
        qk_rope_head_dim=64,
        prefix_pages=40,
        indexer_layers=21,
        index_head_dim=128,
    )
    roles = {}
    for role in ("prefill", "decode"):
        assignments = tuple(
            RankNuma(r["host"], r["rank"], r["node"]) for r in maps[role]
        )
        host = config.prefill_host if role == "prefill" else config.decode_host
        plan = MempoolNumaConfig(rank_map=assignments).plan(
            hosts=(
                HostTopology(
                    host, tuple(range(16)), ",".join(map(str, config.numa_nodes))
                ),
            ),
            tp_size=16,
            role=role,
            instance_id="k1-01",
            kv_identity=config.model_path,
            layout=layout,
        )
        ranks = registrations[role]["ranks"]
        expected: dict[int, int] = {}
        observed: dict[int, int] = {}
        for assignment in assignments:
            expected[assignment.numa_node] = expected.get(
                assignment.numa_node, 0
            ) + layout.contribution_bytes(0 if role == "prefill" else 1)
        for rank in ranks:
            for node, size in rank["numa_evidence"]["resident_bytes"].items():
                observed[int(node)] = observed.get(int(node), 0) + size
        unverified = [
            r["rank"] for r in ranks if not r["numa_evidence"]["placement_verified"]
        ]
        roles[role] = dict(
            plan=plan.report(ranks),
            unverified_ranks=unverified,
            expected_bytes_by_numa=expected,
            resident_bytes_by_numa=observed,
            status="passed" if not unverified and observed == expected else "failed",
        )
    return dict(
        status="passed"
        if all(r["status"] == "passed" for r in roles.values())
        else "failed",
        roles=roles,
        user_accepted=False,
    )


def archive_registration(runner: Runner, root: Path, status: dict[str, Any]) -> None:
    write_json(root / "numa-mapping.json", runner.args.numa_maps)
    write_json(root / "numa-registration.json", status["registrations"])
    report = placement_report(
        runner.config, runner.args.numa_maps, status["registrations"]
    )
    write_json(root / "numa-placement.json", report)


def topology(root: Path, interface: str) -> None:
    """Archive the local NUMA/NPU/network topology alongside rank evidence."""
    probes = []
    for command in (
        ["numactl", "--hardware"],
        ["npu-smi", "info", "-t", "topo"],
        ["ip", "-j", "address", "show", "dev", interface],
    ):
        try:
            result = subprocess.run(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=30,
            )
            probes.append(
                dict(
                    command=command, returncode=result.returncode, output=result.stdout
                )
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            probes.append(dict(command=command, status="unavailable", error=str(error)))
    write_json(
        root / "topology.json",
        dict(
            host=os.uname().nodename,
            interface=interface,
            commands=probes,
            online_nodes=Path("/sys/devices/system/node/online").read_text().strip(),
        ),
    )


def startup_rejections(root: Path, host: str, records: list[dict[str, Any]]) -> None:
    missing_node = next(
        n for n in range(127) if not Path(f"/sys/devices/system/node/node{n}").exists()
    )
    cases = (
        ("duplicate", records + [records[0]], "NUMA_MAP_DUPLICATE_RANK"),
        ("missing", records[:-1], "NUMA_MAP_MISSING_RANK"),
        (
            "offline",
            records[:-1] + [dict(records[-1], node=missing_node)],
            "NUMA_MAP_NODE_OFFLINE",
        ),
    )
    results = []
    env = dict(os.environ, SGLANG_NPU_MEMPOOL_SHARE="0")
    env.pop("SGLANG_NPU_MEMPOOL_LOCAL_NUMA_NODE", None)
    for name, mapping, code in cases:
        path = root / f"{name}.json"
        write_json(path, mapping)
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--check-startup",
            str(path),
            "--host",
            host,
        ]
        result = subprocess.run(
            command,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
        )
        (root / f"{name}.log").write_text(result.stdout)
        results.append(
            dict(
                case=name,
                command=command,
                returncode=result.returncode,
                expected_code=code,
                status="passed"
                if result.returncode != 0 and code in result.stdout
                else "failed",
            )
        )
    write_json(root / "report.json", results)
    if any(r["status"] != "passed" for r in results):
        raise ValueError("NUMA startup rejection checks failed")


def run(runner: Runner) -> None:
    runner.report.update(
        coverage="K1-01 complete dual-mapping acceptance; not smoke", stages=[]
    )
    topology(runner.root, runner.config.interface)
    mappings = {
        role: [
            dict(host=host, rank=r, node=runner.config.numa_nodes[r // 2])
            for r in range(16)
        ]
        for role, host in (
            ("prefill", runner.config.prefill_host),
            ("decode", runner.config.decode_host),
        )
    }
    startup_rejections(
        runner.root / "rejections", runner.host, mappings[runner.args.role]
    )
    runner.peer.barrier("numa-rejections-done")
    failed = False
    for variant in ("default", "swapped"):
        maps = copy.deepcopy(mappings)
        if variant == "swapped":
            for records in maps.values():
                records[0]["node"], records[2]["node"] = (
                    records[2]["node"],
                    records[0]["node"],
                )
        root = runner.root / variant / "full-acceptance"
        root.mkdir(parents=True)
        args = copy.copy(runner.args)
        args.mode, args.config = "full-acceptance", root / "effective-config.json"
        args.run_id = f"{runner.args.run_id}-{variant}-full-acceptance"
        args.rank_numa_map, args.numa_maps = root / "rank-numa-map.json", maps
        write_json(args.config, msgspec.to_builtins(runner.config))
        write_json(args.rank_numa_map, maps[args.role])
        write_json(root / "numa-mapping.json", maps)
        child = Runner(args, runner.config, runner.peer, root)
        child.run()
        write_json(root / "summary.json", child.report)
        if runner.is_p:
            placement = json.loads((root / "target/numa-placement.json").read_text())
            stage = dict(
                variant=variant,
                mode=args.mode,
                functional_status=child.report["status"],
                placement_status=placement["status"],
                placement_reports=[placement],
            )
            write_json(root / "numa-stage.json", stage)
            send_log(runner.peer, root / "numa-stage.json")
        else:
            receive_log(runner.peer, root / "numa-stage.json")
            stage = json.loads((root / "numa-stage.json").read_text())
        failed |= (
            stage["placement_status"] != "passed"
            or stage["functional_status"] != "passed"
        )
        runner.report["stages"].append(stage)
        write_json(runner.root / "summary.json", runner.report)
        runner.peer.barrier(f"{variant}-full-acceptance-archived")
    runner.report.update(
        status="failed" if failed else "passed",
        user_accepted=False,
        hardware_execution="completed by user runner",
    )
    if failed:
        raise ValueError(
            "K1-01 functional acceptance incomplete; inspect placement and V1-V10 reports"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run the production startup-config gate without allocating BM"
    )
    parser.add_argument("--check-startup", type=Path, required=True)
    parser.add_argument("--host", required=True)
    args = parser.parse_args()
    MempoolConfig.from_server_args(
        SimpleNamespace(
            page_size=128,
            disaggregation_mode="decode",
            disable_cuda_graph=False,
            mempool_prefill_host=args.host,
            mempool_nic=f"tcp://{args.host}:25670",
            mempool_rank_numa_map=str(args.check_startup),
        ),
        sparse_enabled=True,
        mla=True,
        dtype="bfloat16",
        mlapo=False,
    )
