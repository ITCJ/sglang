"""Target-host client: profile only after every request has warmed up in decode."""

import argparse
import asyncio
import json
import time
import traceback
from pathlib import Path

import aiohttp


async def run(args):
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    trace_dir = output / "steady_decode"
    prompts = []
    counts = [0] * args.batch_size
    retractions = [0] * args.batch_size
    finished = [False] * args.batch_size
    finishes = [None] * args.batch_size
    errors = []
    request_details = [{} for _ in range(args.batch_size)]
    changed = asyncio.Event()
    summary = {"config": vars(args), "status": "incomplete", "trace_dir": str(trace_dir)}
    tasks = []
    armed = False
    timeout = aiohttp.ClientTimeout(total=args.timeout, sock_read=args.timeout)
    timeline = (output / "progress.jsonl").open("w")

    def snapshot(kind):
        state = {"event": kind, "unix_time": time.time(), "completion_tokens": counts.copy(),
                 "finished": finished.copy(), "num_retractions": retractions.copy()}
        timeline.write(json.dumps(state) + "\n")
        timeline.flush()
        return state

    # Tokenizer setup can outlast the server's keep-alive timeout after precheck.
    connector = aiohttp.TCPConnector(force_close=True)
    async with aiohttp.ClientSession(
        timeout=timeout, read_bufsize=1024 * 1024, connector=connector
    ) as session:
        async def control(path, payload=None):
            async with session.post(args.base_url + path, json=payload) as response:
                body = await response.text()
                if response.status != 200:
                    raise RuntimeError(f"{path}: HTTP {response.status}: {body}")
                return body

        async def generate(i):
            detail = request_details[i]
            detail.update(request_index=i, sent_unix_time=time.time(), stage="sending")
            payload = {
                "input_ids": prompts[i], "stream": True,
                "sampling_params": {"temperature": 0, "max_new_tokens": args.output_len,
                                    "ignore_eos": True},
            }
            try:
                async with session.post(args.base_url + "/generate", json=payload) as response:
                    detail.update(http_status=response.status, headers_unix_time=time.time(), stage="streaming")
                    if response.status != 200:
                        raise RuntimeError(await response.text())
                    async for raw in response.content:
                        line = raw.decode().strip()
                        if not line.startswith("data:"):
                            continue
                        body = line[5:].strip()
                        if body == "[DONE]":
                            break
                        event = json.loads(body)
                        if "error" in event:
                            raise RuntimeError(str(event["error"]))
                        meta = event.get("meta_info", {})
                        counts[i] = meta.get("completion_tokens", counts[i])
                        if counts[i] > 0 and "first_token_unix_time" not in detail:
                            detail["first_token_unix_time"] = time.time()
                        retractions[i] = meta.get("num_retractions", retractions[i])
                        if meta.get("prompt_tokens", args.input_len) != args.input_len:
                            raise RuntimeError(f"Unexpected prompt length: {meta.get('prompt_tokens')}")
                        finishes[i] = meta.get("finish_reason")
                        changed.set()
                if counts[i] != args.output_len:
                    raise RuntimeError(f"request {i}: expected {args.output_len} output tokens, got {counts[i]}")
                detail["stage"] = "completed"
            except asyncio.CancelledError:
                detail["stage"] = "cancelled"
                raise
            except Exception as exc:
                detail.update(error_type=type(exc).__name__, error_repr=repr(exc),
                              cause=repr(exc.__cause__), context=repr(exc.__context__),
                              traceback=traceback.format_exc())
                print(f"request {i} failed during {detail['stage']}:\n{detail['traceback']}", flush=True)
                errors.append(f"request {i}: {exc}")
                raise
            finally:
                detail["ended_unix_time"] = time.time()
                finished[i] = True
                snapshot(f"request_{i}_finished")
                changed.set()

        try:
            print("Waiting for server readiness...", flush=True)
            deadline = time.monotonic() + args.timeout
            while True:
                try:
                    async with session.get(args.base_url + "/health", timeout=aiohttp.ClientTimeout(total=5)) as response:
                        if response.status == 200:
                            break
                        if response.status in (401, 403):
                            raise RuntimeError("Server requires authentication; use the dedicated experiment server")
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError("Server did not become ready before timeout")
                await asyncio.sleep(2)

            info = None
            for endpoint in ("/server_info", "/get_server_info"):
                async with session.get(args.base_url + endpoint) as response:
                    if response.status == 404:
                        continue
                    response.raise_for_status()
                    info = await response.json()
                    break
            if info is None:
                raise RuntimeError("Cannot inspect server configuration via server_info")
            (output / "server_info.json").write_text(json.dumps(info, indent=2) + "\n")
            if Path(info.get("model_path", "")).resolve() != Path(args.model_path).resolve():
                raise RuntimeError("Server model_path and client tokenizer path differ")
            for name, expected in (("tp_size", 16), ("dp_size", 1), ("device", "npu")):
                if info.get(name) != expected:
                    raise RuntimeError(f"Expected server {name}={expected}, got {info.get(name)}")
            if info.get("kv_cache_dtype") not in ("bf16", "bfloat16"):
                raise RuntimeError("This experiment's capacity estimate requires explicit BF16 cache")
            limits = [state.get("effective_max_running_requests_per_dp")
                      for state in info.get("internal_states", [])]
            limits.append(info.get("max_running_requests"))
            if not any(value is not None for value in limits):
                raise RuntimeError("Server did not report its request capacity")
            if any(value < args.batch_size for value in limits if value is not None):
                raise RuntimeError(f"Server request capacity {limits} cannot reach BS={args.batch_size}")
            needed = args.input_len + args.output_len
            if info.get("context_length") is not None and info["context_length"] < needed + 2:
                raise RuntimeError(f"Server context_length must be at least {needed + 2}, including reserved tokens")
            page_size = info.get("page_size") or 128
            # Include an extra page per request for admission/allocation headroom.
            required_tokens = args.batch_size * (((needed + page_size - 1) // page_size + 1) * page_size) + 1
            capacity = info.get("max_total_num_tokens")
            if capacity is not None and capacity < required_tokens:
                raise RuntimeError(f"Cache capacity {capacity} < conservative required {required_tokens}")
            if info.get("speculative_algorithm") or info.get("disaggregation_mode", "null") not in (None, "null"):
                raise RuntimeError("This experiment requires non-speculative colocation")
            if info.get("dcp_size", 1) != 1:
                raise RuntimeError("This experiment requires dcp_size=1")
            summary["server_precheck"] = {"required_cache_tokens": required_tokens, "capacity": capacity}
            if trace_dir.exists() and any(trace_dir.iterdir()):
                raise RuntimeError("Trace directory is not empty; use a new PROFILE_RUN_DIR")

            preparation_started = time.monotonic()
            summary["input_preparation_started_unix_time"] = time.time()
            from transformers import AutoTokenizer

            tokenizer = AutoTokenizer.from_pretrained(
                args.model_path, trust_remote_code=True, local_files_only=True
            )
            seed = tokenizer.encode(
                "Explain how a computer processes data, including memory, arithmetic, "
                "communication and scheduling. Provide a detailed technical discussion.\n",
                add_special_tokens=False,
            )
            if not seed:
                raise RuntimeError("Tokenizer produced no prompt tokens")
            for i in range(args.batch_size):
                prefix = tokenizer.encode(f"Question {i}: ", add_special_tokens=False)
                prompts.append((prefix + seed * (args.input_len // len(seed) + 1))[:args.input_len])
            summary["input_preparation_seconds"] = time.monotonic() - preparation_started
            print(f"Server checked. Starting {args.batch_size} requests, waiting for decode warmup...", flush=True)
            snapshot("workload_start")
            tasks = [asyncio.create_task(generate(i)) for i in range(args.batch_size)]

            async def await_steady():
                while True:
                    changed.clear()
                    if errors or any(finished) or any(retractions):
                        raise RuntimeError(f"Batch drained/failed before steady decode: {errors}")
                    if min(counts) >= args.warmup_tokens:
                        return
                    await changed.wait()

            await asyncio.wait_for(await_steady(), timeout=args.timeout)
            summary["before_start"] = snapshot("before_start_profile")
            print(f"All requests warmed up: tokens={counts}. Starting profile.", flush=True)
            # No start_step: that field is an absolute scheduler counter in the
            # local legacy implementation, not a relative warmup-step count.
            armed = True
            await control("/start_profile", {
                "output_dir": str(trace_dir), "activities": ["CPU", "GPU"],
                "with_stack": False, "record_shapes": True,
                "profile_by_stage": False,
            })
            summary["after_start"] = snapshot("after_start_profile")
            capture_started = time.monotonic()
            while True:
                elapsed = time.monotonic() - capture_started
                if errors or any(finished) or any(retractions):
                    raise RuntimeError("Batch drained, failed or retracted during capture")
                advances = [b - a for a, b in zip(
                    summary["after_start"]["completion_tokens"], counts
                )]
                if elapsed >= args.profile_seconds and min(advances) >= args.profile_min_tokens:
                    break
                if elapsed >= args.profile_max_seconds:
                    raise RuntimeError("Too few decode tokens during capture; inspect client/server logs")
                await asyncio.sleep(0.05)
            summary["before_stop"] = snapshot("before_stop_profile")
            summary["capture_seconds_after_start_ack"] = elapsed
            # Stop and flush before waiting for the workload to finish.
            await control("/stop_profile")
            armed = False
            summary["after_stop"] = snapshot("after_stop_profile")
            # Conservative: reject even if a completion during stop was after
            # the last recorded kernel. Retake with a longer output instead.
            for key in ("before_start", "after_start", "before_stop", "after_stop"):
                if any(summary[key]["num_retractions"]):
                    raise RuntimeError("Request retraction occurred; this is not an uninterrupted decode window")
                if any(summary[key]["finished"]) or max(summary[key]["completion_tokens"]) >= args.output_len:
                    raise RuntimeError("Batch drained during capture/flush; increase OUTPUT_LEN or reduce PROFILE_SECONDS")
            if any(b <= a for a, b in zip(summary["after_start"]["completion_tokens"],
                                         summary["before_stop"]["completion_tokens"])):
                raise RuntimeError("Not every request advanced during capture; inspect trace or extend PROFILE_SECONDS")
            await asyncio.gather(*tasks)
            # API success alone is not proof that all TP ranks exported traces.
            # Requires this client to run in the same filesystem as the server.
            deadline = time.monotonic() + args.trace_timeout
            previous = None
            while True:
                files = sorted(trace_dir.rglob("trace_view.json"))
                manifest = [{"path": str(path), "bytes": path.stat().st_size} for path in files]
                if len(manifest) >= info["tp_size"] and all(item["bytes"] > 0 for item in manifest) and manifest == previous:
                    summary["trace_files"] = manifest
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Expected {info['tp_size']} nonempty NPU trace_view.json files; found {len(files)}. Inspect {trace_dir}")
                previous = manifest
                await asyncio.sleep(2)
            summary["status"] = "trace_files_present_client_window_validated_verify_device_timeline"
            print(f"Decode traces: {trace_dir}", flush=True)
            print("Check server log and trace: batch size must remain constant; no prefill/retraction.", flush=True)
        except BaseException as exc:
            summary["status"] = "invalid"
            summary["error"] = str(exc)
            raise
        finally:
            if armed:
                try:
                    await control("/stop_profile")
                except Exception as exc:
                    summary["stop_cleanup_error"] = str(exc)
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            summary["final_tokens"] = counts
            summary["final_retractions"] = retractions
            summary["finish_reasons"] = finishes
            summary["request_errors"] = errors
            summary["request_details"] = request_details
            (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
            timeline.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:6699")
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--batch-size", type=int, default=11)
    parser.add_argument("--input-len", type=int, default=2048)
    parser.add_argument("--output-len", type=int, default=512)
    parser.add_argument("--warmup-tokens", type=int, default=64)
    parser.add_argument("--profile-seconds", type=float, default=1)
    parser.add_argument("--profile-min-tokens", type=int, default=8)
    parser.add_argument("--profile-max-seconds", type=float, default=30)
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--trace-timeout", type=float, default=300)
    args = parser.parse_args()
    if min(args.batch_size, args.input_len, args.warmup_tokens, args.profile_seconds,
           args.profile_min_tokens, args.profile_max_seconds, args.timeout, args.trace_timeout) <= 0:
        parser.error("sizes and durations must be positive")
    if args.profile_max_seconds < args.profile_seconds:
        parser.error("profile-max-seconds must be >= profile-seconds")
    if args.output_len <= args.warmup_tokens + args.profile_min_tokens + 32:
        parser.error("output-len must exceed warmup-tokens + profile-min-tokens by more than 32")
    asyncio.run(run(args))
