"""Keep detailed output in /tmp and show only short result codes."""

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
            if line in {"S0", "S1", "P0", "P1", "F1", "F2", "F3", "F4", "F5", "F6", "F9"}:
                self.terminal.write(line + "\n")
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
