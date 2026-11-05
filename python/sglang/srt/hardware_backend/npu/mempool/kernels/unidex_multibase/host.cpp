// Copyright (c) 2026 Huawei Technologies Co., Ltd
// All rights reserved.
//
// Licensed under the BSD 3-Clause License  (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#include "torch_helper.h"
#include "aclrtlaunch_unidex_copy_multibase.h"

#include <cstdint>
#include <limits>

namespace sglang {
namespace npu_kernel {
namespace {

void CheckTensor(const at::Tensor &tensor, const at::Tensor &reference,
                 const char *name, bool mask = false)
{
    TORCH_CHECK(tensor.device().type() == at::DeviceType::PrivateUse1, name, " must be on an NPU device");
    TORCH_CHECK(tensor.device() == reference.device(), name, " must be on the same device as src_bases");
    TORCH_CHECK(tensor.is_contiguous(), name, " must be contiguous");
    if (mask) {
        TORCH_CHECK(tensor.scalar_type() == at::kBool || tensor.scalar_type() == at::kByte,
                    name, " must be bool or uint8");
    } else {
        TORCH_CHECK(tensor.scalar_type() == at::kLong, name, " must be int64");
    }
}

void CheckBaseShapes(const at::Tensor &bases, const at::Tensor &ranges,
                     const at::Tensor &valid, const char *side)
{
    TORCH_CHECK(bases.dim() == 1, side, "_bases must be 1-D");
    TORCH_CHECK(ranges.dim() == 2 && ranges.size(0) == bases.numel() && ranges.size(1) == 2,
                side, "_ranges must have shape [base_capacity, 2]");
    TORCH_CHECK(valid.dim() == 1 && valid.numel() == bases.numel(),
                side, "_base_valid must have shape [base_capacity]");
}

}  // namespace

static void unidex_copy_multibase(const at::Tensor &src_bases, const at::Tensor &src_ranges,
                          const at::Tensor &src_base_valid, const at::Tensor &dst_bases,
                          const at::Tensor &dst_ranges, const at::Tensor &dst_base_valid,
                          const at::Tensor &src_index, const at::Tensor &dst_index,
                          const at::Tensor &valid_mask, int64_t block_bytes, int64_t block_dim)
{
    CheckTensor(src_bases, src_bases, "src_bases");
    CheckTensor(src_ranges, src_bases, "src_ranges");
    CheckTensor(src_base_valid, src_bases, "src_base_valid", true);
    CheckTensor(dst_bases, src_bases, "dst_bases");
    CheckTensor(dst_ranges, src_bases, "dst_ranges");
    CheckTensor(dst_base_valid, src_bases, "dst_base_valid", true);
    CheckTensor(src_index, src_bases, "src_index");
    CheckTensor(dst_index, src_bases, "dst_index");
    CheckTensor(valid_mask, src_bases, "valid_mask", true);
    CheckBaseShapes(src_bases, src_ranges, src_base_valid, "src");
    CheckBaseShapes(dst_bases, dst_ranges, dst_base_valid, "dst");
    TORCH_CHECK(src_index.dim() == 1 && dst_index.dim() == 1 && valid_mask.dim() == 1,
                "src_index, dst_index, and valid_mask must be 1-D");
    TORCH_CHECK(src_index.numel() == dst_index.numel() && src_index.numel() == valid_mask.numel(),
                "src_index, dst_index, and valid_mask must have the same length");
    TORCH_CHECK(block_bytes > 0 && block_bytes <= 32 * 1024, "block_bytes must be in [1, 32768]");
    TORCH_CHECK(block_dim > 0 && block_dim <= std::numeric_limits<uint32_t>::max(),
                "block_dim must be positive and fit uint32");
    int32_t currentDevice = -1;
    c10_npu::GetDevice(&currentDevice);
    TORCH_CHECK(src_bases.device().index() == currentDevice, "metadata must be on the current NPU device");

    uint64_t srcCapacity = src_bases.numel();
    uint64_t dstCapacity = dst_bases.numel();
    uint64_t maxCopy = src_index.numel();
    if (maxCopy == 0 || srcCapacity == 0 || dstCapacity == 0) {
        return;
    }
    auto stream = c10_npu::getCurrentNPUStream();
    for (const auto &tensor : {src_bases, src_ranges, src_base_valid, dst_bases, dst_ranges,
                              dst_base_valid, src_index, dst_index, valid_mask}) {
        tensor.record_stream(stream);
    }
    uint32_t blockBytes = block_bytes;
    uint32_t blockDim = block_dim;
    const auto args = TorchNpuHelper::ConvertTypes(src_bases, src_ranges, src_base_valid,
                                                  dst_bases, dst_ranges, dst_base_valid,
                                                  src_index, dst_index, valid_mask,
                                                  srcCapacity, dstCapacity, blockBytes, maxCopy);
    const auto aclStream = stream.stream(false);
    at_npu::native::OpCommand::RunOpApi("unidex_copy_multibase", [=]() -> int {
        return std::apply([=](auto... params) {
            return ACLRT_LAUNCH_KERNEL(unidex_copy_multibase)(blockDim, aclStream, params...);
        }, args);
    });
}

}  // namespace npu_kernel
}  // namespace sglang

TORCH_LIBRARY_FRAGMENT(npu, m)
{
    m.def(torch::schema(
        "unidex_copy_multibase(Tensor src_bases, Tensor src_ranges, Tensor src_base_valid, "
        "Tensor dst_bases, Tensor dst_ranges, Tensor dst_base_valid, "
        "Tensor src_index, Tensor dst_index, Tensor valid_mask, "
        "int block_bytes, int block_dim=8) -> ()",
        c10::AliasAnalysisKind::CONSERVATIVE));
}

TORCH_LIBRARY_IMPL(npu, PrivateUse1, m)
{
    m.impl("unidex_copy_multibase", TORCH_FN(sglang::npu_kernel::unidex_copy_multibase));
}
