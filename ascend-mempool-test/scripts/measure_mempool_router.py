"""Benchmark-only Router adapter: timestamp D SSE tokens without observation.

Production allocation, dispatch, completion and cleanup are inherited unchanged.
The public request remains non-streaming; reported TTFT ends at this Router.
"""

from __future__ import annotations

import asyncio
import json
import logging

from run_mempool_min import RouterConfig
from ascend_mempool_pd.mempool_router import MainRouter
from ascend_mempool_pd.mempool_router_transport import MainServer


def check_response(value: dict, role: str) -> None:
    reason = value.get("meta_info", {}).get("finish_reason") or {}
    if "error" in value or reason.get("type") == "abort":
        raise RuntimeError(f"{role} generation failed: {value}")


async def read_decode_stream(response, clock, started: float) -> dict:
    """Read the native cumulative-token SSE format used by run_service.sh."""
    first_time, last_time, first_count = None, None, 0
    result, count, done = {}, 0, False
    async for raw in response.content:
        line = raw.strip()
        if not line.startswith(b"data:"):
            continue
        data = line[5:].strip()
        if data == b"[DONE]":
            done = True
            break
        value = json.loads(data)
        check_response(value, "decode")
        ids = value.get("output_ids", [])
        if (
            not isinstance(ids, list)
            or len(ids) < count
            or ids[:count] != result.get("output_ids", [])
        ):
            raise ValueError("expected cumulative output_ids in D stream")
        if len(ids) > count:
            now = clock()
            if first_time is None:
                first_time, first_count = now, len(ids)
            last_time = now
        count, result = len(ids), value
    if not done or not count or first_time is None:
        raise ValueError("D stream ended without DONE or output tokens")
    span = last_time - first_time
    subsequent = count - first_count
    result["performance"] = dict(
        clock="prefill Router host monotonic",
        scope="Router admission to D token stream receipt; not client streaming TTFT",
        router_ttft_ms=(first_time - started) * 1000 if first_count == 1 else None,
        router_first_chunk_ms=(first_time - started) * 1000,
        router_time_to_last_token_ms=(last_time - started) * 1000,
        tpot_ms=span * 1000 / subsequent if subsequent else None,
        decode_tokens_per_second=subsequent / span if subsequent and span > 0 else None,
        first_chunk_tokens=first_count,
        first_token_exact=first_count == 1,
        output_tokens=count,
    )
    return result


class MeasurementServer(MainServer):
    async def infer(self, request: dict) -> None:
        config = self.router.config
        started = self.router.active["started"]
        body = dict(
            rid=request["rid"],
            input_ids=request["input_ids"],
            bootstrap_host=config.prefill_host,
            bootstrap_port=config.bootstrap_port,
            bootstrap_room=request["room"],
            sampling_params=dict(
                temperature=0,
                top_k=1,
                top_p=1,
                max_new_tokens=request["max_new_tokens"],
                ignore_eos=request["ignore_eos"],
                stream_interval=1,
            ),
        )

        async def post(role: str) -> dict:
            async with self.http.post(
                config.backend(role) + "/generate",
                json=dict(body, stream=role == "decode"),
            ) as response:
                if response.status != 200:
                    raise RuntimeError(
                        f"{role} HTTP {response.status}: {await response.text()}"
                    )
                if role == "decode":
                    return await read_decode_stream(
                        response, self.router.clock, started
                    )
                value = await response.json()
                check_response(value, role)
                return value

        try:
            _, result = await asyncio.gather(post("prefill"), post("decode"))
            self.router.backend_result(request["rid"], result)
        except Exception as error:
            self.router.fail(str(error))


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    config = RouterConfig.read(args.config)
    if config.observe or config.observe_trace or config.verify_prefix:
        parser.error(
            "performance run requires observe/observe_trace/verify_prefix=false"
        )
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(MeasurementServer(MainRouter(config, args.run_id)).serve())


if __name__ == "__main__":
    main()
