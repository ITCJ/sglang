"""Serial Main Router: quota, opaque locations, explicit completion and free.

Main never owns a device address or selects a worker's physical page number.
SR returns signed rank-pair bindings and opaque per-replica page descriptions.
"""

from __future__ import annotations

import copy
import logging
import secrets
import time
from collections import deque
from typing import Any, Callable

import msgspec

from .mempool_router_protocol import PRODUCERS, ROLES, RouterConfig, page_hashes

logger = logging.getLogger(__name__)


class RequestRejected(ValueError):
    def __init__(self, reason: str, status: int = 400) -> None:
        super().__init__(reason)
        self.status = status


class Quota(msgspec.Struct):
    total: int
    outstanding: int = 0

    def change(self, amount: int) -> None:
        if not 0 <= self.outstanding + amount <= self.total:
            raise RuntimeError("Router resource ledger invariant failed")
        self.outstanding += amount

    def snapshot(self) -> dict[str, int]:
        return dict(
            total=self.total,
            outstanding=self.outstanding,
            free=self.total - self.outstanding,
        )


class MainRouter:
    """Synchronous service boundary; the HTTP/ZMQ adapter only moves messages."""

    def __init__(
        self,
        config: RouterConfig,
        run_id: str,
        *,
        clock: Callable[[], float] = time.monotonic,
        hasher: Callable[[list[int], int], list[str]] = page_hashes,
    ) -> None:
        self.config, self.run_id, self.clock, self.hasher = (
            config,
            run_id,
            clock,
            hasher,
        )
        self.registrations: dict[str, dict[str, Any]] = {}
        self.ledger: dict[str, Quota] = {}
        self.directory: dict[str, dict[str, Any]] = {}
        self.active: dict[str, Any] | None = None
        self.operations: dict[int, dict[str, Any]] = {}
        self.outbox: deque[dict[str, Any]] = deque()
        self.launches: deque[dict[str, Any]] = deque()
        self.history: deque[dict[str, Any]] = deque(maxlen=64)
        self.fault: str | None = None
        self.next_op = 0
        self.next_request = 0
        self.started = self.clock()
        self.last_seen: dict[str, float] = {}

    @property
    def ready(self) -> bool:
        return len(self.registrations) == 2 and self.fault is None

    def register(self, registration: dict[str, Any]) -> None:
        role = registration["role"]
        if role not in ROLES or registration["run_id"] != self.run_id:
            raise ValueError("wrong Router run or role")
        if role in self.registrations:
            if self.registrations[role] != registration:
                raise ValueError("SR registration changed during a run")
            return
        ranks = registration["ranks"]
        if [r["rank"] for r in ranks] != list(range(16)):
            raise ValueError("registration requires the complete ordered TP16 group")
        expected = dict(
            layers=78,
            dim=576,
            indexer_layers=list(PRODUCERS),
            index_dim=128,
            dtype="bfloat16",
            tp_size=16,
            dp_size=1,
            pp_size=1,
            slots=16,
            prompt_capacity=self.config.prompt_capacity,
            decode_capacity=self.config.decode_capacity,
            page_size=self.config.page_size,
            hbm_page_size=self.config.page_size,
            replicas=self.config.replicas(),
            graph=role == "decode",
        )
        if self.config.prefix_pages:
            expected.update(
                prefix_pages=self.config.prefix_pages if role == "prefill" else 0,
                local_dram_bytes=(13 if role == "prefill" else 1) * 2**30,
                verify_prefix=self.config.verify_prefix,
            )
        if self.config.match_prefix:
            expected.update(
                match_prefix=True,
                force_noncontiguous_restore=self.config.force_noncontiguous_restore,
                delay_prefix_fill=self.config.delay_prefix_fill,
            )
        for rank in ranks:
            if any(rank.get(k) != v for k, v in expected.items()):
                raise ValueError("SR registered incompatible ticket01 capabilities")
            if (
                rank["prompt_max"] < self.config.prompt_capacity
                or rank["hbm_pages"]
                < (self.config.prompt_capacity + self.config.decode_capacity)
                // self.config.page_size
                or rank["request_rows"] < 1
            ):
                raise ValueError(
                    "SR native capacities do not cover the configured limits"
                )
            if (
                not rank["session"]
                or not rank["peer_session"]
                or rank["vocab_size"] <= 0
            ):
                raise ValueError("SR is not control-ready")
            if rank["numa_node"] not in self.config.numa_nodes:
                raise ValueError("SR NUMA assignment differs from the deployment")
            if (
                rank["model"] != ranks[0]["model"]
                or rank["vocab_size"] != ranks[0]["vocab_size"]
            ):
                raise ValueError("TP model identity differs")
        other_role = "decode" if role == "prefill" else "prefill"
        if other_role in self.registrations:
            for rank, other in zip(ranks, self.registrations[other_role]["ranks"]):
                if (
                    rank["session"] != other["peer_session"]
                    or rank["peer_session"] != other["session"]
                    or rank["model"] != other["model"]
                    or rank["vocab_size"] != other["vocab_size"]
                ):
                    raise ValueError("P/D model or paired startup sessions differ")
        self.registrations[role] = copy.deepcopy(registration)
        self.last_seen[role] = self.clock()
        if self.ready:
            d = self.registrations["decode"]["ranks"]
            self.ledger = dict(
                p_alive=Quota(
                    16 * self.config.prompt_capacity // self.config.page_size
                ),
                d_alive=Quota(
                    16 * self.config.decode_capacity // self.config.page_size
                ),
                d_hbm=Quota(min(r["hbm_pages"] for r in d)),
                d_rows=Quota(min(r["request_rows"] for r in d)),
            )
            if self.config.prefix_pages:
                self.ledger["p_prefix"] = Quota(self.config.prefix_pages)

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.ready:
            raise RequestRejected(
                self.fault or "all-rank registration is incomplete", 503
            )
        if self.active is not None:
            raise RequestRejected(
                "one request is still active or awaiting free_ack", 429
            )
        if not isinstance(payload, dict) or set(payload) - {
            "input_ids",
            "sampling_params",
            "stream",
        }:
            raise RequestRejected(
                "only token IDs, greedy sampling and stream=false are supported"
            )
        tokens, sampling = payload.get("input_ids"), payload.get("sampling_params")
        page_size = self.config.page_size
        vocab = self.registrations["prefill"]["ranks"][0]["vocab_size"]
        if (
            not isinstance(tokens, list)
            or not page_size <= len(tokens) <= self.config.prompt_capacity
            or len(tokens) % page_size
            or any(type(t) is not int or not 0 <= t < vocab for t in tokens)
        ):
            raise RequestRejected(
                f"prompt must contain valid token IDs, aligned to page_size={page_size} "
                f"and within {self.config.prompt_capacity}"
            )
        if not isinstance(sampling, dict) or set(sampling) - {
            "temperature",
            "top_k",
            "top_p",
            "max_new_tokens",
            "ignore_eos",
        }:
            raise RequestRejected("unsupported sampling options")
        new = sampling.get("max_new_tokens")
        if (
            type(new) is not int
            or not 1 <= new <= self.config.decode_capacity
            or sampling.get("temperature") != 0
            or sampling.get("top_k", 1) != 1
            or sampling.get("top_p", 1) != 1
            or type(sampling.get("ignore_eos", False)) is not bool
            or payload.get("stream", False) is not False
        ):
            raise RequestRejected(
                "requires greedy, non-streaming generation within "
                f"{self.config.decode_capacity} tokens"
            )
        needed = dict(
            p_alive=self.config.prompt_capacity // page_size,
            d_alive=self.config.decode_capacity // page_size,
            d_hbm=(len(tokens) + new + page_size - 1) // page_size,
            d_rows=1,
        )
        if any(self.ledger[k].snapshot()["free"] < n for k, n in needed.items()):
            raise RequestRejected(
                "insufficient capacity for the complete reservation", 429
            )
        hashes = self.hasher(tokens, page_size)
        if len(hashes) != len(tokens) // page_size:
            raise RuntimeError("storage hash returned the wrong logical page count")
        self.next_request += 1
        request = dict(
            rid=f"{self.run_id}-{self.next_request}",
            room=secrets.randbelow(2**53 - 1) + 1,
            attempt=secrets.token_hex(16),
            input_ids=tokens.copy(),
            prompt_tokens=len(tokens),
            max_new_tokens=new,
            ignore_eos=sampling.get("ignore_eos", False),
            hit_len=self._match_prefix(hashes),
            page_size=page_size,
        )
        for key, n in needed.items():
            self.ledger[key].change(n)
        self.active = dict(
            request=request,
            phase="alloc",
            needed=needed,
            hashes=hashes,
            allocations={},
            dispatch={},
            events={},
            free_acks=[],
            result=None,
            started=self.clock(),
            trace=[],
            admit=None,
            admit_complete=not bool(self.config.prefix_pages),
            save_planned=False,
            reads={},
        )
        parent = None
        for hash_value in hashes:
            if hash_value not in self.directory:
                if parent is not None:
                    self.directory[parent]["child_count"] += 1
                self.directory[hash_value] = dict(
                    parent_hash=parent, child_count=0, prefix=None
                )
            node = self.directory[hash_value]
            if node["parent_hash"] != parent:
                raise RuntimeError("page hash has a conflicting parent")
            node.update(
                state="writing",
                owner=request["rid"],
                lock_ref=1,
                source_ops=[],
                locations=[],
            )
            parent = hash_value
        # D first supplies signed local leases; P then chooses its own minimum
        # slot and signs the complete pair. Main never chooses either slot.
        self._command("decode", "alloc", request=request)
        return request

    def _match_prefix(self, hashes: list[str]) -> int:
        """Use only the continuous retained path, leaving the final page to compute."""
        count = 0
        if self.config.match_prefix:
            for value in hashes[:-1]:
                prefix = self.directory.get(value, {}).get("prefix")
                if prefix is None or prefix["state"] != "ready":
                    break
                if len(prefix["locations"]) != 16:
                    raise RuntimeError("ready prefix lacks required replicas")
                count += 1
        return count * self.config.page_size

    def _plan_read(self, kind: str) -> None:
        active = self.active
        assert active is not None
        hit = active["request"]["hit_len"]
        hashes = active["hashes"][: hit // self.config.page_size]
        op_id = self.next_op + 1
        for value in hashes:
            prefix = self.directory[value]["prefix"]
            if prefix["state"] != "ready":
                raise RuntimeError("prefix source is no longer ready")
            prefix["lock_ops"].append(op_id)
        active["reads"][kind] = op_id
        active["trace"].append(dict(kind="prefix_protect", role="prefill", op_id=op_id))
        self._command(
            "prefill",
            kind,
            hit_len=hit,
            sources=[
                dict(
                    rank=rank["rank"],
                    slot=rank["slot"],
                    pages=[
                        self.directory[value]["prefix"]["locations"][rank["rank"]]
                        for value in hashes
                    ],
                )
                for rank in active["allocations"]["prefill"]
            ],
        )

    def _read_ack(self, command: dict[str, Any], ranks: list[dict[str, Any]]) -> None:
        active = self.active
        assert active is not None
        kind, hit = command["kind"], command["hit_len"]
        if (
            command["role"] != "prefill"
            or active["reads"].get(kind) != command["op_id"]
        ):
            raise ValueError("prefix completion has no planned operation")
        for rank, receipt in enumerate(ranks):
            source = command["sources"][rank]
            if (
                receipt.get("readable") is not True
                or receipt.get("tokens") != hit
                or receipt.get("slot") != source["slot"]
                or receipt.get("source_pages")
                != [p["logical_page"] for p in source["pages"]]
                or receipt.get("main_layers") != 78
                or receipt.get("index_layers") != len(PRODUCERS)
            ):
                raise ValueError(
                    "prefix completion differs from source/target contract"
                )
            if kind == "load_back":
                indices = receipt.get("prefix_indices", [])
                size = self.config.page_size
                if (
                    len(indices) != hit
                    or len(set(indices)) != hit
                    or any(type(i) is not int or i < size for i in indices)
                    or any(indices[i] % size for i in range(0, hit, size))
                    or any(
                        indices[i] != indices[i // size * size] + i % size
                        for i in range(hit)
                    )
                ):
                    raise ValueError("load_back lacks complete native token indices")
        if kind == "load_back":
            # Protect the next read before releasing this operation's source hold.
            self._plan_read("fill")
            active["phase"] = "running"
            self.launches.append(active["request"])
        for value in active["hashes"][: hit // self.config.page_size]:
            self.directory[value]["prefix"]["lock_ops"].remove(command["op_id"])
        active["trace"].append(
            dict(kind="prefix_unprotect", role="prefill", op_id=command["op_id"])
        )

    def _command(self, role: str, kind: str, **data: Any) -> None:
        if self.fault or self.active is None:
            raise RuntimeError("cannot issue operations after fail-stop")
        self.next_op += 1
        request = self.active["request"]
        command = dict(
            run_id=self.run_id,
            role=role,
            kind=kind,
            op_id=self.next_op,
            rid=request["rid"],
            room=request["room"],
            attempt=request["attempt"],
            **data,
        )
        self.operations[self.next_op] = dict(
            command=command, sent=self.clock(), ack=None
        )
        self.outbox.append(command)
        self.active["trace"].append(dict(kind=kind, role=role, op_id=self.next_op))

    def status(self) -> dict[str, Any]:
        active = None
        if self.active is not None:
            active = {
                k: self.active[k]
                for k in (
                    "phase",
                    "needed",
                    "allocations",
                    "dispatch",
                    "free_acks",
                    "trace",
                    "admit",
                )
            }
            active["rid"] = self.active["request"]["rid"]
            active["room"] = self.active["request"]["room"]
        return copy.deepcopy(
            dict(
                ready=self.ready,
                fault=self.fault,
                active=active,
                ledger={k: v.snapshot() for k, v in self.ledger.items()},
                directory=self.directory,
                registrations=self.registrations,
                completed=list(self.history),
            )
        )

    def receive(self, message: dict[str, Any]) -> None:
        """Validate all-rank evidence before publish, dispatch or quota return."""
        if self.fault is not None:
            return
        try:
            self._receive(message)
        except Exception as exc:
            self.fail(str(exc))
            raise

    def _receive(self, message: dict[str, Any]) -> None:
        role, kind = message["role"], message["kind"]
        if message["run_id"] != self.run_id or role not in ROLES:
            raise ValueError("message belongs to another run")
        if kind == "register":
            self.register({k: message[k] for k in ("run_id", "role", "ranks")})
            return
        if role not in self.registrations:
            raise ValueError("unregistered SR message")
        self.last_seen[role] = self.clock()
        if kind == "heartbeat":
            return
        if kind == "error":
            raise RuntimeError(f"SR {role}: {message.get('reason')}")
        operation = self.operations.get(message["op_id"])
        if operation is None:
            raise ValueError("unknown Router operation")
        command = operation["command"]
        if any(message[k] != command[k] for k in ("role", "rid", "room", "attempt")):
            raise ValueError("ack identity does not match dispatch")
        ranks = message["ranks"]
        if [r["rank"] for r in ranks] != list(range(16)):
            raise ValueError("ack is missing ranks or contains duplicates")
        if kind.endswith("_ack"):
            if kind != command["kind"] + "_ack":
                raise ValueError("ack kind does not match operation")
            if operation["ack"] is not None:
                if operation["ack"] != message:
                    raise ValueError("conflicting duplicate ack")
                return
        active = self.active
        if active is None or active["request"]["rid"] != message["rid"]:
            # Historical duplicate events are not a new completion.
            for prior in self.history:
                if (
                    prior["rid"] == message["rid"]
                    and prior["events"].get(f"{role}:{kind}") == message
                ):
                    return
            raise ValueError("event has no active request")
        key = f"{role}:{kind}"
        if not kind.endswith("_ack"):
            if command["kind"] != "dispatch":
                raise ValueError("completion does not refer to dispatch")
            previous = active["events"].get(key)
            if previous is not None:
                if previous != message:
                    raise ValueError("conflicting completion event")
                return
        active["trace"].append(dict(kind=kind, role=role, op_id=message["op_id"]))
        if kind == "alloc_ack":
            capacity = (
                self.config.prompt_capacity
                if role == "prefill"
                else self.config.decode_capacity
            )
            pages = capacity // self.config.page_size
            if len({r["slot"] for r in ranks}) != 1 or any(
                len(r["pages"]) != pages
                or any(p["page_size"] != self.config.page_size for p in r["pages"])
                for r in ranks
            ):
                raise ValueError("SR allocation differs across TP ranks")
            if role == "decode" and any(
                r["native"]["pages"] != active["needed"]["d_hbm"]
                or r["native"]["tokens"]
                != active["needed"]["d_hbm"] * self.config.page_size
                or r["native"]["row"] <= 0
                for r in ranks
            ):
                raise ValueError("D did not reserve the full native budget")
            active["allocations"][role] = ranks
            if role == "decode":
                self._command(
                    "prefill", "alloc", request=active["request"], bindings=ranks
                )
            else:
                for index, hash_value in enumerate(active["hashes"]):
                    self.directory[hash_value]["locations"] = [
                        r["pages"][index] for r in ranks
                    ]
                self._command(
                    "decode", "dispatch", request=active["request"], bindings=ranks
                )
        elif kind == "dispatch_ack":
            active["dispatch"][role] = message["op_id"]
            if role == "decode":
                self._command(
                    "prefill", "dispatch", request=active["request"], bindings=ranks
                )
            else:
                if active["request"]["hit_len"]:
                    active["phase"] = "restoring"
                    self._plan_read("load_back")
                else:
                    active["phase"] = "running"
                    self.launches.append(active["request"])
        elif kind in ("load_back_ack", "fill_ack"):
            self._read_ack(command, ranks)
        elif kind == "ready_tokens":
            if (
                role != "prefill"
                or active["phase"] != "running"
                or len(active["dispatch"]) != 2
            ):
                raise ValueError("prompt became ready before both dispatches")
            if active["request"]["hit_len"] and (
                "fill" not in active["reads"]
                or self.operations[active["reads"]["fill"]]["ack"] is None
            ):
                raise ValueError("prompt became ready before all fill replicas")
            for hash_value in active["hashes"]:
                self.directory[hash_value]["state"] = "ready"
            self._command("decode", "ready", bindings=ranks)
        elif kind == "ready_ack":
            active["phase"] = "decoding"
        elif kind == "native_freed":
            if role != "prefill" or not all(r["native_freed"] for r in ranks):
                raise ValueError("invalid P native cleanup receipt")
        elif kind == "finished":
            if (
                role != "decode"
                or active["phase"] != "decoding"
                or not all(r["native_freed"] and r["drained"] for r in ranks)
            ):
                raise ValueError(
                    "finished arrived before D readiness/drain/native cleanup"
                )
            active["phase"] = "saving" if self.config.prefix_pages else "freeing"
            for key in ("d_hbm", "d_rows"):
                self.ledger[key].change(-active["needed"][key])
            if not self.config.prefix_pages:
                for entry in self.directory.values():
                    entry["state"], entry["lock_ref"] = "draining", 0
                for side in ROLES:
                    self._command(side, "free")
        elif kind == "admit_ack":
            self._publish_prefix(command, ranks)
        elif kind == "free_ack":
            if active["phase"] != "freeing" or not all(
                r["freed"] and r["native_freed"] for r in ranks
            ):
                raise ValueError("free_ack lacks actual native/alive cleanup")
            key = "p_alive" if role == "prefill" else "d_alive"
            self.ledger[key].change(-active["needed"][key])
            active["free_acks"].append(role)
            if role == "prefill":
                for hash_value in active["hashes"]:
                    self.directory[hash_value].update(
                        state="freed", owner=None, locations=[]
                    )
        else:
            raise ValueError(f"unsupported SR event {kind}")
        if kind.endswith("_ack"):
            operation["ack"] = copy.deepcopy(message)
        else:
            active["events"][f"{role}:{kind}"] = copy.deepcopy(message)
        logger.info(
            "mempool_router event=%s role=%s rid=%s op_id=%s",
            kind,
            role,
            message["rid"],
            message["op_id"],
        )
        if active["phase"] == "saving" and not active["save_planned"]:
            self._plan_save()
        self._complete()

    def _plan_save(self) -> None:
        active = self.active
        assert active is not None
        if "prefill:native_freed" not in active["events"]:
            return
        active["save_planned"] = True
        missing = [
            i
            for i, value in enumerate(active["hashes"])
            if self.directory[value]["prefix"] is None
        ]
        pages = missing[: self.ledger["p_prefix"].snapshot()["free"]]
        if pages:
            op_id = self.next_op + 1
            hashes = [active["hashes"][page] for page in pages]
            parents = [
                value
                for value in active["hashes"][: pages[-1]]
                if self.directory[value]["prefix"] is not None
            ]
            for value in parents:
                prefix = self.directory[value]["prefix"]
                if prefix["state"] != "ready":
                    raise RuntimeError("admit parent is not ready")
                prefix["lock_ops"].append(op_id)
            for value in hashes:
                node = self.directory[value]
                node["lock_ref"] += 1
                node["source_ops"].append(op_id)
                node["prefix"] = dict(
                    state="writing", op_id=op_id, locations=[], lock_ops=[]
                )
            self.ledger["p_prefix"].change(len(pages))
            active["admit"] = dict(op_id=op_id, hashes=hashes, parents=parents)
            active["trace"].append(
                dict(kind="source_protect", role="prefill", op_id=op_id)
            )
            self._command(
                "prefill",
                "admit",
                source_pages=pages,
                hashes=hashes,
                sources=[
                    dict(
                        rank=rank["rank"],
                        slot=rank["slot"],
                        pages=[rank["pages"][page] for page in pages],
                    )
                    for rank in active["allocations"]["prefill"]
                ],
            )
        for value in active["hashes"]:
            node = self.directory[value]
            node["owner"] = None
            node["lock_ref"] -= 1
            if not node["lock_ref"]:
                node["state"] = "draining"
        active["trace"].append(
            dict(
                kind="owner_release",
                role="prefill",
                op_id=active["admit"]["op_id"] if pages else None,
            )
        )
        if not pages:
            active["admit_complete"] = True
            self._free_after_save()

    def _publish_prefix(
        self, command: dict[str, Any], ranks: list[dict[str, Any]]
    ) -> None:
        active = self.active
        if active is None or active["phase"] != "saving" or active["admit"] is None:
            raise ValueError("prefix ack has no planned save")
        admit = active["admit"]
        if command["role"] != "prefill" or admit["op_id"] != command["op_id"]:
            raise ValueError("prefix ack differs from planned save")
        targets = ranks[0]["target_pages"]
        if (
            len(targets) != len(command["source_pages"])
            or len(set(targets)) != len(targets)
            or any(
                type(page) is not int or not 0 <= page < self.config.prefix_pages
                for page in targets
            )
        ):
            raise ValueError("prefix ack has invalid target pages")
        occupied = {
            node["prefix"]["locations"][0]["logical_page"]
            for node in self.directory.values()
            if node["prefix"] is not None and node["prefix"]["state"] == "ready"
        }
        if occupied.intersection(targets):
            raise ValueError("prefix ack aliases a retained page")
        for rank, receipt in enumerate(ranks):
            expected_pages = [
                dict(
                    pool="prefix",
                    region=self.registrations["prefill"]["ranks"][rank]["session"],
                    replica=rank,
                    logical_page=page,
                    page_size=self.config.page_size,
                )
                for page in targets
            ]
            if (
                receipt.get("readable") is not True
                or receipt.get("main_layers") != 78
                or receipt.get("index_layers") != len(PRODUCERS)
                or receipt.get("source_pages") != command["source_pages"]
                or receipt.get("slot") != active["allocations"]["prefill"][rank]["slot"]
                or receipt.get("target_pages") != targets
                or receipt.get("pages") != expected_pages
            ):
                raise ValueError("prefix ack lacks matching source/target completion")
        for index, value in enumerate(admit["hashes"]):
            node = self.directory[value]
            parent = node["parent_hash"]
            if (
                parent is not None
                and self.directory[parent]["prefix"]["state"] != "ready"
            ):
                raise ValueError("prefix child cannot publish before parent")
            node["prefix"].update(
                state="ready", locations=[rank["pages"][index] for rank in ranks]
            )
            node["source_ops"].remove(command["op_id"])
            node["lock_ref"] -= 1
            node["state"] = "draining"
            active["trace"].append(
                dict(kind="publish", role="prefill", op_id=command["op_id"], hash=value)
            )
        for value in admit["parents"]:
            self.directory[value]["prefix"]["lock_ops"].remove(command["op_id"])
        active["admit_complete"] = True
        self._free_after_save()

    def _free_after_save(self) -> None:
        active = self.active
        assert active is not None
        if any(self.directory[value]["lock_ref"] for value in active["hashes"]):
            raise RuntimeError("alive source remains protected")
        active["phase"] = "freeing"
        for side in ROLES:
            self._command(side, "free")

    def backend_result(self, rid: str, result: dict[str, Any]) -> None:
        if self.active is None or self.active["request"]["rid"] != rid:
            raise ValueError("HTTP result does not belong to the active request")
        self.active["result"] = result
        self._complete()

    def _complete(self) -> None:
        active = self.active
        if (
            self.fault
            or active is None
            or len(active["free_acks"]) != 2
            or active["result"] is None
            or not active["admit_complete"]
        ):
            return
        if any(q.outstanding for name, q in self.ledger.items() if name != "p_prefix"):
            raise RuntimeError("completed request left outstanding quota")
        self.history.append(
            dict(
                rid=active["request"]["rid"],
                room=active["request"]["room"],
                result=active["result"],
                allocations=active["allocations"],
                events=active["events"],
                trace=active["trace"],
                hit_len=active["request"]["hit_len"],
                reads={
                    kind: copy.deepcopy(self.operations[op])
                    for kind, op in active["reads"].items()
                },
                admit=(
                    copy.deepcopy(self.operations[active["admit"]["op_id"]])
                    if active["admit"] is not None
                    else None
                ),
            )
        )
        # Bound replay receipts during persistent serving, together with the
        # public history. Older operations are stale and cannot free anything.
        retained = {entry["rid"] for entry in self.history}
        self.operations = {
            op_id: op
            for op_id, op in self.operations.items()
            if op["command"]["rid"] in retained
        }
        for value in reversed(active["hashes"]):
            node = self.directory[value]
            if node["prefix"] is None and not node["child_count"]:
                parent = node["parent_hash"]
                del self.directory[value]
                if parent is not None:
                    self.directory[parent]["child_count"] -= 1
        self.active = None

    def fail(self, reason: str) -> None:
        if self.fault is None:
            self.fault = reason
            self.outbox.clear()
            self.launches.clear()
            logger.error("mempool_router fail-stop: %s", reason)

    def check_timeouts(self) -> None:
        now = self.clock()
        if not self.ready and now - self.started > self.config.startup_timeout:
            self.fail("SR registration timeout")
        if self.ready and any(
            now - t > self.config.operation_timeout for t in self.last_seen.values()
        ):
            self.fail("SR heartbeat timeout")
        if any(
            op["ack"] is None and now - op["sent"] > self.config.operation_timeout
            for op in self.operations.values()
        ):
            self.fail("SR operation timeout; allocations retained")
        if (
            self.active is not None
            and now - self.active["started"] > self.config.operation_timeout
        ):
            self.fail("request timeout; allocations retained")


if __name__ == "__main__":
    # Construct the canonical class in the adapter; -m gives this module a
    # second __main__ identity, including a distinct RequestRejected class.
    from .mempool_router_transport import main

    main()
