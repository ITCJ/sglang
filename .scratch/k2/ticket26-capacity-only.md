# Ticket26：017通过后的018容量拒绝检查

017双侧target通过，reference沿用015；完整验收仍incomplete。
本轮只跑capacity：人为限制一个rank的注册容量，检查Router拒绝请求、worker退出及双侧清理。
不会运行reference/target/timeout，也不执行成功生成请求；仍需各侧启动一次模型服务。
用户确认31/32资源清理后再运行；不重编译插件。使用015原配置600秒，保留017长生成1800秒记录。

## 32容器：读取017缓存淘汰证据，不启动模型

```bash
cd /home/cryang/sglang &&
python3 - <<'PY'
import json, sys
from pathlib import Path
sys.path.insert(0, 'ascend-mempool-test/scripts')
from run_ticket26 import cache_eviction
root = Path('/home/cryang/log/k2/k2-ticket26-017-target/decode/target')
report = cache_eviction(root)
(root / 'cache-eviction.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))
raise SystemExit(0 if report['status'] == 'passed' else 1)
PY
```

该专项不由TARGET_PASSED自动证明；回传status及报告。缺失/失败保留待定位，不重跑已过服务。

## 两侧容器准备

```bash
cd /home/cryang/sglang &&
git pull --ff-only &&
mkdir -p /home/cryang/log/k2/k2-ticket26-018-capacity &&
cp /home/cryang/log/k2/k2-ticket26-015-reference/config.json \
   /home/cryang/log/k2/k2-ticket26-018-capacity/config.json
```

## 31容器先启动

```bash
cd /home/cryang/sglang &&
SGLANG_NPU_MEMPOOL_INDEX_DIAG=0 python3 -u ascend-mempool-test/scripts/run_mempool_min.py \
  --role prefill --mode full-acceptance \
  --config /home/cryang/log/k2/k2-ticket26-018-capacity/config.json \
  --log-root /home/cryang/log/k2 --run-id k2-ticket26-018-capacity \
  --capacity-only
```

## 32容器随后启动

```bash
cd /home/cryang/sglang &&
SGLANG_NPU_MEMPOOL_INDEX_DIAG=0 python3 -u ascend-mempool-test/scripts/run_mempool_min.py \
  --role decode --mode full-acceptance \
  --config /home/cryang/log/k2/k2-ticket26-018-capacity/config.json \
  --log-root /home/cryang/log/k2 --run-id k2-ticket26-018-capacity \
  --capacity-only
```

预期两侧CAPACITY_PASSED，summary的stopped_after=capacity，full_acceptance=incomplete。
容量限制导致的worker退出是此项测试输入，只有最终标记和evidence检查通过才算成功。
回传018各角色summary.json、capacity/evidence.json、capacity/process-cleanup.json。
失败回传31 capacity/router.log与双侧capacity/launcher.log；脚本返回非零，不自动进入timeout。

## 文件与本地验证

功能源码：Ascend/NPU、通用主线均无修改。
脚本run_mempool_min.py新增互斥capacity-only，复用既有容量拒绝及清理路径。
测试test_service_resume.py验证双侧仅运行容量检查、失败传播及reference选项互斥。
两项定向CPU检查通过；成对清理测试因沙箱限制失败后，沙箱外第二轮通过。
静态检查通过，Standards 0、Spec 0未解决。CPU结果不替代018硬件结果。
