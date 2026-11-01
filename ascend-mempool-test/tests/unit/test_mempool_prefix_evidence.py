"""Correlate hardware content reports with real Main allocations/publications."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

import test_mempool_router as fixture
import verify_mempool_prefix as verifier


class TestPrefixEvidence(unittest.TestCase):
    def evidence(
        self,
        *,
        reuse=False,
        full=False,
        controls=False,
        index_checks=False,
        shared=False,
    ):
        system = fixture.TestPrefixMainRouter()
        system.setUp(shared=shared)
        physical_order = []

        def source_value(rank, page):
            return (
                {
                    "main": f"{rank * 100 + page:064x}",
                    "index": f"{rank * 200 + page + 10000:064x}",
                }
                if shared
                else f"{rank * 100 + page:064x}"
            )

        snapshots, cases, lines = [], [], []
        retained = [{} for _ in range(16)]
        scenarios = (
            ([7] * 4096, 32, 32),
            ([7] * 4096 + [8] * 1024, 8, 40),
            ([7] * 4096, 0, 40),
            ([9] * 4096, 0, 40),
        )
        if reuse:
            from msgspec.structs import replace

            system.main.config = replace(system.main.config, match_prefix=True)
            if not full:
                scenarios = scenarios[:3]
        if controls:
            scenarios += (([10] * 4096, 0, 40), ([11] * 128, 0, 40))
        if index_checks:
            scenarios += (
                ([12] * 4096, 0, 40),
                ([7] * 4096 + [13] * 1024, 0, 40),
                ([7] * 4096, 0, 40),
            )
        for index, (tokens, added, total) in enumerate(scenarios):
            request, p_dispatch, d_dispatch = system.start_inference(tokens)
            hit = request["hit_len"]
            indices = [
                p * 128 + t for p in range(1, 2 * (hit // 128), 2) for t in range(128)
            ]
            if hit:
                load = system.main.outbox.popleft()

                def read_receipt(rank):
                    return dict(
                        slot=5,
                        readable=True,
                        tokens=hit,
                        source_pages=[
                            p["logical_page"] for p in load["sources"][rank]["pages"]
                        ],
                        main_layers=78,
                        index_layers=21,
                    )

                system.ack(
                    load, lambda rank: dict(read_receipt(rank), prefix_indices=indices)
                )
                fill = system.main.outbox.popleft()
                system.main.receive(
                    dict(
                        p_dispatch,
                        kind="native_freed",
                        ranks=[dict(rank=r, native_freed=True) for r in range(16)],
                    )
                )
                system.ack(fill, read_receipt)
            system.finish_started(request, p_dispatch, d_dispatch)
            command = system.main.outbox.popleft() if added else None
            if command:
                targets = list(range(total - added, total))
                if shared:
                    from ascend_mempool.prefix_layout import PrefixLayout

                    targets = PrefixLayout(
                        40, system.main.config.prefill_groups
                    ).select(set(range(40)) - set(physical_order), added, index)
                physical_order.extend(targets)
                ack = system.admit_ack(command, targets)
                writers = {}
                if shared:
                    for page, target in zip(command["source_pages"], targets):
                        writers[str(target)] = verifier.prefix_source_writers(
                            system.main.status(), page, target, len(tokens)
                        )
                    for receipt in ack["ranks"]:
                        receipt["source_digests"] = {
                            str(target): {
                                region: source_value(writer, page)[region]
                                for region, writer in writers[str(target)].items()
                                if writer == receipt["rank"]
                            }
                            for page, target in zip(command["source_pages"], targets)
                        }
                system.main.receive(ack)
            system.free()
            snapshots.append(system.main.status())
            cases.append(
                dict(
                    name=("reuse-prefix-" if reuse else "cold-prefix-") + "abce"[index]
                    if index < 4
                    else (
                        "long-generation",
                        "early-eos",
                        "index-cold",
                        "index-update",
                        "index-full-hit",
                    )[index - 4],
                    input_ids=tokens,
                )
            )
            for rank in range(16):
                base = dict(
                    rank=rank,
                    rid=request["rid"],
                    slot=5,
                    prompt_tokens=len(tokens),
                    main_layers=78,
                    index_layers=21,
                    page_size=128,
                    prefix_shared=shared,
                )
                source = {
                    str(page): source_value(rank, page)
                    for page in range(len(tokens) // 128)
                }

                def report(stage, op_id, source_pages, pages, **evidence):
                    lines.append(
                        "mempool prefix_content data="
                        + json.dumps(
                            dict(
                                base,
                                stage=stage,
                                op_id=op_id,
                                source_pages=source_pages,
                                pages=pages,
                                content_source=(
                                    "native_hbm"
                                    if stage == "load_back"
                                    else "computed_source"
                                ),
                                bm_readback=False,
                                **evidence,
                            )
                        )
                    )

                if hit:
                    prefix_values = {
                        str(p): retained[rank][str(physical_order[p])]
                        for p in range(hit // 128)
                    }
                    source.update(prefix_values)
                    report(
                        "load_back",
                        load["op_id"],
                        physical_order[: hit // 128],
                        prefix_values,
                        prefix_indices=indices,
                        parts=[512, 64],
                    )
                    lines.append(
                        f"mempool sr role=prefill rank={rank} event=load_back rid={request['rid']} op_id={load['op_id']}"
                    )
                    report(
                        "suffix",
                        0,
                        list(range(hit // 128, len(tokens) // 128)),
                        {p: v for p, v in source.items() if int(p) >= hit // 128},
                    )
                else:
                    report("alive", 0, list(range(len(tokens) // 128)), source)
                lines.append(
                    f"mempool native_free role=prefill rank={rank} room={request['room']}"
                )
                if hit:

                    def progress(filled):
                        lines.append(
                            "mempool prefix_progress data="
                            + json.dumps(
                                dict(
                                    rid=request["rid"],
                                    room=request["room"],
                                    rank=rank,
                                    hit_len=hit,
                                    suffix_start=hit,
                                    suffix_completed=len(tokens) - hit,
                                    native_freed=True,
                                    fill_complete=filled,
                                    ready_tokens=len(tokens) if filled else 0,
                                )
                            )
                        )

                    progress(False)
                    report(
                        "fill",
                        fill["op_id"],
                        physical_order[: hit // 128],
                        prefix_values,
                    )
                    lines.append(
                        f"mempool sr role=prefill rank={rank} event=fill rid={request['rid']} op_id={fill['op_id']}"
                    )
                    progress(True)
                    report("alive", 0, list(range(len(tokens) // 128)), source)
                if command:
                    saved = {
                        str(target): {
                            region: source_value(writer, page)[region]
                            for region, writer in writers[str(target)].items()
                        }
                        if shared
                        else source[str(page)]
                        for page, target in zip(command["source_pages"], targets)
                    }
                    retained[rank].update(saved)
                    report(
                        "admit",
                        command["op_id"],
                        command["source_pages"],
                        saved,
                        **({"page_sources": writers} if shared else {}),
                    )
                    lines.append(
                        f"mempool sr role=prefill rank={rank} event=admit rid={request['rid']} op_id={command['op_id']}"
                    )
                report("retained", 0, [], retained[rank])
                free_op = next(
                    t["op_id"]
                    for t in snapshots[-1]["completed"][-1]["trace"]
                    if t["kind"] == "free" and t["role"] == "prefill"
                )
                lines.append(
                    f"mempool sr role=prefill rank={rank} event=free rid={request['rid']} op_id={free_op}"
                )
        return snapshots, cases, lines

    def test_shared_source_digests_follow_actual_writers_and_reject_corruption(self):
        import verify_mempool_reuse

        snapshots, cases, lines = self.evidence(
            reuse=True, full=True, controls=True, shared=True
        )
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "prefill.log"
            log.write_text("\n".join(lines))
            report = verify_mempool_reuse.check_reuse(log, snapshots, cases, full=True)
            self.assertEqual(report["hit_lengths"], [0, 4096, 3968, 0, 0, 0])
            for stage, field in (("admit", "page_sources"), ("load_back", "pages")):
                bad = lines.copy()
                for i, line in enumerate(bad):
                    if "prefix_content" not in line:
                        continue
                    value = json.loads(line.split(" data=", 1)[1])
                    if value["stage"] == stage:
                        key = next(iter(value[field]))
                        value[field][key]["main"] = (
                            15 if field == "page_sources" else "f" * 64
                        )
                        bad[i] = "mempool prefix_content data=" + json.dumps(value)
                        break
                log.write_text("\n".join(bad))
                with self.assertRaises(ValueError):
                    verify_mempool_reuse.check_reuse(log, snapshots, cases, full=True)

    def test_full_evidence_and_corrupted_rank_page_content_or_order(self):
        snapshots, cases, lines = self.evidence()
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "prefill.log"

            def check(data=snapshots, records=lines):
                log.write_text("\n".join(records))
                return verifier.check_prefix(log, data, cases)

            self.assertEqual(check()["pages_per_request"], [32, 40, 40, 40])
            for change in (
                "missing_rank",
                "wrong_source",
                "content",
                "bm_readback",
                "content_source",
                "early_free",
                "early_ack",
                "directory",
                "short_receipt",
                "hit",
            ):
                with self.subTest(change=change):
                    data, records = copy.deepcopy(snapshots), list(lines)
                    if change == "missing_rank":
                        records.pop(2)
                    elif change in (
                        "wrong_source",
                        "content",
                        "bm_readback",
                        "content_source",
                    ):
                        content = json.loads(records[2].split("data=")[1])
                        if change == "wrong_source":
                            content["slot"] = 4
                        elif change == "content":
                            content["pages"]["0"] = "f" * 64
                        elif change == "bm_readback":
                            content["bm_readback"] = True
                        else:
                            content["content_source"] = "bm_dram"
                        records[2] = "mempool prefix_content data=" + json.dumps(
                            content
                        )
                    elif change == "early_free":
                        records[2], records[5] = records[5], records[2]
                    elif change == "early_ack":
                        trace = data[0]["completed"][-1]["trace"]
                        a = next(
                            i
                            for i, item in enumerate(trace)
                            if item["kind"] == "admit_ack"
                        )
                        trace[a], trace[a + 1] = trace[a + 1], trace[a]
                    elif change == "directory":
                        next(iter(data[0]["directory"].values()))["prefix"][
                            "locations"
                        ][0]["logical_page"] = 39
                    elif change == "short_receipt":
                        data[0]["completed"][-1]["admit"]["ack"]["ranks"][15][
                            "pages"
                        ].pop()
                    else:
                        data[0]["completed"][-1]["hit_len"] = 128
                    with self.assertRaises(ValueError):
                        check(data, records)


if __name__ == "__main__":
    unittest.main()
