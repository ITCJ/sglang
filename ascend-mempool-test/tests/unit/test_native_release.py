"""Exercise actual Ascend release methods with CPU transport/storage boundaries.

The server module imports torch_npu on Mac. Compile its unchanged class methods
instead of mocking them; substitute only native transport and storage objects.
"""

import ast
import logging
import unittest
from pathlib import Path
from types import SimpleNamespace


def load_methods(path, name, methods, namespace, *, standalone=False):
    """Load production method bodies without running device-only module imports."""
    root = Path(__file__).resolve().parents[3] / "python/sglang/srt"
    source = root / path
    tree = ast.parse(source.read_text())
    definition = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name
    )
    if standalone:
        definition.bases = []
    definition.body = [
        n
        for n in definition.body
        if isinstance(n, ast.FunctionDef) and n.name in methods
    ]
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0
            ),
            definition,
        ],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
    return namespace[name]


def native_receiver(room, events, *, mempool_control=None, senders=1):
    """Use real abort/ACK methods with a native transport status sink."""
    namespace = dict(
        KVPoll=SimpleNamespace(Success="success", Failed="failed"),
        BaseKVReceiver=object,
        logger=logging.getLogger(__name__),
    )
    manager_base = load_methods(
        "disaggregation/common/conn.py",
        "CommonKVManager",
        {
            "register_deferred_abort_room",
            "note_abort_ack",
            "is_abort_release_safe",
            "clear_deferred_abort_state",
        },
        namespace,
        standalone=True,
    )
    manager_base.update_status = lambda manager, room, value: events.append(
        ("status", value)
    )
    namespace["MooncakeKVManager"] = manager_base
    common = load_methods(
        "disaggregation/common/conn.py", "CommonKVReceiver", {"abort"}, namespace
    )
    common.clear = lambda receiver: events.append(("clear", receiver.bootstrap_room))
    common._send_abort_notification = lambda receiver: events.append(
        ("abort", receiver.bootstrap_room)
    )
    namespace["MooncakeKVReceiver"] = common
    manager_cls = load_methods(
        "disaggregation/ascend/conn.py",
        "AscendKVManager",
        {"update_status"},
        namespace,
    )
    receiver_cls = load_methods(
        "disaggregation/ascend/conn.py",
        "AscendKVReceiver",
        {"abort", "clear", "_send_abort_notification"},
        namespace,
    )
    manager = manager_cls()
    manager.mempool_control = mempool_control
    manager.sparse_pd_decode_staging = None
    manager._deferred_abort_ack_tracker = {}
    manager.record_failure = lambda room, reason: None
    receiver = receiver_cls()
    receiver.kv_mgr = manager
    receiver.bootstrap_room = room
    receiver.abort_notified = False
    receiver.bootstrap_infos = [object() for _ in range(senders)]
    return manager, receiver


class TestNativeRelease(unittest.TestCase):
    """Keep ordinary staging cleanup separate from formal mempool ACK retention."""

    def setUp(self):
        """Wire real abort/update_status/clear across a fake native boundary."""
        self.events = []
        self.rooms = {7}
        self.manager, self.receiver = native_receiver(7, self.events)
        staging_cls = load_methods(
            "disaggregation/ascend/sparse_pd.py",
            "SparsePDDecodeStagingPool",
            {"offload_room_to_host"},
            {},
        )
        staging = staging_cls()
        staging.has_room = lambda room: room in self.rooms
        staging.release_room = lambda room: self.rooms.discard(room)
        staging.get_transfer_metadata = lambda room: SimpleNamespace(
            room=room, slot_id=0, req_pool_idx=1, token_count=2
        )
        staging.manager = SimpleNamespace(
            offload_pd_decode_staging_to_host=lambda **kwargs: None
        )
        self.manager.sparse_pd_decode_staging = staging

    def test_formal_abort_defers_native_clear(self):
        """Formal mode has no staging; service owns native destination release."""
        manager, receiver = native_receiver(7, self.events, mempool_control=object())
        receiver.abort()
        self.assertIsNone(manager.sparse_pd_decode_staging)
        self.assertEqual(self.events, [("status", "failed"), ("abort", 7)])
        self.assertFalse(manager.is_abort_release_safe(7, 1))

    def test_ordinary_abort_releases_staging(self):
        """Ordinary sparse PD preserves its immediate staging cleanup."""
        self.receiver.abort()
        self.assertNotIn(7, self.rooms)
        self.assertEqual(self.events, [("status", "failed"), ("abort", 7)])

    def test_ordinary_offload_error_releases_staging(self):
        """A failed host offload cannot leak an ordinary staging slot."""

        def fail_offload(*args, **kwargs):
            """Model a device-copy failure after the transfer status arrived."""
            raise RuntimeError("device failure")

        self.manager.sparse_pd_decode_staging.manager.offload_pd_decode_staging_to_host = fail_offload
        self.manager.update_status(7, "success")
        self.assertNotIn(7, self.rooms)
        self.assertEqual(self.events, [("status", "failed")])

    def test_successful_staging_commit_releases_the_room(self):
        """Successful local offload still frees scarce native staging promptly."""
        self.manager.update_status(7, "success")
        self.assertNotIn(7, self.rooms)
        self.assertEqual(self.events, [("status", "success")])

    def test_poll_failure_arms_ack_tracking_only_once(self):
        """Timeout polling can send ABORT before the service observes failure."""
        manager, receiver = native_receiver(7, self.events, mempool_control=object())
        receiver._send_abort_notification()
        manager.note_abort_ack(7, 0)
        receiver._send_abort_notification()
        receiver.abort()
        self.assertTrue(manager.is_abort_release_safe(7, 1))
        self.assertNotIn(("clear", 7), self.events)

    def test_scheduler_preserves_fake_and_grammar_abort(self):
        """Only an admitted real request delegates its resource lifetime to service."""
        events = []
        managed = SimpleNamespace(rid="real", finished=lambda: False)
        fake = SimpleNamespace(rid="fake", finished=lambda: False)
        namespace = dict(
            DisaggregationMode=SimpleNamespace(PREFILL="P", DECODE="D"),
            FINISH_ABORT=lambda: "aborted",
            logger=logging.getLogger(__name__),
        )
        cls = load_methods(
            "managers/scheduler.py",
            "Scheduler",
            {"abort_request"},
            namespace,
            standalone=True,
        )
        scheduler = cls()
        scheduler.mempool_service = SimpleNamespace(
            abort_matching=lambda *args, **kwargs: events.append("managed"),
            tracks=lambda req: req is managed,
        )
        scheduler.chunked_req = None
        scheduler.mm_receiver = None
        scheduler.waiting_queue = [managed]
        scheduler.dllm_config = None
        scheduler.grammar_manager = SimpleNamespace(
            abort_requests=lambda req: events.append("grammar")
        )
        scheduler.disaggregation_mode = "P"
        scheduler.disagg_prefill_bootstrap_queue = SimpleNamespace(queue=[])
        scheduler.disagg_prefill_inflight_queue = []
        scheduler.collect_inflight_reqs = lambda: [managed, fake]
        scheduler.abort_request(
            SimpleNamespace(rid="", abort_all=True, abort_message=None)
        )
        self.assertEqual(events, ["managed", "grammar"])
        self.assertEqual(fake.to_finish, "aborted")
        self.assertFalse(hasattr(managed, "to_finish"))
        self.assertEqual(scheduler.waiting_queue, [managed])

    def test_rejected_pd_intake_never_enters_mempool(self):
        """Native validation finishes before either queue tracks a request."""
        for role, name in (
            ("prefill", "PrefillBootstrapQueue"),
            ("decode", "DecodePreallocQueue"),
        ):
            events = []
            namespace = dict(is_unadmitted_reject=lambda req: req.rejected)
            cls = load_methods(
                f"disaggregation/{role}.py", name, {"add"}, namespace, standalone=True
            )
            queue = cls()
            queue.scheduler = SimpleNamespace(
                retire_unadmitted_request=lambda req: events.append("rejected"),
                mempool_service=SimpleNamespace(
                    track=lambda req: events.append("tracked")
                ),
            )
            queue.create_sender = lambda *args: False
            queue._check_if_req_exceed_kv_capacity = lambda req: True
            for rejected in (True, False):
                req = SimpleNamespace(rejected=rejected)
                if role == "prefill":
                    queue.add(req, 1)
                else:
                    queue.add(req)
            self.assertEqual(events, ["rejected"])
