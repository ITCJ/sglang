"""Keep detailed output in /tmp and show only short result codes."""

import os
import sys
from pathlib import Path


class LogStream:
    def __init__(self, terminal, logfile):
        self.terminal = terminal
        self.logfile = logfile
        self.pending = ""

    def write(self, message):
        self.logfile.write(message)
        self.pending += message
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            if line in {"S0", "S1", "H0", "H1", "D0", "D1", "P0", "P1", "F1", "F2", "F3", "F4", "F5", "F6", "F9"}:
                self.terminal.write(line + "\n")
        self.flush()
        return len(message)

    def flush(self):
        self.terminal.flush()
        self.logfile.flush()

    def fileno(self):
        return self.logfile.fileno()

    def __getattr__(self, name):
        return getattr(self.logfile, name)


def print_result(message: str) -> None:
    """Emit an intentional compact result, bypassing the detail-log filter."""
    if isinstance(sys.stdout, LogStream):
        sys.stdout.logfile.write(message + "\n")
        sys.stdout.terminal.write(message + "\n")
        sys.stdout.flush()
    else:
        print(message, flush=True)


def enable_log(role: str, size: str, path: Path | None = None) -> None:
    path = path or Path(f"/tmp/a3-kv-feasibility-{role}-{size}.log")
    sys.stdout.flush()
    sys.stderr.flush()
    terminal = os.fdopen(os.dup(1), "w", buffering=1)
    logfile = path.open("w", buffering=1)
    # Capture native libraries and subprocesses that bypass Python's streams.
    os.dup2(logfile.fileno(), 1)
    os.dup2(logfile.fileno(), 2)
    sys.stdout = LogStream(terminal, logfile)
    sys.stderr = LogStream(terminal, logfile)
    print(f"LOG={path}", flush=True)
