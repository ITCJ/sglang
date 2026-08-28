#单机图模式
echo performance | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor
sysctl -w vm.swappiness=0
sysctl -w kernel.numa_balancing=0
sysctl -w kernel.sched_migration_cost_ns=50000

export SGLANG_SET_CPU_AFFINITY=1

unset https_proxy
unset http_proxy
unset HTTPS_PROXY
unset HTTP_PROXY
unset ASCEND_LAUNCH_BLOCKING
source /usr/local/Ascend/ascend-toolkit/set_env.sh
source /usr/local/Ascend/nnal/atb/set_env.sh
export PATH=/usr/local/Ascend/8.5.0/compiler/bishengir/bin:$PATH
# 内存碎片
export SGLANG_NPU_PROFILING=0
# export SGLANG_NPU_PROFILING=1
# export SGLANG_NPU_PROFILING_BS=4
# export SGLANG_NPU_PROFILING_STAGE="decode"

export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True
export STREAMS_PER_DEVICE=32
MODEL_PATH=/home/caofei/DeepSeek-V3.2-Exp-w8a8

# enable mlapo
export SGLANG_NPU_USE_MLAPO=1
#export SGLANG_USE_FIA_NZ=1
export SGLANG_NPU_USE_MULTI_STREAM=1
export HCCL_BUFFSIZE=1600
#export SGLANG_ENABLE_OVERLAP_PLAN_STREAM=1
#export SGLANG_ENABLE_SPEC_V2=1
export HCCL_OP_EXPANSION_MODE=AIV

IPs=('127.0.0.1')

# get IP in current node
LOCAL_HOST=127.0.0.1
# get node index
for i in "${!IPs[@]}";
do
    echo "LOCAL_HOST=${LOCAL_HOST}, IPs[${i}]=${IPs[$i]}"
    if [ "$LOCAL_HOST" == "${IPs[$i]}" ]; then
        echo "Node Rank : ${i}"
        VC_TASK_INDEX=$i
        break
    fi
done
nnodes=${#IPs[@]}
tp_size=`expr 16 \* ${nnodes}`

export HCCL_SOCKET_IFNAME=lo
export GLOO_SOCKET_IFNAME=lo
python3 -m sglang.launch_server --model-path ${MODEL_PATH} \
    --tp-size $tp_size --dp-size 1 \
    --trust-remote-code \
    --attention-backend ascend \
    --device npu \
    --watchdog-timeout 9000 \
    --host ${IPs[$VC_TASK_INDEX]} --port 6699 \
    --mem-fraction-static 0.80 \
    --disable-radix-cache --chunked-prefill-size -1 --max-prefill-tokens 512 --context-length 256 \
    --max-running-requests 4 \
    --quantization modelsim \
    --disable-cuda-graph \
    --nnodes $nnodes --node-rank $VC_TASK_INDEX \
    --enable-dp-attention --disable-shared-experts-fusion --dtype bfloat16 \
    --enable-dp-lm-head

    # --moe-a2a-backend deepep --deepep-mode auto
    # --disable-radix-cache --chunked-prefill-size -1 --max-prefill-tokens 8192 --context-length 4096 \