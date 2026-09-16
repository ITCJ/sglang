"""Run the real Host constructor with allocation stubs; no torch/NPU required."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest


class TensorView:
    """Minimal strided view: bounds and pointer arithmetic, no data allocation."""

    def __init__(self, shape, strides=None, offset=0):
        self.shape = shape
        if strides is None:
            strides = []
            stride = 1
            for size in reversed(shape):
                strides.insert(0, stride)
                stride *= size
        self.strides = tuple(strides)
        self.offset = offset

    def transpose(self, a, b):
        shape, strides = list(self.shape), list(self.strides)
        shape[a], shape[b] = shape[b], shape[a]
        strides[a], strides[b] = strides[b], strides[a]
        return TensorView(tuple(shape), strides, self.offset)

    def __getitem__(self, i):
        if not 0 <= i < self.shape[0]:
            raise IndexError(i)
        return TensorView(self.shape[1:], self.strides[1:],
                          self.offset + i * self.strides[0])

    def data_ptr(self):
        return 4096 + self.offset * 2


class AllocationStub:
    def __init__(self, device_pool, ratio, host_size, page_size, layout, *args, **kwargs):
        self.device_pool = device_pool
        self.layout = layout
        self.layer_num = 61
        self.kv_buffer = TensorView((61, 2, 128, 1, 512) if layout == 'layer_first'
                                    else (2, 61, 128, 1, 512))

    def _init_write_back_staging_buffers(self):
        pass


def host_class():
    source = (Path(__file__).resolve().parents[2] /
              'python/sglang/srt/mem_cache/pool_host/mla.py')
    tree = ast.parse(source.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == 'MLATokenToKVPoolHost')
    # Execute the production constructor, replacing only allocations/dependencies.
    cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef)
                and n.name == '__init__']
    cls.bases = [ast.Name(id='AllocationStub', ctx=ast.Load())]
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[
        ast.alias(name='annotations')], level=0), cls], type_ignores=[])
    namespace = dict(AllocationStub=AllocationStub, _is_cuda=False, _is_hip=False,
                     torch=SimpleNamespace(tensor=lambda values, **kw: values,
                                           uint64='uint64'))
    exec(compile(ast.fix_missing_locations(module), str(source), 'exec'), namespace)
    return namespace['MLATokenToKVPoolHost']


class HostViewsTest(unittest.TestCase):
    def test_fewer_pages_than_layers_and_correct_layer_addresses(self):
        for layout in ('page_first_kv_split', 'page_first', 'layer_first'):
            with self.subTest(layout=layout):
                host = host_class()(SimpleNamespace(device='cpu'), 1, 0, 128, layout)
                self.assertEqual(len(host.data_refs), 61)
                for layer, view in enumerate(host.data_refs):
                    self.assertEqual(view.shape, (2, 128, 1, 512))
                    for page in range(2):
                        expected = ((layer * 2 + page) if layout == 'layer_first'
                                    else (page * 61 + layer)) * 128 * 512
                        self.assertEqual(view[page].data_ptr(), 4096 + 2 * expected)


if __name__ == '__main__':
    unittest.main()
