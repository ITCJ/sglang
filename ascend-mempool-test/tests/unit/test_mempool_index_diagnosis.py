"""Cold comparisons preserve failure evidence without granting acceptance."""

import json
import unittest

from mempool_index_diagnosis import compare


class TestIndexDiagnosis(unittest.TestCase):
    def setUp(self):
        self.case = dict(
            name="index-update",
            input_ids=[1] * 5120,
            sampling_params=dict(max_new_tokens=128, ignore_eos=False),
            expected_answer=dict(place="harbor", pencils=17, route="north"),
        )
        self.original = self.response(
            '```json\n{"place": "harbor", "pencils": 12, "route": "north"}\n```'
        )
        self.snapshot = dict(
            fault=None, active=None, completed=[dict(hit_len=0, reads={})]
        )

    def response(self, text):
        ids = list(map(ord, text))
        return dict(
            text=text,
            output_ids=ids,
            meta_info=dict(
                prompt_tokens=5120,
                completion_tokens=len(ids),
                finish_reason=dict(type="stop"),
            ),
        )

    def test_same_wrong_answer_is_diagnostic_evidence_not_a_pass(self):
        report = compare(self.case, self.original, self.original, self.snapshot)
        self.assertEqual(report["status"], "failed")
        self.assertTrue(report["ids"]["equal"])
        self.assertEqual(report["cold_text"], self.original["text"])
        self.assertFalse(report["bm_readback"])

    def test_correct_cold_answer_retains_the_original_failure(self):
        cold = self.response(json.dumps(self.case["expected_answer"]))
        report = compare(self.case, self.original, cold, self.snapshot)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["original"]["status"], "failed")
        self.assertFalse(report["ids"]["equal"])
        self.assertIn("not ticket acceptance", report["coverage"])

    def test_prefix_hit_cannot_be_reported_as_a_cold_control(self):
        self.snapshot["completed"][0]["hit_len"] = 4096
        with self.assertRaisesRegex(ValueError, "reused a prefix"):
            compare(self.case, self.original, self.original, self.snapshot)
