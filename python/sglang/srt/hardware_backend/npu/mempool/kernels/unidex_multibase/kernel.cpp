/**
 * Masked global-row copies with 64-bit addresses and two-slot FIFO buffering.
 * A task must match exactly one enabled, well-formed range on each side.
 * The caller owns allocation bounds, accessibility and non-aliasing guarantees.
 */
#include "kernel_operator.h"

using CopyUnit = uint8_t;
constexpr uint32_t BUFFER_NUM = 2;

class KernelUniDexCopy
{
public:
    __aicore__ inline void Init(GM_ADDR src_bases, GM_ADDR src_ranges, GM_ADDR src_base_valid,
                                GM_ADDR dst_bases, GM_ADDR dst_ranges, GM_ADDR dst_base_valid,
                                GM_ADDR src_index, GM_ADDR dst_index, GM_ADDR valid_mask,
                                uint64_t srcCapacity, uint64_t dstCapacity,
                                uint32_t blockBytes, uint64_t maxCopy, AscendC::TPipe *pipe)
    {
        this->srcCapacity = srcCapacity;
        this->dstCapacity = dstCapacity;
        this->blockBytes = blockBytes;
        this->maxCopy = maxCopy;
        const uint64_t blockNum = AscendC::GetBlockNum();
        copyRowsPerCore = maxCopy / blockNum + (maxCopy % blockNum != 0);
        srcBases.SetGlobalBuffer((__gm__ int64_t *)src_bases, srcCapacity);
        srcRanges.SetGlobalBuffer((__gm__ int64_t *)src_ranges, srcCapacity * 2);
        srcBaseValid.SetGlobalBuffer((__gm__ uint8_t *)src_base_valid, srcCapacity);
        dstBases.SetGlobalBuffer((__gm__ int64_t *)dst_bases, dstCapacity);
        dstRanges.SetGlobalBuffer((__gm__ int64_t *)dst_ranges, dstCapacity * 2);
        dstBaseValid.SetGlobalBuffer((__gm__ uint8_t *)dst_base_valid, dstCapacity);
        srcIndex.SetGlobalBuffer((__gm__ int64_t *)src_index, maxCopy);
        dstIndex.SetGlobalBuffer((__gm__ int64_t *)dst_index, maxCopy);
        validMask.SetGlobalBuffer((__gm__ uint8_t *)valid_mask, maxCopy);
        pipe->InitBuffer(copyQue, BUFFER_NUM, (blockBytes + 31U) & ~31U);
    }

    __aicore__ inline void Process()
    {
        const uint64_t begin = static_cast<uint64_t>(AscendC::GetBlockIdx()) * copyRowsPerCore;
        uint64_t end = begin + copyRowsPerCore;
        if (end > maxCopy) {
            end = maxCopy;
        }
        if (begin >= end) {
            return;
        }
        const int64_t srcOrdered = OrderedPrefix(srcRanges, srcBaseValid, srcCapacity);
        const int64_t dstOrdered = OrderedPrefix(dstRanges, dstBaseValid, dstCapacity);
        if (srcOrdered == 0 || dstOrdered == 0) {
            return;
        }
        uint64_t dstAddresses[BUFFER_NUM] = {0, 0};
        uint32_t head = 0, tail = 0, queued = 0;
        for (uint64_t i = begin; i < end; ++i) {
            if (validMask.GetValue(i) == 0) {
                continue;
            }
            uint64_t srcAddress = 0, dstAddress = 0;
            if (!Resolve(srcIndex.GetValue(i), srcBases, srcRanges, srcBaseValid, srcCapacity, srcOrdered, srcAddress) ||
                !Resolve(dstIndex.GetValue(i), dstBases, dstRanges, dstBaseValid, dstCapacity, dstOrdered, dstAddress)) {
                continue;
            }
            if (queued == BUFFER_NUM) {
                CopyOut(dstAddresses[head]);
                head = (head + 1) % BUFFER_NUM;
                --queued;
            }
            CopyIn(srcAddress);
            dstAddresses[tail] = dstAddress;
            tail = (tail + 1) % BUFFER_NUM;
            ++queued;
        }
        while (queued > 0) {
            CopyOut(dstAddresses[head]);
            head = (head + 1) % BUFFER_NUM;
            --queued;
        }
    }

private:
    // A replay may change every entry. Prove uniqueness once per active core,
    // never on the host or across replays. -1 retains the general lookup.
    __aicore__ inline int64_t OrderedPrefix(AscendC::GlobalTensor<int64_t> &ranges,
                                           AscendC::GlobalTensor<uint8_t> &valid, uint64_t capacity)
    {
        int64_t count = 0, previousEnd = 0;
        for (uint64_t b = 0; b < capacity; ++b) {
            if (!valid.GetValue(b)) {
                continue;
            }
            if (b != static_cast<uint64_t>(count)) {
                return -1;
            }
            const int64_t begin = ranges.GetValue(2 * b);
            const int64_t end = ranges.GetValue(2 * b + 1);
            if (begin < previousEnd || begin >= end) {
                return -1;
            }
            previousEnd = end;
            ++count;
        }
        return count;
    }

    __aicore__ inline bool RowAddress(int64_t rawBase, int64_t local, uint64_t &address)
    {
        if (rawBase <= 0) {
            return false;
        }
        const uint64_t base = static_cast<uint64_t>(rawBase);
        const uint64_t localRow = static_cast<uint64_t>(local);
        const uint64_t maxAddress = ~static_cast<uint64_t>(0);
        if (localRow > (maxAddress - base - (blockBytes - 1)) / blockBytes) {
            return false;
        }
        address = base + localRow * static_cast<uint64_t>(blockBytes);
        return true;
    }

    __aicore__ inline bool Resolve(int64_t row, AscendC::GlobalTensor<int64_t> &bases,
                                   AscendC::GlobalTensor<int64_t> &ranges,
                                   AscendC::GlobalTensor<uint8_t> &valid, uint64_t capacity,
                                   int64_t ordered, uint64_t &address)
    {
        if (row < 0) {
            return false;
        }
        if (ordered >= 0) {
            int64_t lo = 0, hi = ordered;
            while (lo < hi) {
                const int64_t mid = lo + (hi - lo) / 2;
                const int64_t begin = ranges.GetValue(2 * mid);
                if (row < begin) {
                    hi = mid;
                } else if (row >= ranges.GetValue(2 * mid + 1)) {
                    lo = mid + 1;
                } else {
                    return RowAddress(bases.GetValue(mid), row - begin, address);
                }
            }
            return false;
        }
        // Unordered or overlapping ranges may still have uniquely mapped rows.
        bool found = false;
        for (uint64_t b = 0; b < capacity; ++b) {
            if (!valid.GetValue(b)) {
                continue;
            }
            const int64_t begin = ranges.GetValue(2 * b);
            const int64_t end = ranges.GetValue(2 * b + 1);
            if (begin < 0 || row < begin || row >= end) {
                continue;
            }
            if (found || !RowAddress(bases.GetValue(b), row - begin, address)) {
                return false;
            }
            found = true;
        }
        return found;
    }

    __aicore__ inline void CopyIn(uint64_t address)
    {
        AscendC::GlobalTensor<CopyUnit> src;
        src.SetGlobalBuffer(reinterpret_cast<__gm__ CopyUnit *>(address), blockBytes);
        auto local = copyQue.AllocTensor<CopyUnit>();
        AscendC::DataCopyExtParams params{1, blockBytes, 0, 0, 0};
        AscendC::DataCopyPadExtParams<CopyUnit> padding{false, 0, 0, 0};
        AscendC::DataCopyPad(local, src, params, padding);
        copyQue.EnQue(local);
    }

    __aicore__ inline void CopyOut(uint64_t address)
    {
        AscendC::GlobalTensor<CopyUnit> dst;
        dst.SetGlobalBuffer(reinterpret_cast<__gm__ CopyUnit *>(address), blockBytes);
        auto local = copyQue.DeQue<CopyUnit>();
        AscendC::DataCopyExtParams params{1, blockBytes, 0, 0, 0};
        AscendC::DataCopyPad(dst, local, params);
        copyQue.FreeTensor(local);
    }

    AscendC::TQueBind<AscendC::TPosition::VECIN, AscendC::TPosition::VECOUT, BUFFER_NUM> copyQue;
    AscendC::GlobalTensor<int64_t> srcBases, srcRanges, dstBases, dstRanges, srcIndex, dstIndex;
    AscendC::GlobalTensor<uint8_t> srcBaseValid, dstBaseValid, validMask;
    uint64_t srcCapacity = 0, dstCapacity = 0, maxCopy = 0, copyRowsPerCore = 0;
    uint32_t blockBytes = 0;
};

extern "C" __global__ __aicore__ void unidex_copy_multibase(
    GM_ADDR src_bases, GM_ADDR src_ranges, GM_ADDR src_base_valid,
    GM_ADDR dst_bases, GM_ADDR dst_ranges, GM_ADDR dst_base_valid,
    GM_ADDR src_index, GM_ADDR dst_index, GM_ADDR valid_mask,
    uint64_t srcCapacity, uint64_t dstCapacity, uint32_t blockBytes, uint64_t maxCopy)
{
    AscendC::TPipe pipe;
    KernelUniDexCopy kernel;
    kernel.Init(src_bases, src_ranges, src_base_valid, dst_bases, dst_ranges, dst_base_valid,
                src_index, dst_index, valid_mask, srcCapacity, dstCapacity, blockBytes, maxCopy, &pipe);
    kernel.Process();
}
