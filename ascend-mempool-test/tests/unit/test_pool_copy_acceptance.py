"""The service report must reject missing delay and missing requested pages."""

import json
from pathlib import Path
import tempfile
import unittest
from run_pool_copy_acceptance import verify


class TestPoolCopyAcceptance(unittest.TestCase):
    def test_valid_trace_and_disabled_delay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for phase in ("target", "restart"):
                folder = root / phase
                folder.mkdir()
                lines = []
                for op_id, kind in ((1, "admit"), (2, "fill")):
                    for rank in range(16):
                        blocks = (
                            (2 if rank == 0 else 0)
                            if kind == "admit"
                            else (2 if rank == 0 else 1 if rank % 2 == 0 else 0)
                        )
                        size = 12189696 if rank == 0 else 11501568 if blocks else 0
                        row = dict(
                            rid="r",
                            op_id=op_id,
                            kind=kind,
                            rank=rank,
                            blocks=blocks,
                            bytes=size,
                        )
                        lines.append("mempool pool_copy_submit data=" + json.dumps(row))
                        done = dict(
                            rid="r",
                            op_id=op_id,
                            rank=rank,
                            state="READY",
                            ready_at=2,
                            delay_started=1,
                            delay_seconds=0.05 if blocks else 0,
                            poll_count=3,
                            submit_return=0,
                            wait_return=0,
                        )
                        lines.append(
                            "mempool pool_copy_complete data=" + json.dumps(done)
                        )
                        lines.append(
                            f"mempool sr role=prefill rank={rank} event={kind} rid=r room=1 attempt=a op_id={op_id} free=15"
                        )
                (folder / "prefill-normal.log").write_text("\n".join(lines))
                (folder / "snapshots.json").write_text(
                    json.dumps(
                        [
                            {
                                "completed": [
                                    dict(
                                        rid="r",
                                        hit_len=128,
                                        admit={
                                            "command": dict(op_id=1, source_pages=[0])
                                        },
                                        reads={"fill": {"command": dict(op_id=2)}},
                                    )
                                ]
                            }
                        ]
                    )
                )
            self.assertEqual(len(verify(root)), 2)
            log = root / "target/prefill-normal.log"
            log.write_text(
                log.read_text().replace('"delay_seconds": 0.05', '"delay_seconds": 0')
            )
            with self.assertRaisesRegex(ValueError, "delay was not applied"):
                verify(root)
