"""Keep the hardware gate within its HBM budget without losing wide offsets."""

import tarfile
import unittest
from pathlib import Path

import verify_page_first_copy


class TestPageFirstArena(unittest.TestCase):
    def test_both_phases_fit_eight_gib_and_access_above_uint32(self):
        root = Path(__file__).resolve().parents[3]
        archive = root / ".scratch/k2/verification/ticket26-local/phase1-source.tar.gz"
        with tarfile.open(archive) as bundle:
            source = bundle.extractfile(
                "ascend-mempool-test/scripts/verify_page_first_copy.py"
            ).read()
        namespace = {
            "__name__": "phase1_probe",
            "__file__": verify_page_first_copy.__file__,
        }
        exec(compile(source, "phase1_probe", "exec"), namespace)
        for phase, geometries in (
            ("phase1", namespace["geometries"]),
            ("phase2", verify_page_first_copy.geometries),
        ):
            for page_size in (64, 128):
                with self.subTest(phase=phase, page_size=page_size):
                    layouts = geometries(page_size)
                    self.assertEqual(len(layouts), 3)
                    self.assertLessEqual(max(x.total_bytes for x in layouts), 8 << 30)
                    main = layouts[1]
                    # Replay version 1 selects the last physical page, last layer,
                    # and last token for logical position page_size - 1.
                    offset = main.total_bytes - main.row_bytes
                    self.assertGreater(offset, (1 << 32) - 1)
                    self.assertEqual(
                        (main.layers, main.tokens, main.dim), (78, 8192, 576)
                    )
                    self.assertGreaterEqual(
                        main.physical_pages, 2 * main.tokens // page_size
                    )
