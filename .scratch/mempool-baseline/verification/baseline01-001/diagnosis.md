# baseline01-001: user-reported NPU failure

Evidence source: user pasted P/D terminal output, P summary/report excerpts,
the complete reference B response, and B responses from rounds 1–4. The NPU
archive itself is not available in this local workspace. This is a failure
analysis, not a passed baseline report.

## Executed coverage and answers

The runner completed the independent ABCE reference phase. Each of target
rounds 1–4 reached B and failed its full-output comparison. By the sequential
runner's control flow, A passed its lightweight checks before each B. Target
C/E were skipped after B failed. Round 5 failed during readiness and produced
no outcome.json. Round 1 was warmup; at most three measured A requests remain,
and no measured B/C/E request passed the required checks.

The independent fixture expects:

| Case | Input tokens | Expected answer | Evidence in this exchange |
| --- | ---: | --- | --- |
| A | 4096 | harbor / 22 / north | Passed ID/reference checks by control flow; raw text not supplied |
| B | 5120 | harbor / 25 / south | Reference and all four target responses start with this correct JSON |
| C | 4096 | harbor / 22 / north | Target not run |
| E | 4096 | meadow / 27 / south | Target not run |

All four target B output ID sequences are identical. Compared with the cold B
reference, the first mismatch is index 30 (the 31st generated token): reference
7424, target 11129. Both sides have 128 tokens and finish by length. The target
reports cached_tokens=4096; the reference reports 0. Because V3 raises before
the later snapshot checks, this evidence alone does not establish B's full
V1/V6 checks passed.

The leading B JSON correctly incorporates the added three pencils and the
changed red marker. The continuation differs: the reference repeats archive
text and JSON fences; the targets repeat archive text and include the earlier
blue-marker statement. Neither complete text is just one JSON object. The
perf workload forces ignore_eos=true and 128 generated tokens, so correct
leading answer fields do not establish whole-response correctness or exact
output equivalence. No claim is made about the tokenizer identity of an EOS
token, numerical root cause, or all cases being semantically correct.

The perf check currently compares complete IDs, lengths/finish reasons,
hit lengths, and resource/prefix snapshots. It does not independently parse
and grade the expected_answer fields as the separate full acceptance mode
does. Its strict 128-ID rule remains unchanged.

## Readiness failure

P reported BadStatusLine with the line `GET /ready HTTP/1.1`; D reported the
forwarded peer failure. This is independent of the already-observed B output
divergence. The exact cause of the malformed reply on the NPU host is unknown.

The old wait_http helper retries OSError/URLError but allows HTTPException
(including BadStatusLine) to escape immediately. A real local HTTP regression
server reproduces the reported failure by returning the request line once,
then a valid HTTP 200 on the next connection. The regression failed with the
same BadStatusLine before the fix.

The helper now treats malformed HTTP as not-ready within the original startup
deadline, logs the first protocol failure with its URL, and includes the last
error on timeout. Permanent failures still fail. Child/peer watchdog failures
still propagate immediately. Generation requests and output checks are unchanged.

Local validation: 29 acceptance-runner and mempool-entry tests passed, including
real HTTP recovery, persistent malformed-reply timeout, and watchdog failure
after a malformed reply. No NPU rerun has been performed for this fix. Fixing
readiness alone cannot make the existing B samples pass strict output checks.
