"""Mirror feasibility status and tracebacks to a predictable /tmp log."""

import sys
from pathlib import Path


class LogStream:
    def __init__(self, terminal, logfile):
        self.terminal = terminal
        self.logfile = logfile

    def write(self, message):
        self.terminal.write(message)
        self.logfile.write(message)
        self.flush()
        return len(message)

    def flush(self):
        self.terminal.flush()
        self.logfile.flush()

    def __getattr__(self, name):
        return getattr(self.terminal, name)


def enable_log(role: str, size: str) -> None:
    path = Path(f"/tmp/a3-kv-feasibility-{role}-{size}.log")
    logfile = path.open("w")
    sys.stdout = LogStream(sys.stdout, logfile)
    sys.stderr = LogStream(sys.stderr, logfile)
    print(f"LOG={path}", flush=True)
