"""Token comparison must reject equal-but-invalid and nonidentical workloads."""

import copy
import json
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

    def test_quick_answer_cannot_reuse_previous_question_result(self):
        for answer in (63, 84):
            case = dict(self.case, expected_answer=answer)
            with self.assertRaisesRegex(ValueError, "answer"):
                gate.check_response(case, self.response)
            gate.check_response(
                case, dict(self.response, text=f"Calculation done. Answer: {answer}")
            )

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
                text = "<user>" + messages[0]["content"] + "</user><assistant>"
                return self.encode(text) if kwargs.get("tokenize") else text

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
            quick = gate.prepare(Path(folder), quick=True)
        self.assertEqual(
            [c["expected_answer"] for c in quick["cases"]], [42, 42, 63, 84]
        )
        self.assertEqual(quick["cases"][0]["max_new_tokens"], 1)
        for case in quick["cases"]:
            self.assertLessEqual(len(case["input_ids"]), 512)
            self.assertNotIn("PADDING", case["rendered_input"])
            self.assertNotIn(
                f"Answer: {case['expected_answer']}", case["rendered_input"]
            )
        self.assertFalse(tokenizer.options["enable_thinking"])
        self.assertEqual(
            [len(c["input_ids"]) for c in fixture["cases"]],
            [1024, 1024, 2047, 2048, 3072],
        )
        for case in fixture["cases"]:
            self.assertTrue(case["rendered_input"].endswith("</user><assistant>"))
            self.assertIn("Compute 17 + 25", case["rendered_input"])
            self.assertNotIn("Answer: 42", case["rendered_input"])

    def test_quick_fixture_serializes_when_tokenized_chat_returns_batch_encoding(self):
        from transformers.tokenization_utils_base import BatchEncoding

        class Tokenizer:
            def apply_chat_template(self, messages, **kwargs):
                rendered = "<user>" + messages[0]["content"] + "</user><assistant>"
                if not kwargs.get("tokenize"):
                    return rendered
                ids = self.encode(rendered, add_special_tokens=False)
                return BatchEncoding(
                    {"input_ids": ids, "attention_mask": [1] * len(ids)}
                )

            def encode(self, text, **kwargs):
                # Chat template already contains special tokens.
                assert kwargs.get("add_special_tokens") is False
                return list(map(ord, text))

            def decode(self, ids):
                if isinstance(ids, BatchEncoding):
                    ids = ids["input_ids"]
                return "".join(map(chr, ids))

        tokenizer = Tokenizer()
        transformers = SimpleNamespace(
            AutoTokenizer=SimpleNamespace(from_pretrained=lambda *a, **kw: tokenizer)
        )
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.dict(sys.modules, {"transformers": transformers}),
        ):
            # Exercise the same prepare -> JSON boundary as the failing CLI.
            fixture = json.loads(json.dumps(gate.prepare(Path(folder), quick=True)))
        self.assertEqual(len(fixture["cases"]), 4)
        for case in fixture["cases"]:
            self.assertIsInstance(case["input_ids"], list)
            self.assertTrue(all(type(i) is int for i in case["input_ids"]))
            self.assertEqual(
                tokenizer.decode(case["input_ids"]), case["rendered_input"]
            )


if __name__ == "__main__":
    unittest.main()
