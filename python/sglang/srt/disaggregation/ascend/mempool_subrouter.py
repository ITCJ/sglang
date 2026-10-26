"""Scheduler-owned SR shards and the rank-zero Main Router connection.

No transport callback performs a collective, allocator mutation or device
operation. Each idle/busy scheduler tick uses the same two all-gathers.
"""

from __future__ import annotations

import copy
import logging
import time
from collections import deque
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sglang.srt.hardware_backend.npu.mempool.prefix import PrefixStore

from .mempool_control import MempoolPDControl
from .mempool_native_reservation import NativeReservations
from .mempool_prefill_restore import PrefillRestore
from .mempool_protocol import MempoolMessage, RequestIdentity
from .mempool_router_protocol import native_message, native_wire
from .mempool_tick import RequestObservation

logger = logging.getLogger(__name__)


class RouterShard:
    """Translate opaque Main operations into the single local control owner."""

    def __init__(
        self,
        control: MempoolPDControl,
        *,
        native: NativeReservations | None,
        effect: Callable[[str, int], None],
        reply_to: str,
        page_size: int,
        bootstrap: tuple[str, int] | None = None,
        prefix: PrefixStore | None = None,
        restore: PrefillRestore | None = None,
    ) -> None:
        self.control, self.native, self.effect, self.reply_to = (
            control,
            native,
            effect,
            reply_to,
        )
        self.request: dict[str, Any] | None = None
        self.dispatch: dict[str, Any] | None = None
        self.identity: RequestIdentity | None = None
        self.req: Any = None
        self.sent_events: set[str] = set()
        self.bootstrap = bootstrap
        self.prefix = prefix
        self.restore = restore
        self.fill_complete = False
        if type(page_size) is not int or page_size <= 0:
            raise ValueError("SR requires a positive token page_size")
        if any(
            n % page_size
            for n in (
                control.local.layout.prompt_tokens,
                control.local.layout.decode_tokens,
            )
        ):
            raise ValueError("SR alive capacities must align to page_size")
        if native is not None and native.allocator.page_size != page_size:
            raise ValueError("SR native and DRAM page_size differ")
        self.page_size = page_size

    def track(self, req: Any) -> None:
        """Reject direct-backend bypass, fake warmups and dispatch mismatches."""
        request = self.request
        if request is None or self.dispatch is None or self.req is not None:
            raise ValueError("HTTP Req has no unused Router dispatch")
        sampling = req.sampling_params
        if (
            req.rid != request["rid"]
            or req.bootstrap_room != request["room"]
            or req.bootstrap_host == "2.2.2.2"
            or list(req.origin_input_ids) != request["input_ids"]
            or sampling.max_new_tokens != request["max_new_tokens"]
            or sampling.top_k != 1
            or bool(sampling.ignore_eos) != request["ignore_eos"]
            or (
                self.bootstrap is not None
                and (req.bootstrap_host, req.bootstrap_port) != self.bootstrap
            )
        ):
            raise ValueError("HTTP Req differs from the confirmed Router dispatch")
        # Compare normalized native sampling, including options which Main
        # never forwards. A direct backend request cannot reuse a dispatch with
        # different penalties, stopping rules or a grammar.
        defaults = dict(
            temperature=1.0,
            top_p=1.0,
            min_p=0.0,
            frequency_penalty=0.0,
            presence_penalty=0.0,
            repetition_penalty=1.0,
            min_new_tokens=0,
            n=1,
        )
        if any(getattr(sampling, k, v) != v for k, v in defaults.items()) or any(
            getattr(sampling, field, None)
            for field in (
                "stop_strs",
                "stop_token_ids",
                "stop_regex_strs",
                "beam_width",
                "json_schema",
                "regex",
                "ebnf",
                "structural_tag",
                "logit_bias",
                "custom_params",
            )
        ):
            raise ValueError("HTTP Req sampling differs from Router dispatch")
        for field in (
            "return_logprob",
            "lora_id",
            "multimodal_inputs",
            "extra_key",
            "cache_salt",
            "input_embeds",
            "positional_embed_overrides",
            "session",
            "custom_logit_processor",
        ):
            if getattr(req, field, None):
                raise ValueError(f"Router mode does not support {field}")
        self.req = req

    def state(self) -> tuple[Any, ...] | None:
        if self.identity is None:
            return None
        state = self.control.get_request(self.identity)
        if state is None:
            raise RuntimeError("SR lost its control allocation")
        return (
            state.phase,
            state.identity.room,
            state.identity.attempt,
            state.p_slot.slot if state.p_slot else None,
            state.d_slot.slot,
            state.owns_slot,
            state.prompt_written,
            state.transfer_ready,
        )

    def execute(
        self, command: dict[str, Any], slot: int, prefix_pages: tuple[int, ...] = ()
    ) -> dict[str, Any] | None:
        role, rank = self.control.local.role, self.control.local.tp_rank
        kind = command["kind"]
        result: dict[str, Any] = dict(rank=rank)
        if kind == "alloc":
            if self.request is not None:
                raise ValueError("SR already has an active request")
            request = command["request"]
            if request["page_size"] != self.page_size:
                raise ValueError("Router and SR page_size differ")
            if any(request[k] != command[k] for k in ("rid", "room", "attempt")):
                raise ValueError("allocation envelope differs from request")
            self.request = copy.deepcopy(request)
            if role == "decode":
                if self.native is None:
                    raise RuntimeError("D native reservation allocator is missing")
                result["native"] = self.native.reserve(
                    command["room"], request["prompt_tokens"], request["max_new_tokens"]
                )
                message = self.control.acquire_decode(
                    command["room"],
                    command["attempt"],
                    slot,
                    request["prompt_tokens"],
                    request["max_new_tokens"],
                    self.reply_to,
                )
            else:
                message = self.control.router_acquire_prefill(
                    self._binding(command), slot
                )
            self.identity = message.request
            if self.prefix is not None:
                self.prefix.begin_request(
                    rid=request["rid"], slot=slot, tokens=request["prompt_tokens"]
                )
            if self.restore is not None:
                self.restore.begin_request(request, slot=slot)
            self.fill_complete = request.get("hit_len", 0) == 0
            result["message"] = native_wire(message)
            result["slot"] = slot
            capacity = (
                self.control.local.layout.prompt_tokens
                if role == "prefill"
                else self.control.local.layout.decode_tokens
            )
            result["pages"] = [
                dict(
                    pool="alive_main",
                    region=self.control.local.session,
                    replica=rank,
                    logical_page=slot * (capacity // self.page_size) + i,
                    page_size=self.page_size,
                )
                for i in range(capacity // self.page_size)
            ]
        else:
            if (
                self.request is None
                or self.identity is None
                or any(
                    command[k] != self.request[k] for k in ("rid", "room", "attempt")
                )
            ):
                raise ValueError("operation has no matching SR allocation")
            if kind == "dispatch":
                if self.dispatch is not None or command["request"] != self.request:
                    raise ValueError("dispatch conflicts with allocation")
                reply = self.control.router_bind(self._binding(command))
                if reply is not None:
                    result["message"] = native_wire(reply)
                self.dispatch = command
            elif kind == "ready":
                self.control.router_ready(self._binding(command))
            elif kind == "admit":
                receipt = self._admit(command=command, prefix_pages=prefix_pages)
                if receipt is None:
                    return None
                result.update(receipt)
            elif kind in ("load_back", "fill"):
                receipt = self._prefix_read(command)
                if receipt is None:
                    return None
                result.update(receipt)
            elif kind == "free":
                if self.prefix is not None:
                    self.prefix.assert_source_releasable(command["rid"])
                self.control.router_free(self.identity)
                if self.prefix is not None:
                    self.prefix.release_source(command["rid"])
                if self.restore is not None:
                    self.restore.finish_request()
                result.update(freed=True, native_freed=True)
                self.request = self.dispatch = self.identity = self.req = None
                self.sent_events.clear()
            else:
                raise ValueError(f"unsupported Router command {kind}")
        logger.info(
            "mempool sr role=%s rank=%s event=%s rid=%s room=%s attempt=%s op_id=%s free=%s",
            role,
            rank,
            kind,
            command["rid"],
            command["room"],
            command["attempt"],
            command["op_id"],
            len(self.control.available_slots()),
        )
        return result

    def _prefix_read(self, command: dict[str, Any]) -> dict[str, Any] | None:
        request, restore, prefix = self.request, self.restore, self.prefix
        rank = self.control.local.tp_rank
        state = self.control.get_request(self.identity) if self.identity else None
        if (
            self.control.local.role != "prefill"
            or request is None
            or restore is None
            or prefix is None
            or state is None
            or state.p_slot is None
            or command["hit_len"] != request["hit_len"]
            or not request["hit_len"]
            or [r["rank"] for r in command["sources"]] != list(range(16))
        ):
            raise ValueError("prefix read lacks its bound request and replicas")
        source = command["sources"][rank]
        pages = source["pages"]
        if (
            source["slot"] != state.p_slot.slot
            or len(pages) * self.page_size != request["hit_len"]
        ):
            raise ValueError("prefix read target differs from this request's alive")
        for page in pages:
            if page != dict(
                pool="prefix",
                region=self.control.local.session,
                replica=rank,
                logical_page=page["logical_page"],
                page_size=self.page_size,
            ):
                raise ValueError("prefix read source differs from local mapping")
        source_pages = [p["logical_page"] for p in pages]
        if command["kind"] == "load_back":
            loaded: dict[str, Any] | None = restore.load_back(
                op_id=command["op_id"], source_pages=source_pages
            )
            return loaded
        if not restore.ready:
            raise RuntimeError("fill cannot precede completed load_back")
        # Acceptance forces the native-free-before-fill boundary on every rank.
        # Main already protects these prefix sources while submission is delayed.
        if restore.verify_boundaries and (
            not restore.native_freed or "native_freed" not in self.sent_events
        ):
            return None
        receipt: dict[str, Any] | None = prefix.fill(
            rid=request["rid"],
            op_id=command["op_id"],
            slot=state.p_slot.slot,
            source_pages=source_pages,
        )
        if receipt is not None:
            self.fill_complete = True
        return receipt

    def _admit(
        self, *, command: dict[str, Any], prefix_pages: tuple[int, ...]
    ) -> dict[str, Any] | None:
        """Copy only an identified P source whose native resources are gone."""
        assert self.identity is not None and self.request is not None
        role, rank = self.control.local.role, self.control.local.tp_rank
        state = self.control.get_request(self.identity)
        if (
            role != "prefill"
            or self.prefix is None
            or state is None
            or state.phase != "WAITING_DONE"
            or state.p_slot is None
            or "native_freed" not in self.sent_events
        ):
            raise ValueError("prefix admit requires completed P native cleanup")
        sources, pages = command["sources"], command["source_pages"]
        if [source["rank"] for source in sources] != list(range(16)):
            raise ValueError("admit source is missing TP ranks")
        capacity = self.control.local.layout.prompt_tokens // self.page_size
        expected = dict(
            rank=rank,
            slot=state.p_slot.slot,
            pages=[
                dict(
                    pool="alive_main",
                    region=self.control.local.session,
                    replica=rank,
                    logical_page=state.p_slot.slot * capacity + page,
                    page_size=self.page_size,
                )
                for page in pages
            ],
        )
        if sources[rank] != expected or len(command["hashes"]) != len(pages):
            raise ValueError("admit source differs from actual alive allocation")
        receipt = self.prefix.admit(
            rid=command["rid"],
            op_id=command["op_id"],
            slot=state.p_slot.slot,
            prompt_tokens=self.request["prompt_tokens"],
            source_pages=pages,
            target_pages=list(prefix_pages),
        )
        if receipt is None:
            return None
        result = dict(rank=rank, **receipt)
        result["pages"] = [
            dict(
                pool="prefix",
                region=self.control.local.session,
                replica=rank,
                logical_page=page,
                page_size=self.page_size,
            )
            for page in receipt["target_pages"]
        ]
        return result

    def _binding(self, command: dict[str, Any]) -> MempoolMessage:
        ranks = command["bindings"]
        if [r["rank"] for r in ranks] != list(range(16)):
            raise ValueError("binding must contain every TP rank")
        message = native_message(ranks[self.control.local.tp_rank]["message"])
        request = message.request
        if (
            request is None
            or request.room != command["room"]
            or request.attempt != command["attempt"]
        ):
            raise ValueError("native binding identity differs from Router operation")
        return message

    def advance(
        self,
        facts: Sequence[RequestObservation | None],
        *,
        restored: bool = True,
        filled: bool = True,
    ) -> list[dict[str, Any]]:
        """Advance only facts agreed by all ranks; receipts outlive native rows."""
        if self.identity is None or self.dispatch is None:
            return []
        state = self.control.get_request(self.identity)
        if state is None:
            raise RuntimeError("SR request disappeared")
        role, room = self.control.local.role, self.identity.room
        present = all(f is not None for f in facts)

        def all_fact(name: str) -> bool:
            return present and all(bool(getattr(f, name)) for f in facts)

        def event(kind: str, **data: Any) -> dict[str, Any]:
            self.sent_events.add(kind)
            return dict(kind=kind, rank=self.control.local.tp_rank, **data)

        events = []
        if any(f and f.cancel for f in facts):
            raise RuntimeError("Router request cancelled; fail-stop without freeing")
        if role == "prefill":
            if state.phase == "BOUND" and present and restored:
                self.control.start_prefill(self.identity)
            elif state.phase == "PREFILLING" and all_fact("prompt_ready") and filled:
                self.control.finish_prefill_writes(self.identity)
                ready = self.control.publish_kv_ready(self.identity, state.prompt_limit)
                events.append(event("ready_tokens", message=native_wire(ready)))
            if all_fact("native_release"):
                self.effect("native_release", room)
            if all_fact("native_freed") and "native_freed" not in self.sent_events:
                events.append(event("native_freed", native_freed=True))
        elif state.phase == "WAITING_READY":
            if all_fact("transfer_ready"):
                if not state.transfer_ready:
                    self.control.transfer_succeeded(self.identity)
                if state.prompt_written == state.prompt_limit:
                    self.control.start_decode(self.identity)
        elif state.phase == "DECODING" and all_fact("release"):
            if all_fact("drained") and all_fact("writes_done"):
                self.effect("native_release", room)
                self.control.begin_drain(self.identity)
                self.control.finish_drain(self.identity)
            elif not all_fact("host_drained"):
                self.effect("drain", room)
        elif state.phase == "ROUTER_FINISHED" and all_fact("native_freed"):
            if "finished" not in self.sent_events:
                events.append(
                    event("finished", native_freed=True, drained=all_fact("drained"))
                )
        return events


class MempoolSubrouter:
    """Agree commands and effects before rank zero acknowledges the full side."""

    def __init__(
        self,
        shard: RouterShard,
        *,
        run_id: str,
        gather: Callable[[Any], Sequence[Any]],
        receive: Callable[[], list[dict[str, Any]]],
        send: Callable[[dict[str, Any]], None],
        registration: Callable[[], dict[str, Any]],
        timeout: float = 600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.shard, self.run_id, self.gather = shard, run_id, gather
        self.receive, self.send, self.registration = receive, send, registration
        self.timeout, self.clock = timeout, clock
        self.last_main = clock()
        self.last_sent = float("-inf")
        self.registered = False
        self.pending: deque[dict[str, Any]] = deque()
        self.completed: dict[int, tuple[dict[str, Any], dict[str, Any]]] = {}
        self.last_op = 0
        self._admit_pages: tuple[int, ...] | None = None
        self.fault: str | None = None

    def track(self, req: Any) -> None:
        self.shard.track(req)

    def advance(
        self, facts: Sequence[RequestObservation], fault: str | None = None
    ) -> None:
        control = self.shard.control
        leader = control.local.tp_rank == 0
        incoming: list[dict[str, Any]] = []
        registration = None
        state = None
        free: frozenset[int] = frozenset()
        prefix_free: tuple[int, ...] = ()
        try:
            if leader:
                incoming = self.receive()
                if incoming:
                    self.last_main = self.clock()
                if not self.registered:
                    self.last_main = self.clock()
                if self.clock() - self.last_main > self.timeout:
                    raise TimeoutError("Main Router heartbeat expired")
            if not self.registered and control.peer is not None:
                registration = self.registration()
            free = control.available_slots()
            prefix_free = self.shard.prefix.free_pages if self.shard.prefix else ()
            state = self.shard.state()
        except Exception as exc:
            fault = str(exc)
        observations = self.gather(
            dict(
                incoming=incoming,
                facts=list(facts),
                free=free,
                prefix_free=prefix_free,
                state=state,
                registration=registration,
                restored=self.shard.restore is None or self.shard.restore.ready,
                filled=self.shard.restore is None or self.shard.fill_complete,
                fault=fault or self.fault or control.snapshot().protocol_fault,
            )
        )
        results: dict[str, Any] = dict(
            error=None, ack=None, events=[], registration=None
        )
        command = None
        dispatch = self.shard.dispatch
        try:
            if len(observations) != 16 or any(o["fault"] for o in observations):
                raise RuntimeError(
                    next(
                        (o["fault"] for o in observations if o["fault"]),
                        "incomplete TP group",
                    )
                )
            if any(
                o["free"] != observations[0]["free"]
                or o["prefix_free"] != observations[0]["prefix_free"]
                or o["state"] != observations[0]["state"]
                for o in observations
            ):
                raise RuntimeError("SR ownership diverged across TP ranks")
            if not self.registered and all(
                o["registration"] is not None for o in observations
            ):
                results["registration"] = [o["registration"] for o in observations]
                self.registered = True
            for message in observations[0]["incoming"]:
                if (
                    message["run_id"] != self.run_id
                    or message["role"] != control.local.role
                ):
                    raise ValueError("Router command belongs to another startup")
                if message["kind"] == "heartbeat":
                    continue
                if message["kind"] == "error":
                    raise RuntimeError(message["reason"])
                self.pending.append(message)
            room = self.shard.request["room"] if self.shard.request else None
            common_facts = [
                next((f for f in o["facts"] if f.room == room), None)
                for o in observations
            ]
            if self.pending:
                command = self.pending[0]
                if command["op_id"] in self.completed:
                    old, result = self.completed[command["op_id"]]
                    if old != command:
                        raise ValueError("conflicting duplicate Router command")
                    results["ack"] = result
                elif command["op_id"] <= self.last_op:
                    raise ValueError("stale Router operation")
                elif command["kind"] != "free" or all(
                    f is not None and f.native_freed for f in common_facts
                ):
                    if command["kind"] == "admit" and self._admit_pages is None:
                        count = len(command["source_pages"])
                        self._admit_pages = observations[0]["prefix_free"][:count]
                        if len(self._admit_pages) != count:
                            raise RuntimeError(
                                "prefix page reservation exceeds local capacity"
                            )
                    execution = self.shard.execute(
                        command,
                        min(observations[0]["free"], default=-1),
                        self._admit_pages or (),
                    )
                    results["ack"] = execution
            results["events"] = self.shard.advance(
                common_facts,
                restored=all(o["restored"] for o in observations),
                filled=all(o["filled"] for o in observations),
            )
        except Exception as exc:
            results["error"] = str(exc)
        committed = self.gather(results)
        errors = [r["error"] for r in committed if r["error"]]
        if errors:
            self.fault = errors[0]
            control.fail_protocol(self.fault)
            if leader:
                self.send(
                    dict(
                        run_id=self.run_id,
                        role=control.local.role,
                        kind="error",
                        reason=self.fault,
                    )
                )
            raise RuntimeError(f"SR fail-stop: {self.fault}")
        acknowledged = command is not None and all(
            r["ack"] is not None for r in committed
        )
        if acknowledged:
            assert command is not None
            if command["op_id"] not in self.completed:
                self.completed[command["op_id"]] = copy.deepcopy(
                    (command, results["ack"])
                )
            if len(self.completed) > 64:
                del self.completed[next(iter(self.completed))]
            self.last_op = max(self.last_op, command["op_id"])
            self.pending.popleft()
            self._admit_pages = None
        if not leader:
            return
        envelope = dict(run_id=self.run_id, role=control.local.role)
        if results["registration"] is not None:
            self.send(dict(envelope, kind="register", ranks=results["registration"]))
        if acknowledged and command is not None:
            self.send(
                {
                    **command,
                    "kind": command["kind"] + "_ack",
                    "ranks": [r["ack"] for r in committed],
                }
            )
        for index, event in enumerate(results["events"]):
            if dispatch is None or any(
                len(r["events"]) != len(results["events"]) for r in committed
            ):
                raise RuntimeError("SR completion event agreement failed")
            self.send(
                {
                    **dispatch,
                    "kind": event["kind"],
                    "ranks": [r["events"][index] for r in committed],
                }
            )
        if self.clock() - self.last_sent >= 1:
            if self.registered:
                self.send(dict(envelope, kind="heartbeat"))
            self.last_sent = self.clock()
