# CANN 9.0 two-replay regression

Source: user's `k2-ticket13-001/graph-prefill`, captured on 31 on 2026-11-03.
The test keeps actual device tick records, clock frequency, capture stream mapping
and exported MSTX events. The source archive SHA256 is recorded in
`.scratch/k2/verification/ticket13-cann90-log-fix-20261103/checks.json`.

Host records retain only format/type, range ID and message fields; process,
thread, host timestamps, domain and all unused/reserved memory are zeroed.
Capture records retain mapping fields with PID and host timestamp zeroed.
Profiler and hardware metadata contain only versions and device clock frequency.
Trace contains only MSTX events and thread labels, without kernel/model contents.

Four graph range IDs each have four device endpoints. CANN 9.0 export merges
each into one first-to-last interval; recovery must produce two actual ranges.
Removing the final endpoint must fail, not silently shorten the measurement.
