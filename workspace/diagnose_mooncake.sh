#!/usr/bin/env bash
# Read-only checks; detailed output is kept locally for follow-up.
set -uo pipefail

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "D2:P"
  exit 2
fi

"${PYTHON_BIN}" - "$@" <<'PY'
import ctypes
import glob
import importlib.metadata
import os
import platform
import resource
import re
import shutil
import subprocess
import tempfile

fd, log_path = tempfile.mkstemp(prefix="mooncake-diag-", suffix=".log")
log = os.fdopen(fd, "w")
codes = []
missing_dependencies = set()

def collect_missing(output):
    for match in re.finditer(
        r"([A-Za-z0-9_+.-]+\.so(?:\.[A-Za-z0-9_+.-]+)*)"
        r"(?:\s+=>\s+not found|: cannot open shared object file)", output
    ):
        missing_dependencies.add(match.group(1))

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

def load_code(error):
    # 0=OK, P=search path, F=file absent, L=broken link,
    # D=dependency absent, V=ABI/version, A=ELF/architecture, E=other.
    if "version" in error or "undefined symbol" in error:
        return "V"
    if any(term in error for term in ("ELF", "file too short", "Exec format")):
        return "A"
    if "cannot open shared object file" in error:
        return "D"
    return "E"

def detail(value):
    record("summary", value)

if "--native" in os.sys.argv[1:]:
    packages = {}
    for dist in importlib.metadata.distributions():
        name = (dist.metadata.get("Name") or "").lower().replace("_", "-")
        if "mooncake" in name or name in ("torch", "torch-npu", "numpy"):
            packages.setdefault(name, []).append(dist.version)
            record(name, f"versions={packages[name]} location={dist.locate_file('')}")
    npu_version = "+".join(packages.get("mooncake-transfer-engine-npu", ["NONE"]))
    count = sum(len(versions) for name, versions in packages.items()
                if name.startswith("mooncake-transfer-engine"))
    record("environment", str({key: os.environ.get(key, "") for key in
                              ("LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH")}))
    # Inspect distribution-owned binaries without importing the crashing module.
    for dist in importlib.metadata.distributions():
        if "mooncake" not in (dist.metadata.get("Name") or "").lower():
            continue
        for file in dist.files or ():
            if str(file).endswith(".so"):
                run(["ldd", str(dist.locate_file(file))])
    trace = "NO_GDB"
    if shutil.which("gdb"):
        source = ("import resource; resource.setrlimit(resource.RLIMIT_CORE, (0, 0)); "
                  "import mooncake.engine; print('IMPORT_DONE', flush=True)")
        _, output = run(["gdb", "--batch", "-nx", "-ex", "set pagination off",
                         "-ex", "run", "-ex", "bt 40", "--args",
                         os.sys.executable, "-c", source])
        # Prefer an owning library over generic libc abort/free frames.
        owners = re.findall(r"^#\d+.*?\bfrom\s+(\S+)", output, re.MULTILINE)
        owners = [os.path.basename(owner) for owner in owners
                  if not any(skip in os.path.basename(owner) for skip in
                             ("libc.so", "libpthread", "ld-linux", "libpython"))]
        trace = short(owners[0], 48) if owners else "SEE_LOG"
    report = f"N1:{npu_version} W={count} BT={trace}"
    record("report", report)
    log.close()
    print(report)
    raise SystemExit(0)

if "--isolate" in os.sys.argv[1:]:
    # Separate processes distinguish native aborts from Python import errors.
    probes = (
        "import mooncake.engine",
        "from mooncake.store import MooncakeDistributedStore",
        "import mooncake.mooncake_store_service",
        "import torch; import torch_npu",
        "import torch; import torch_npu; import mooncake.engine",
        "import mooncake.engine; from mooncake.store import MooncakeDistributedStore; import mooncake.mooncake_store_service",
    )
    results = []
    for probe in probes:
        # Prevent large core dumps; retain stderr and the exact return code.
        source = (
            "import resource; resource.setrlimit(resource.RLIMIT_CORE, (0, 0)); "
            + probe + "; print('IMPORT_DONE', flush=True)"
        )
        rc, output = run([os.sys.executable, "-c", source])
        status = ("0" if rc == 0 else "A" if rc == -6 else
                  "S" if rc == -11 else "T" if rc == -1 and "timed out" in output
                  else "E")
        # Lowercase means imports completed but the process failed at shutdown.
        results.append(status.lower() if "IMPORT_DONE" in output.splitlines() else status)
    versions = []
    for package in ("mooncake-transfer-engine-npu", "mooncake-transfer-engine",
                    "mooncake-transfer-engine-non-cuda", "torch", "torch-npu"):
        try:
            versions.append(package + "=" + importlib.metadata.version(package))
        except importlib.metadata.PackageNotFoundError:
            pass
    record("packages", "\n".join(versions))
    report = "X1:" + "".join(results)
    record("report", report)
    log.close()
    print(report)
    raise SystemExit(0)

record("environment", str({k: os.environ.get(k, "") for k in (
    "PATH", "LD_LIBRARY_PATH", "PYTHONPATH", "HOST_SGLANG_REPO",
    "ASCEND_ENABLE_USE_FABRIC_MEM", "HCCL_INTRA_ROCE_ENABLE")}))
detail(f"DIAG1 ARCH={platform.machine()} PY={platform.python_version()}")
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
        code = "0"
    except OSError as exc:
        result = "FAIL " + short(str(exc))
        code = load_code(str(exc))
        record(name + " load error", str(exc))
    absolute_ok = 0
    absolute_errors = []
    for path in sorted(real_files):
        run(["file", path])
        _, dependencies = run(["ldd", path])
        collect_missing(dependencies)
        try:
            ctypes.CDLL(path)
            absolute_ok += 1
            record(path + " absolute load", "OK")
        except OSError as exc:
            absolute_errors.append(str(exc))
            collect_missing(str(exc))
            record(path + " absolute load", str(exc))
            if "FAIL" in result and "cannot open shared object file" in result:
                result += " ABS=" + short(str(exc), 160)
    if code != "0":
        if absolute_ok:
            code = "P"
        elif absolute_errors:
            code = load_code(absolute_errors[0])
        elif not real_files and code == "D":
            code = "L" if broken else "F"
    codes.append(code)
    detail(f"{name}: {result} [files={len(real_files)} broken={broken} abs_ok={absolute_ok}]")

try:
    version = importlib.metadata.version("mooncake-transfer-engine-npu")
except importlib.metadata.PackageNotFoundError:
    version = "NOT_INSTALLED"
rc, output = run([os.sys.executable, "-c",
    "import mooncake.engine; from mooncake.store import MooncakeDistributedStore; "
    "import mooncake.mooncake_store_service; print('OK')"])
error = output.strip().splitlines()[-1] if output.strip() else f"exit={rc}"
import_report = "OK"
if rc != 0:
    missing_module = re.search(r"No module named ['\"]([^'\"]+)['\"]", error)
    missing_library = re.search(
        r"([A-Za-z0-9_+.-]+\.so(?:\.[A-Za-z0-9_+.-]+)*): cannot open shared object file",
        error,
    )
    if missing_module:
        import_report = "MODULE:" + missing_module.group(1)
    elif missing_library:
        import_report = "LIB:" + missing_library.group(1)
    else:
        import_report = short(error, 72)
codes.append("0" if rc == 0 else ("F" if version == "NOT_INSTALLED" else "E"))
codes.append("0" if shutil.which("mooncake_master") else "F")
detail(f"MOONCAKE={version} IMPORT={'OK' if rc == 0 else short(error)} MASTER={'YES' if shutil.which('mooncake_master') else 'NO'}")

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
codes.append("0" if devices else "F")
codes.append("0" if uverbs else "F")
found = re.search(r"\b([0-9]+) HCAs? found", output)
codes.append("T" if not shutil.which("ibv_devinfo") else
             "E" if rc != 0 else
             "0" if found and int(found.group(1)) > 0 else
             "F" if found else "U")
detail(f"RDMA sys={len(devices)} uverbs={len(uverbs)} ibv={verbs} ports={short(','.join(ports) or 'NONE', 300)}")
run(["ibv_devinfo"])
run(["rdma", "link", "show"])
run(["ip", "-brief", "address"])
rc, _ = run(["npu-smi", "info"])
memlock = resource.getrlimit(resource.RLIMIT_MEMLOCK)[0]
codes.append("0" if rc == 0 else "E")
codes.append("0" if memlock == resource.RLIM_INFINITY else "L")
detail(f"NPU_SMI={'OK' if rc == 0 else 'FAIL'} MEMLOCK={'unlimited' if memlock == resource.RLIM_INFINITY else str(memlock) + 'B'}")
record("code order", "ibverbs rdmacm mooncake master sysfs uverbs ibv npu memlock")
record("report", "D2:" + "".join(codes))
log.close()
if "--import" in os.sys.argv[1:]:
    print("MC:" + import_report)
elif "--deps" in os.sys.argv[1:]:
    print("DEP:" + (",".join(sorted(missing_dependencies)) or "NONE"))
else:
    print("D2:" + "".join(codes))
PY
