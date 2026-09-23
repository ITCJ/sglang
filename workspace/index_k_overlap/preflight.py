"""Target-machine checks before expensive model loading; not a benchmark."""

import argparse
import importlib
import json
import os
import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--devices", type=int, required=True)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    report = {"status": "checking", "python": sys.executable, "versions": {}}
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    try:
        for module in ("torch", "torch_npu", "aiohttp", "transformers"):
            importlib.import_module(module)
        import torch

        for package in ("torch", "torch-npu", "sglang", "aiohttp", "transformers"):
            try:
                report["versions"][package] = metadata.version(package)
            except metadata.PackageNotFoundError:
                report["versions"][package] = "unregistered"
        report["visible_devices"] = torch.npu.device_count()
        if report["visible_devices"] < args.devices:
            raise RuntimeError(f"Need {args.devices} logical NPUs, visible: {report['visible_devices']}")
        model = Path(args.model_path)
        if not (model / "config.json").is_file():
            raise RuntimeError(f"Expected local model directory with config.json: {model}")
        config = json.loads((model / "config.json").read_text())
        report["model_config"] = {
            name: config.get(name) for name in
            ("architectures", "num_hidden_layers", "index_head_dim", "kv_lora_rank", "qk_rope_head_dim")
        }
        for name, expected in (("num_hidden_layers", 61), ("index_head_dim", 128),
                               ("kv_lora_rank", 512), ("qk_rope_head_dim", 64)):
            if config.get(name) != expected:
                raise RuntimeError(f"DSv3.2 byte estimate assumes {name}={expected}, got {config.get(name)}")
        report["environment"] = {
            name: os.environ.get(name) for name in
            ("ASCEND_RT_VISIBLE_DEVICES", "ASCEND_HOME_PATH", "HCCL_SOCKET_IFNAME",
             "SGLANG_PROFILE_V2", "SGLANG_NPU_USE_MLAPO", "SGLANG_NPU_USE_MULTI_STREAM")
        }
        # Exercise only the installed entrypoint's argument parser, never load
        # model weights. Persist help so version-specific failures are visible.
        help_result = subprocess.run(
            [sys.executable, "-m", "sglang.launch_server", "--help"],
            capture_output=True, text=True, timeout=120, check=False,
        )
        help_text = help_result.stdout + help_result.stderr
        (output / "server_cli_help.txt").write_text(help_text)
        if help_result.returncode:
            raise RuntimeError("Installed server --help failed; inspect server_cli_help.txt")
        for flag in ("--enable-profile-cuda-graph", "--cuda-graph-bs", "--kv-cache-dtype",
                     "--max-running-requests", "--decode-log-interval", "--quantization",
                     "--max-prefill-tokens", "--disable-shared-experts-fusion",
                     "--enable-dp-attention", "--enable-dp-lm-head"):
            if not re.search(re.escape(flag) + r"(?=[\s,=\]])", help_text):
                raise RuntimeError(f"Installed server does not advertise {flag}")
        report["status"] = "ok"
        print(json.dumps(report, indent=2), flush=True)
    except BaseException as exc:
        report["status"] = "failed"
        report["error"] = repr(exc)
        raise
    finally:
        (output / "preflight.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
