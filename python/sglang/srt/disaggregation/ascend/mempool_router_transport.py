"""HTTP/ZMQ adapters for Main and the scheduler's rank-zero SR.

The Main event loop owns its state machine. SR sockets are polled only by
rank zero at the ordered scheduler tick; received messages cannot mutate KV.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlsplit

from .mempool_native_reservation import NativeReservations
from .mempool_prefill_restore import PrefillRestore, install_prefill_cache
from .mempool_router import MainRouter, RequestRejected
from .mempool_router_protocol import CONFIG_ENV, RUN_ENV, RouterConfig, model_digest
from .mempool_subrouter import MempoolSubrouter, RouterShard

logger = logging.getLogger(__name__)


class SRConnection:
    def __init__(self, endpoint: str, role: str, session: str) -> None:
        from zmq.constants import Flag, SocketOption, SocketType
        from zmq.error import Again
        from zmq.sugar.context import Context

        self.nonblock, self.again = Flag.NOBLOCK, Again
        self.socket = Context.instance().socket(SocketType.DEALER)
        self.socket.setsockopt(SocketOption.IDENTITY, f"{role}:{session}".encode())
        self.socket.setsockopt(SocketOption.LINGER, 0)
        self.socket.setsockopt(SocketOption.MAXMSGSIZE, 4 * 1024 * 1024)
        self.socket.setsockopt(SocketOption.SNDHWM, 128)
        self.socket.setsockopt(SocketOption.RCVHWM, 128)
        self.socket.connect(endpoint)

    def receive(self) -> list[dict[str, Any]]:
        incoming: list[dict[str, Any]] = []
        for _ in range(64):
            try:
                message: Any = self.socket.recv_json(flags=self.nonblock)
                incoming.append(message)
            except self.again:
                break
        return incoming

    def send(self, message: dict[str, Any]) -> None:
        self.socket.send_json(message, flags=self.nonblock)


def attach_subrouter(
    service: Any, gather: Callable[[Any], Sequence[Any]]
) -> MempoolSubrouter:
    """Attach after BM, Graph and native transfer layout initialization."""
    config = RouterConfig.read(os.environ[CONFIG_ENV])
    run_id = os.environ[RUN_ENV]
    scheduler, control = service.scheduler, service.control
    args, runtime = scheduler.server_args, service.runtime
    role = control.local.role
    required = dict(
        page_size=config.page_size,
        disable_radix_cache=True,
        chunked_prefill_size=-1,
        max_prefill_tokens=config.prompt_capacity,
        mempool_prefill_capacity=config.prompt_capacity,
        mempool_decode_capacity=config.decode_capacity,
        disable_cuda_graph=role == "prefill",
    )
    for name, value in required.items():
        if getattr(args, name) != value:
            raise ValueError(f"Router requires {name}={value!r}")
    if (
        args.model_path != config.model_path
        or args.context_length < config.context_length
    ):
        raise ValueError("Router model/context differs from worker launch")
    required_length = config.prompt_capacity + config.decode_capacity
    generation_limit = scheduler.max_new_tokens_limit
    if scheduler.max_req_len - 1 < required_length or (
        generation_limit is not None and 0 < generation_limit < config.decode_capacity
    ):
        raise ValueError("native scheduler would clip the Router generation budget")
    allocator, rows = scheduler.token_to_kv_pool_allocator, scheduler.req_to_token_pool
    pool = allocator.get_kvcache()
    transfer = control.local.transfer_layout
    if (
        allocator.page_size != config.page_size
        or pool.page_size != config.page_size
        or runtime.config.page_size != config.page_size
        or transfer is None
        or transfer.page_size != config.page_size
    ):
        raise ValueError(
            "Router, DRAM, native allocator, KV pool and Index K page_size must match"
        )
    native = (
        NativeReservations(
            rows,
            allocator,
            reset_rows=scheduler.tp_worker.model_runner.attn_backend.sparse_kv_manager.reset_requests,
        )
        if role == "decode"
        else None
    )
    if native is not None:
        rows.mempool_reservations = native
    prefix = getattr(runtime, "prefix_store", None)
    if config.prefix_pages:
        layout = runtime.manager.layout
        if (
            layout.prefix_main is None
            or layout.prefix_index is None
            or layout.prompt_index is None
            or layout.prefix_main.slots != config.prefix_pages
            or layout.prefix_main.tokens != config.page_size
            or (
                role == "prefill"
                and (prefix is None or prefix.verify != config.verify_prefix)
            )
            or (role == "decode" and prefix is not None)
        ):
            raise ValueError("Router prefix regions differ from initialized storage")
    elif prefix is not None:
        raise ValueError(
            "Router prefix is disabled but worker allocated prefix storage"
        )
    restore = None
    if config.match_prefix and role == "prefill":
        if prefix is None:
            raise ValueError("prefix restore requires local storage")
        restore = PrefillRestore(
            allocator, prefix, verify_boundaries=config.verify_prefix
        )
        install_prefill_cache(scheduler, restore)
    shard = RouterShard(
        control,
        native=native,
        effect=service._effect,
        reply_to=service.tick.reply_to,
        page_size=config.page_size,
        bootstrap=(config.prefill_host, config.bootstrap_port),
        prefix=prefix,
        restore=restore,
    )
    connection = (
        SRConnection(config.control_endpoint, role, control.local.session)
        if control.local.tp_rank == 0
        else None
    )
    # Read before any real Req can consume these capacities.
    registration = dict(
        rank=control.local.tp_rank,
        session=control.local.session,
        model=model_digest(args.model_path),
        vocab_size=scheduler.model_config.vocab_size,
        layers=runtime.layout.layers,
        dim=runtime.layout.dim,
        dtype=runtime.layout.dtype,
        indexer_layers=list(pool.indexer_layer_ids),
        index_dim=pool.index_head_dim,
        tp_size=control.local.tp_size,
        dp_size=args.dp_size,
        pp_size=args.pp_size,
        slots=runtime.layout.slots,
        prompt_capacity=runtime.manager.layout.prompt.tokens,
        decode_capacity=runtime.manager.layout.decode.tokens,
        page_size=runtime.config.page_size,
        hbm_page_size=allocator.page_size,
        hbm_pages=allocator.available_size() // allocator.page_size,
        request_rows=rows.available_size(),
        prompt_max=min(
            config.prompt_capacity, args.max_prefill_tokens, allocator.available_size()
        ),
        numa_node=runtime.manager.numa_node,
        numa_placement_verified=False,
        replicas=config.replicas(),
        replica_locations=runtime.manager.replica_registration(),
        graph=not args.disable_cuda_graph,
    )
    if runtime.config.numa.rank_map is not None:
        registration.update(
            runtime.manager.placement_registration(
                control.local.tp_rank, urlsplit(runtime.config.nic).hostname or ""
            )
        )
    if config.prefix_pages:
        registration.update(
            prefix_pages=config.prefix_pages if role == "prefill" else 0,
            local_dram_bytes=runtime.manager.layout.contribution_bytes(
                runtime.manager.rank
            ),
            verify_prefix=config.verify_prefix,
        )
    if config.match_prefix:
        registration["match_prefix"] = True

    def registered() -> dict[str, Any]:
        if control.peer is None:
            raise RuntimeError("native control handshake has not completed")
        result = dict(registration, peer_session=control.peer.session)
        capacity_test = os.environ.get("SGLANG_NPU_MEMPOOL_TEST_CAPACITY")
        if capacity_test is not None:
            if capacity_test != "1" or not (
                config.verify_prefix and config.match_prefix
            ):
                raise ValueError("capacity fault requires verified prefix acceptance")
            if role == "decode" and control.local.tp_rank == 15:
                # Isolated acceptance fault: limit the advertised budget, not the
                # physical allocator. Preserve both numbers as hardware evidence.
                required = (
                    config.prompt_capacity + config.decode_capacity
                ) // config.page_size
                actual = result["hbm_pages"]
                result["hbm_pages"] = min(actual, required - 1)
                logger.warning(
                    "mempool acceptance_capacity data=%s",
                    json.dumps(
                        dict(
                            run_id=run_id,
                            rank=15,
                            actual_hbm_pages=actual,
                            advertised_hbm_pages=result["hbm_pages"],
                            required_hbm_pages=required,
                        )
                    ),
                )
        return result

    return MempoolSubrouter(
        shard,
        run_id=run_id,
        gather=gather,
        receive=connection.receive if connection else lambda: [],
        send=connection.send if connection else lambda message: None,
        registration=registered,
        timeout=config.operation_timeout,
    )


class MainServer:
    def __init__(self, router: MainRouter) -> None:
        self.router = router
        self.routes: dict[str, bytes] = {}
        self.clients: dict[str, asyncio.Future[Any]] = {}
        self.jobs: set[asyncio.Task[Any]] = set()
        self.socket: Any = None
        self.http: Any = None

    async def generate(self, request: Any) -> Any:
        from aiohttp import web

        try:
            payload = await request.json()
            admitted = self.router.submit(payload)
        except RequestRejected as exc:
            return web.json_response(dict(error=str(exc)), status=exc.status)
        except (ValueError, TypeError):
            return web.json_response(dict(error="invalid request JSON"), status=400)
        future = asyncio.get_running_loop().create_future()
        self.clients[admitted["rid"]] = future
        # Disconnecting a client must not cancel inference, manufacture a drain
        # or release a reservation. The pump keeps running to real completion.
        result = await asyncio.shield(future)
        return web.json_response(result)

    async def status(self, request: Any) -> Any:
        from aiohttp import web

        return web.json_response(self.router.status())

    async def health(self, request: Any) -> Any:
        from aiohttp import web

        ready = (
            self.router.fault is None
            if request.path == "/health"
            else self.router.ready
        )
        return web.json_response(
            dict(ready=ready, error=self.router.fault), status=200 if ready else 503
        )

    async def infer(self, request: dict[str, Any]) -> None:
        config = self.router.config
        body = dict(
            rid=request["rid"],
            input_ids=request["input_ids"],
            stream=False,
            bootstrap_host=config.prefill_host,
            bootstrap_port=config.bootstrap_port,
            bootstrap_room=request["room"],
            sampling_params=dict(
                temperature=0,
                top_k=1,
                top_p=1,
                max_new_tokens=request["max_new_tokens"],
                ignore_eos=request["ignore_eos"],
            ),
        )

        async def post(role: str) -> dict[str, Any]:
            async with self.http.post(
                config.backend(role) + "/generate", json=body
            ) as response:
                value = await response.json()
                if (
                    response.status != 200
                    or "error" in value
                    or value.get("meta_info", {}).get("finish_reason", {}).get("type")
                    == "abort"
                ):
                    raise RuntimeError(f"{role} generation failed: {value}")
                return value  # type: ignore[no-any-return]

        try:
            responses: Sequence[dict[str, Any]] = await asyncio.gather(
                post("prefill"), post("decode")
            )
            result = responses[1]
            ids = result.get("output_ids")
            if (
                not isinstance(ids, list)
                or not ids
                or any(type(i) is not int for i in ids)
            ):
                raise RuntimeError("D response has no output token IDs")
            self.router.backend_result(request["rid"], result)
        except Exception as exc:
            self.router.fail(str(exc))

    async def pump(self) -> None:
        from zmq.constants import Flag

        last_heartbeat = float("-inf")
        while True:
            try:
                while await self.socket.poll(timeout=0):
                    route, data = await self.socket.recv_multipart()
                    message = json.loads(data)
                    role = message["role"]
                    if role in self.routes and route != self.routes[role]:
                        raise ValueError(
                            "a different SR attempted to replace the active session"
                        )
                    if message["kind"] == "register":
                        self.routes[role] = route
                    elif role not in self.routes:
                        raise ValueError("message from unregistered SR")
                    self.router.receive(message)
                self.router.check_timeouts()
                while self.router.outbox:
                    command = self.router.outbox.popleft()
                    await self.socket.send_multipart(
                        [self.routes[command["role"]], json.dumps(command).encode()],
                        flags=Flag.NOBLOCK,
                    )
                while self.router.launches:
                    job = asyncio.create_task(
                        self.infer(self.router.launches.popleft())
                    )
                    self.jobs.add(job)
                    job.add_done_callback(self.jobs.discard)
                for completed in self.router.history:
                    future = self.clients.pop(completed["rid"], None)
                    if future is not None and not future.done():
                        future.set_result(completed["result"])
                now = time.monotonic()
                if now - last_heartbeat >= 1:
                    for role, route in self.routes.items():
                        message = dict(
                            run_id=self.router.run_id, role=role, kind="heartbeat"
                        )
                        if self.router.fault:
                            message.update(kind="error", reason=self.router.fault)
                        await self.socket.send_multipart(
                            [route, json.dumps(message).encode()], flags=Flag.NOBLOCK
                        )
                    last_heartbeat = now
                if self.router.fault:
                    for future in self.clients.values():
                        if not future.done():
                            future.set_exception(RuntimeError(self.router.fault))
                    self.clients.clear()
            except Exception as exc:
                self.router.fail(str(exc))
            await asyncio.sleep(0.01)

    async def serve(self) -> None:
        import aiohttp
        from aiohttp import web
        from zmq.asyncio import Context
        from zmq.constants import SocketOption, SocketType

        config = self.router.config
        self.socket = Context.instance().socket(SocketType.ROUTER)
        self.socket.setsockopt(SocketOption.LINGER, 0)
        self.socket.setsockopt(SocketOption.ROUTER_MANDATORY, 1)
        self.socket.setsockopt(SocketOption.MAXMSGSIZE, 4 * 1024 * 1024)
        self.socket.bind(config.control_endpoint)
        app = web.Application(client_max_size=1024 * 1024)
        app.add_routes(
            [
                web.post("/generate", self.generate),
                web.get("/status", self.status),
                web.get("/health", self.health),
                web.get("/ready", self.health),
            ]
        )
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, config.prefill_host, config.main_port).start()
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=config.operation_timeout),
            trust_env=False,
        ) as self.http:
            pump = asyncio.create_task(self.pump())
            try:
                await asyncio.Event().wait()
            finally:
                pump.cancel()
                for job in self.jobs:
                    job.cancel()
                await asyncio.gather(pump, *self.jobs, return_exceptions=True)
                await runner.cleanup()
                self.socket.close()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Ascend mempool-min Main Router")
    parser.add_argument("--config", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(
        MainServer(MainRouter(RouterConfig.read(args.config), args.run_id)).serve()
    )
