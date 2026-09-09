#!/usr/bin/env python3
"""Compare Store-owned Fabric memory with torch pinned Host registration.

Run inside the prepared Ascend container, with the model server stopped:
    python workspace/setup/check_fabric_register.py

No model, downloads, remote node or existing master needed. Uses logical NPU 0
by default and about 3 GiB of Host memory (1 GiB Store segment, 1 GiB local
buffer, 1 GiB pinned tensor). --device and --timeout are supported.
--pinned-mib 1696 reproduces the buffer length in the reported HiCache failure;
the default 1024 deliberately uses a whole GiB to separate size from origin.

F3:BASE_OK,PIN_OK means setup/local put-get AND external registration passed.
F3:PIN_REGISTER_EXIT1 means baseline passed but external registration failed;
see probe.log in the printed Logs directory for the underlying error code.
PIN_ALLOC means allocation failed before registration. SETUP/PUTGET failures
mean the baseline failed; they do not establish a pinned-memory incompatibility.
CLOSE failures indicate cleanup trouble. A signal/timeout also counts as failure.

Registration success alone does not prove external-buffer transfers work, nor
does this local probe independently verify physical HCCS routing. Fabric mode
is requested; check this run's probe.log for 'Fabric mem mode is enabled'.
"""

from check_fabric_local import main


if __name__ == "__main__":
    raise SystemExit(main(registration=True))
