"""Acceptance rejects stale fill behavior and incorrect completed write bytes."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ascend_mempool.page_write import PageWriter
from verify_dual_write import check_stage


class TestDualWriteAcceptance(unittest.TestCase):
    def test_service_evidence_accepts_publish_and_rejects_fill_or_wrong_bytes(self):
        config = SimpleNamespace(
            page_size=128,
            prefix_pages=40,
            prefill_groups=tuple((r, r + 1) for r in range(0, 16, 2)),
            prefill_write_assignment="contiguous",
        )
        snapshots, cases, logs = [], [], []
        for index, (tokens, hit) in enumerate(
            zip([4096, 5120, 4096, 4096, 8192, 1024], [0, 4096, 3968, 0, 0, 0])
        ):
            targets = (
                list(range(32))
                if index == 0
                else list(range(32, 40))
                if index == 1
                else []
            )
            retained = 32 if index == 0 else 40
            ranks = []
            for rank in range(16):
                owned = [p for p in targets if config.prefill_groups[p // 5][0] == rank]
                ranks.append(
                    dict(
                        rank=rank,
                        target_pages=targets,
                        written_pages=owned,
                        main_bytes=0,
                        index_bytes=0,
                    )
                )
                remote = PageWriter(rank % 2, 2, 128, "contiguous").rows(
                    hit, tokens, tokens
                )
                candidate = len(owned) * 128
                logs.append(
                    dict(
                        main_remote_rows=remote,
                        candidate_main_rows=candidate,
                        candidate_index_rows=candidate,
                        main_remote_bytes=remote * 78 * 1152,
                        candidate_main_bytes=candidate * 78 * 1152,
                        candidate_index_bytes=candidate * 21 * 256,
                        p_alive_bytes=0,
                        completion="forward_event",
                    )
                )
            done = dict(
                hit_len=hit,
                reads={"load_back": dict(ack=dict(ranks=ranks))} if hit else {},
                admit=dict(
                    command=dict(target_pages=targets, sources=[]),
                    ack=dict(ranks=ranks),
                ),
                trace=[
                    dict(role=role, kind=kind)
                    for role, kind in [
                        ("decode", "finished"),
                        ("prefill", "admit"),
                        ("prefill", "admit_ack"),
                        ("prefill", "free"),
                    ]
                ],
            )
            snapshots.append(
                dict(
                    completed=[done],
                    directory={
                        str(p): dict(prefix=dict(lock_ops=[])) for p in range(retained)
                    },
                    ledger=dict(
                        p_prefix=dict(
                            total=40, free=40 - retained, outstanding=retained
                        )
                    ),
                )
            )
            cases.append(dict(input_ids=[7] * tokens))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "responses.json").write_text(json.dumps(dict(cases=cases)))

            def write(snapshots, logs):
                (root / "snapshots.json").write_text(json.dumps(snapshots))
                (root / "prefill.log").write_text(
                    "\n".join(
                        "mempool dual_write_complete data=" + json.dumps(r)
                        for r in logs
                    )
                )

            write(snapshots, logs)
            self.assertEqual(check_stage(root, config)["status"], "passed")
            invalid = copy.deepcopy(snapshots)
            invalid[1]["completed"][-1]["reads"]["fill"] = {}
            write(invalid, logs)
            with self.assertRaisesRegex(ValueError, "unexpected fill"):
                check_stage(root, config)
            wrong = copy.deepcopy(logs)
            wrong[0]["main_remote_bytes"] += 1
            write(snapshots, wrong)
            with self.assertRaisesRegex(ValueError, "row/byte"):
                check_stage(root, config)
