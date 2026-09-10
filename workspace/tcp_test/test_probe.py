"""Offline checks of payload verification and pointer-API failure handling."""
import argparse
import ctypes
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import check_pair


class ProbeTest(unittest.TestCase):
    def run_probe(self, pinned=False, corrupt=False, register_rc=0):
        objects = {}

        class Store:
            def setup(self, host, metadata, segment, buffer, protocol, devices, master):
                assert segment == 0 and protocol == "tcp" and devices == ""
                return 0

            def register_buffer(self, ptr, size):
                return register_rc

            def put(self, key, data):
                objects[key] = data
                return 0

            def get(self, key):
                return b"bad" if corrupt else objects[key]

            def batch_put_from(self, keys, ptrs, sizes):
                objects[keys[0]] = ctypes.string_at(ptrs[0], sizes[0])
                return [0]

            def batch_get_into(self, keys, ptrs, sizes):
                data = objects[keys[0]]
                if corrupt:
                    data = b"x" * sizes[0]
                ctypes.memmove(ptrs[0], data, sizes[0])
                return [sizes[0]]

            def close(self):
                pass

        class Tensor:
            def __init__(self, size, **kwargs):
                self.buffer = ctypes.create_string_buffer(size)
                self.size = size

            def is_pinned(self):
                return True

            def data_ptr(self):
                return ctypes.addressof(self.buffer)

            def numel(self):
                return self.size

        modules = {
            "mooncake": types.ModuleType("mooncake"),
            "mooncake.store": types.SimpleNamespace(MooncakeDistributedStore=Store),
            "torch": types.SimpleNamespace(empty=Tensor, uint8=0,
                                           npu=types.SimpleNamespace(set_device=lambda _: None)),
            "torch_npu": types.ModuleType("torch_npu"),
        }
        with tempfile.TemporaryDirectory() as folder, patch.dict(sys.modules, modules), \
                patch.object(check_pair.resource, "setrlimit"):
            args = argparse.Namespace(output=folder, mode="client", pinned=pinned,
                                      device=0, pinned_mib=1, local_ip="192.0.2.1",
                                      target_ip="192.0.2.2", port=50081,
                                      worker="write", token="test")
            check_pair.worker(args)
            args.worker = "read"
            check_pair.worker(args)
            self.assertEqual((Path(folder) / "stage").read_text(), "DONE")

    def test_basic_roundtrip(self):
        self.run_probe()

    def test_pinned_roundtrip(self):
        self.run_probe(pinned=True)

    def test_basic_corruption_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "read mismatch"):
            self.run_probe(corrupt=True)

    def test_pinned_corruption_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "pinned read mismatch"):
            self.run_probe(pinned=True, corrupt=True)

    def test_registration_failure_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "register_buffer returned -600"):
            self.run_probe(pinned=True, register_rc=-600)


if __name__ == "__main__":
    unittest.main()
