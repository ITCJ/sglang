"""Token comparison must reject equal-but-invalid and nonidentical workloads."""

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import verify_model_tokens as gate


class TestModelTokens(unittest.TestCase):
    def setUp(self):
        self.case = dict(name="long", input_ids=[1] * 3072, max_new_tokens=128)
        self.response = dict(
            output_ids=[42, 7, 8],
            text="Adding 17 and 25 gives 42. Answer: 42",
            meta_info=dict(
                prompt_tokens=3072, completion_tokens=3, finish_reason=dict(type="stop")
            ),
        )

    def test_response_needs_actual_length_answer_and_decode(self):
        gate.check_response(self.case, self.response)
        for key, value in (("text", "The sum is 43."), ("output_ids", [42])):
            response = copy.deepcopy(self.response)
            response[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                gate.check_response(self.case, response)
        wrong = dict(self.response, text="Answer: 42.5")
        with self.assertRaisesRegex(ValueError, "answer"):
            gate.check_response(self.case, wrong)
        for key, value in (
            ("prompt_tokens", 1024),
            ("finish_reason", dict(type="length")),
        ):
            response = copy.deepcopy(self.response)
            response["meta_info"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                gate.check_response(self.case, response)

    def test_comparison_checks_tokens_and_workload(self):
        report = dict(
            status="passed",
            fixture=dict(cases=[self.case]),
            results=[dict(case=self.case, response=self.response)],
        )
        gate.compare(report, copy.deepcopy(report))
        changed = copy.deepcopy(report)
        changed["results"][0]["response"]["output_ids"][1] = 9
        with self.assertRaisesRegex(ValueError, "token.*position 1"):
            gate.compare(report, changed)
        changed = copy.deepcopy(report)
        changed["fixture"]["cases"][0]["input_ids"][0] = 2
        with self.assertRaisesRegex(ValueError, "fixture"):
            gate.compare(report, changed)

    def test_same_wrong_answer_cannot_pass(self):
        self.response["text"] = "The sum is 43."
        report = dict(
            status="passed",
            fixture={},
            results=[dict(case=self.case, response=self.response)],
        )
        with self.assertRaisesRegex(ValueError, "answer"):
            gate.compare(report, copy.deepcopy(report))

    def test_prepare_keeps_template_suffix_and_exact_lengths(self):
        class Tokenizer:
            def apply_chat_template(self, messages, **kwargs):
                self.options = kwargs
                return "<user>" + messages[0]["content"] + "</user><assistant>"

            def encode(self, text, **kwargs):
                return list(map(ord, text))

            def decode(self, ids):
                return "".join(map(chr, ids))

        tokenizer = Tokenizer()
        transformers = SimpleNamespace(
            AutoTokenizer=SimpleNamespace(from_pretrained=lambda *a, **kw: tokenizer)
        )
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.dict(sys.modules, {"transformers": transformers}),
        ):
            fixture = gate.prepare(Path(folder))
        self.assertFalse(tokenizer.options["enable_thinking"])
        self.assertEqual(
            [len(c["input_ids"]) for c in fixture["cases"]],
            [1024, 1024, 2047, 2048, 3072],
        )
        for case in fixture["cases"]:
            self.assertTrue(case["rendered_input"].endswith("</user><assistant>"))
            self.assertIn("Compute 17 + 25", case["rendered_input"])
            self.assertNotIn("Answer: 42", case["rendered_input"])


if __name__ == "__main__":
    unittest.main()
