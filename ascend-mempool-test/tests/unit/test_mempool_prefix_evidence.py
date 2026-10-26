"""Correlate hardware content reports with real Main allocations/publications."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

import test_mempool_router as fixture
import verify_mempool_prefix as verifier


class TestPrefixEvidence(unittest.TestCase):
    def evidence(self):
        system = fixture.TestPrefixMainRouter()
        system.setUp()
        snapshots, cases, lines = [], [], []
        retained = [{} for _ in range(16)]
        for index, (tokens, added, total) in enumerate(
            (
                ([7] * 4096, 32, 32),
                ([7] * 4096 + [8] * 1024, 8, 40),
                ([7] * 4096, 0, 40),
                ([9] * 4096, 0, 40),
            )
        ):
            request = system.finish_inference(tokens)
            command = system.main.outbox.popleft() if added else None
            if command:
                system.main.receive(
                    system.admit_ack(command, list(range(total - added, total)))
                )
            system.free()
            snapshots.append(system.main.status())
            cases.append(dict(name="cold-prefix-" + "abce"[index], input_ids=tokens))
            for rank in range(16):
                base = dict(
                    rank=rank,
                    rid=request["rid"],
                    slot=5,
                    prompt_tokens=len(tokens),
                    main_layers=78,
                    index_layers=21,
                    page_size=128,
                )
                source = {
                    str(page): f"{rank * 100 + page:064x}"
                    for page in range(len(tokens) // 128)
                }

                def report(stage, op_id, source_pages, pages):
                    lines.append(
                        "mempool prefix_content data="
                        + json.dumps(
                            dict(
                                base,
                                stage=stage,
                                op_id=op_id,
                                source_pages=source_pages,
                                pages=pages,
                            )
                        )
                    )

                report("alive", 0, list(range(len(tokens) // 128)), source)
                lines.append(
                    f"mempool native_free role=prefill rank={rank} room={request['room']}"
                )
                if command:
                    saved = {
                        str(page): source[str(page)]
                        for page in range(total - added, total)
                    }
                    retained[rank].update(saved)
                    report("admit", command["op_id"], command["source_pages"], saved)
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
                    elif change in ("wrong_source", "content"):
                        content = json.loads(records[2].split("data=")[1])
                        if change == "wrong_source":
                            content["slot"] = 4
                        else:
                            content["pages"]["0"] = "f" * 64
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
