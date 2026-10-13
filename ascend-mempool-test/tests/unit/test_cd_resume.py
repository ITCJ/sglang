"""The launch-deadline exception cannot admit unrelated measurement changes."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import cd_resume


class TestCodeCompatibility(unittest.TestCase):
    def tearDown(self):
        cd_resume._audited_current.cache_clear()

    def test_reviewed_runtime_and_exact_recorded_source_are_required(self):
        current = dict(git_sha="new", source_sha256="new-source")
        for actual, digest, expected in (
            (current, cd_resume.REVIEWED_RUNTIME_SHA256, True),
            (current, "changed-copy-or-timing", False),
            (
                dict(current, source_sha256="dirty"),
                cd_resume.REVIEWED_RUNTIME_SHA256,
                False,
            ),
        ):
            cd_resume._audited_current.cache_clear()
            with (
                patch.object(cd_resume, "code_identity", return_value=actual),
                patch.object(cd_resume, "runtime_digest", return_value=digest),
            ):
                self.assertEqual(
                    cd_resume.compatible_code(cd_resume.BASELINE, current), expected
                )
        self.assertFalse(
            cd_resume.compatible_code(
                dict(cd_resume.BASELINE, source_sha256="dirty"), current
            )
        )
        self.assertFalse(
            cd_resume.compatible_code(
                dict(cd_resume.BASELINE, git_sha="other"), current
            )
        )
        self.assertFalse(cd_resume.compatible_code(cd_resume.BASELINE, {}))
        self.assertTrue(cd_resume.compatible_code(current, current))

    def test_checked_in_exception_matches_actual_audited_runtime(self):
        self.assertEqual(cd_resume.runtime_digest(), cd_resume.REVIEWED_RUNTIME_SHA256)
        self.assertTrue(
            cd_resume.compatible_code(cd_resume.BASELINE, cd_resume.code_identity())
        )
