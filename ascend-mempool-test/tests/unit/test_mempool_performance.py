"""Router-received token timing is distinct from client response latency."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from measure_mempool_router import MeasurementServer, read_decode_stream
from run_mempool_min import RouterConfig


async def lines(values):
    for value in values:
        yield (
            b"data: "
            + (b"[DONE]" if value == "DONE" else json.dumps(value).encode())
            + b"\n"
        )
        yield b"\n"


class TestMempoolPerformance(unittest.IsolatedAsyncioTestCase):
    async def test_first_and_last_token_not_final_metadata_or_done(self):
        chunks = [
            dict(output_ids=[]),
            dict(output_ids=[10]),
            dict(output_ids=[10, 20]),
            dict(output_ids=[10, 20, 30]),
            dict(
                output_ids=[10, 20, 30],
                meta_info=dict(finish_reason=dict(type="length")),
            ),
            "DONE",
        ]
        clock = Mock(side_effect=[12.0, 13.0, 16.0])
        result = await read_decode_stream(
            SimpleNamespace(content=lines(chunks)), clock, 10.0
        )
        metric = result["performance"]
        self.assertEqual(metric["router_ttft_ms"], 2000)
        self.assertEqual(metric["router_time_to_last_token_ms"], 6000)
        self.assertEqual(metric["tpot_ms"], 2000)
        self.assertEqual(metric["decode_tokens_per_second"], 0.5)
        self.assertTrue(metric["first_token_exact"])
        self.assertEqual(clock.call_count, 3)
        self.assertEqual(result["output_ids"], [10, 20, 30])

    async def test_coalesced_first_chunk_is_not_exact_first_token(self):
        response = SimpleNamespace(
            content=lines(
                [dict(output_ids=[1, 2]), dict(output_ids=[1, 2, 3, 4]), "DONE"]
            )
        )
        result = await read_decode_stream(response, Mock(side_effect=[2, 6]), 1)
        metric = result["performance"]
        self.assertFalse(metric["first_token_exact"])
        self.assertIsNone(metric["router_ttft_ms"])
        self.assertEqual(metric["first_chunk_tokens"], 2)
        self.assertEqual(metric["tpot_ms"], 2000)

    async def test_aborted_truncated_or_non_cumulative_stream_fails(self):
        for chunks in (
            [dict(output_ids=[1])],
            [dict(error="backend failure")],
            [dict(output_ids=[1], meta_info=dict(finish_reason=dict(type="abort")))],
            [dict(output_ids=[1]), dict(output_ids=[2]), "DONE"],
        ):
            with (
                self.subTest(chunks=chunks),
                self.assertRaises((RuntimeError, ValueError)),
            ):
                await read_decode_stream(
                    SimpleNamespace(content=lines(chunks)), lambda: 2, 1
                )

    async def test_adapter_streams_only_decode_and_retains_router_completion(self):
        router = SimpleNamespace(
            config=RouterConfig("p", "d", "/model"),
            active={"started": 1.0},
            clock=Mock(side_effect=[2.0, 4.0]),
            backend_result=Mock(),
            fail=Mock(),
        )
        server = MeasurementServer(router)

        class Response:
            status = 200

            def __init__(self):
                self.content = lines(
                    [dict(output_ids=[1]), dict(output_ids=[1, 2]), "DONE"]
                )

            async def json(self):
                return {"output_ids": [1]}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        server.http = SimpleNamespace(
            post=Mock(side_effect=lambda *a, **kw: Response())
        )
        request = dict(
            rid="r", input_ids=[8], room=4, max_new_tokens=2, ignore_eos=True
        )
        await server.infer(request)
        calls = server.http.post.call_args_list
        self.assertEqual(
            [call.kwargs["json"]["stream"] for call in calls], [False, True]
        )
        self.assertEqual(
            calls[1].kwargs["json"]["sampling_params"]["stream_interval"], 1
        )
        router.fail.assert_not_called()
        rid, result = router.backend_result.call_args.args
        self.assertEqual(rid, "r")
        self.assertEqual(result["output_ids"], [1, 2])
        self.assertEqual(result["performance"]["router_ttft_ms"], 1000)
