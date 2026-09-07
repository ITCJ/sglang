#!/usr/bin/env bash
# Read-only checks; detailed output is kept locally for follow-up.
set -uo pipefail

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "DIAG1 PYTHON=NOT_FOUND"
  exit 2
fi

"${PYTHON_BIN}" - <<'PY'
import ctypes
import glob
import importlib.metadata
import os
import platform
import resource
import shutil
import subprocess
import tempfile

fd, log_path = tempfile.mkstemp(prefix="mooncake-diag-", suffix=".log")
log = os.fdopen(fd, "w")

def record(title, value):
    log.write(f"\n## {title}\n{value}\n")
    log.flush()

def run(args):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=15)
        output = p.stdout + p.stderr
        record(" ".join(args), f"exit={p.returncode}\n{output}")
        return p.returncode, output
    except (OSError, subprocess.TimeoutExpired) as exc:
        record(" ".join(args), str(exc))
        return -1, str(exc)

def short(value, limit=240):
    value = " ".join(value.split())
    return value if len(value) <= limit else value[:limit] + "..."

record("environment", str({k: os.environ.get(k, "") for k in (
    "PATH", "LD_LIBRARY_PATH", "PYTHONPATH", "HOST_SGLANG_REPO",
    "ASCEND_ENABLE_USE_FABRIC_MEM", "HCCL_INTRA_ROCE_ENABLE")}))
print(f"DIAG1 ARCH={platform.machine()} PY={platform.python_version()}")
run(["uname", "-a"])
run(["ldconfig", "-p"])
for path in ("/etc/os-release", "/etc/ld.so.conf",
             "/usr/local/Ascend/driver/version.info",
             "/usr/local/Ascend/ascend-toolkit/latest/version.cfg") + tuple(glob.glob("/etc/ld.so.conf.d/*")):
    try:
        with open(path) as f:
            record(path, f.read())
    except OSError:
        pass

for name in ("libibverbs.so.1", "librdmacm.so.1"):
    candidates = set()
    for directory in ("/usr/lib64", "/lib64", "/usr/lib", "/lib",
                      "/usr/lib/aarch64-linux-gnu", "/lib/aarch64-linux-gnu",
                      "/usr/lib/x86_64-linux-gnu", "/lib/x86_64-linux-gnu",
                      "/usr/local/lib"):
        candidates.update(glob.glob(f"{directory}/{name}*"))
    real_files = set()
    broken = 0
    for path in sorted(candidates):
        exists = os.path.exists(path)
        broken += int(not exists)
        record(path, f"realpath={os.path.realpath(path)} exists={exists}")
        if exists:
            real_files.add(os.path.realpath(path))
    try:
        ctypes.CDLL(name)
        result = "OK"
    except OSError as exc:
        result = "FAIL " + short(str(exc))
        record(name + " load error", str(exc))
    absolute_ok = 0
    for path in sorted(real_files):
        run(["file", path])
        run(["ldd", path])
        try:
            ctypes.CDLL(path)
            absolute_ok += 1
            record(path + " absolute load", "OK")
        except OSError as exc:
            record(path + " absolute load", str(exc))
            if "FAIL" in result and "cannot open shared object file" in result:
                result += " ABS=" + short(str(exc), 160)
    print(f"{name}: {result} [files={len(real_files)} broken={broken} abs_ok={absolute_ok}]")

try:
    version = importlib.metadata.version("mooncake-transfer-engine-npu")
except importlib.metadata.PackageNotFoundError:
    version = "NOT_INSTALLED"
rc, output = run([os.sys.executable, "-c",
    "import mooncake.engine; from mooncake.store import MooncakeDistributedStore; "
    "import mooncake.mooncake_store_service; print('OK')"])
error = output.strip().splitlines()[-1] if output.strip() else f"exit={rc}"
print(f"MOONCAKE={version} IMPORT={'OK' if rc == 0 else short(error)} MASTER={'YES' if shutil.which('mooncake_master') else 'NO'}")

devices = sorted(glob.glob("/sys/class/infiniband/*"))
ports = []
for device in devices:
    for port in sorted(glob.glob(device + "/ports/*")):
        values = []
        for field in ("state", "link_layer"):
            try:
                with open(port + "/" + field) as f:
                    values.append(f.read().strip())
            except OSError:
                values.append("?")
        ports.append(os.path.basename(device) + "/" + os.path.basename(port) + ":" + "/".join(values))
uverbs = glob.glob("/dev/infiniband/uverbs*")
rc, output = run(["ibv_devinfo", "-l"])
verbs = "TOOL_MISSING" if not shutil.which("ibv_devinfo") else ("OK" if rc == 0 else "FAIL")
print(f"RDMA sys={len(devices)} uverbs={len(uverbs)} ibv={verbs} ports={short(','.join(ports) or 'NONE', 300)}")
run(["ibv_devinfo"])
run(["rdma", "link", "show"])
run(["ip", "-brief", "address"])
rc, _ = run(["npu-smi", "info"])
memlock = resource.getrlimit(resource.RLIMIT_MEMLOCK)[0]
print(f"NPU_SMI={'OK' if rc == 0 else 'FAIL'} MEMLOCK={'unlimited' if memlock == resource.RLIM_INFINITY else str(memlock) + 'B'}")
log.close()
print(f"LOG={log_path}")
PY
