"""CPU mapped-memory oracle for the public multibase ABI."""

import ctypes

import torch
from page_first_oracle import read_rows, write_rows


class MappedKernel:
    def __init__(self, memory, counts=None):
        # Tensor entries are physical arenas; (view, tensor) entries are the
        # existing independent, logically labelled page-first fixtures.
        self.memory, self.counts = memory, counts

    def __call__(self, sb, sr, sv, db, dr, dv, si, di, valid, width, blocks):
        if self.counts is not None:
            self.counts.extend(
                int((valid & (si >= begin) & (si < end)).sum()) for begin, end in sr[:2]
            )
        for source, target, enabled in zip(si.tolist(), di.tolist(), valid.tolist()):
            if not enabled:
                continue
            src = [
                (int(base), source - int(begin))
                for base, (begin, end), ok in zip(sb, sr, sv)
                if ok and int(begin) <= source < int(end)
            ]
            dst = [
                (int(base), target - int(begin))
                for base, (begin, end), ok in zip(db, dr, dv)
                if ok and int(begin) <= target < int(end)
            ]
            if not src or not dst:
                continue
            payload = self.read(*src[0], width)
            self.write(*dst[0], width, payload)

    def address(self, base, row, width):
        for address, tensor in self.memory.items():
            if isinstance(tensor, tuple):
                continue
            if address <= base < address + tensor.numel() * tensor.element_size():
                return tensor.data_ptr() + base - address + row * width
        return base + row * width

    def read(self, base, row, width):
        entry = self.memory.get(base)
        if isinstance(entry, tuple):
            view, tensor = entry
            return (
                read_rows(view, tensor, torch.tensor([row]), width)
                .contiguous()
                .view(torch.uint8)
                .numpy()
                .tobytes()
            )
        return ctypes.string_at(self.address(base, row, width), width)

    def write(self, base, row, width, payload):
        entry = self.memory.get(base)
        if isinstance(entry, tuple):
            view, tensor = entry
            values = torch.frombuffer(bytearray(payload), dtype=torch.bfloat16).reshape(
                1, -1
            )
            write_rows(view, tensor, torch.tensor([row]), width, values)
        else:
            ctypes.memmove(self.address(base, row, width), payload, width)
