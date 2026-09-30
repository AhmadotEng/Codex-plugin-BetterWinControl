# BetterWinControl trace capsules

Full local tracing is enabled automatically in each newly started BetterWinControl MCP server. This is the user's explicitly requested full-content mode. It preserves entered text, returned clipboard data, observed page text, exact tool arguments/results and screenshot data without redaction. Traces are not uploaded. Capsule directories grant access to the current Windows account and SYSTEM.

## Open a task's report

Call `diagnostics` with `{"report":true}`. It returns the current MCP process's capsule directory and a standalone HTML report. A report is also refreshed after `stop` and at normal MCP shutdown. Logging continues across Stop/re-attach in the same MCP process, so related attempts stay together. A new MCP process gets a separate capsule. `CODEX_THREAD_ID`, when supplied by the host, is recorded to correlate the task; unavailable task identity is not guessed.

Location: `<repository-root>\runtime\traces\<UTC-start-pid-run-id>\`.

For an exited or interrupted task, run `scripts/trace_report.py <capsule-directory>` using Python. Omitting the directory selects the most recently written capsule, which need not belong to the currently visible task. Logs can still be inspected while a report is stale or missing.

## What the capsule records

- Every MCP request/response, including initialization, discovery, errors and cancellations; each tool call has its own call ID, caller request ID, arrival/start/end timestamps and queued/execution/total duration.
- Full tool arguments and results in payload files, plus JSONL events. Screenshots are preserved when the tool actually returned them. There is no extra desktop capture, background keystroke collection or continuous screen recording.
- Exact repeated requests and logical repeats that ignore changing observation/frame IDs. Repeated requests are evidence of attempts, not automatically evidence of failure.
- Native controller RPC arguments/responses, stderr, cancellation and launch events; lock wait spans; browser preflight/postflight, process/window identity checks, connection waits and the native-host/extension round trip.
- Browser command envelopes, wire request IDs/deadlines, full replies and errors, notifications, disconnects and original authorization/adapter failure reasons. Authentication handshake credentials are not tool requests or results and are not collected.
- Counts, slowest calls, errors, pending verification, unfinished calls, overlapping calls and gaps between calls. Busy time uses interval unions rather than double-counting concurrent calls.

The `zen.roundtrip` stage includes transport, native messaging and extension execution together. Companion 0.2.0 does not return separate browser-internal timings, so the report cannot honestly split those further. Nested stage durations overlap and must not be added as independent wall time.

## Interpreting completeness

This records activity reaching BetterWinControl after the updated MCP server starts. It cannot reconstruct previous tasks, another plugin's actions, internal Codex reasoning or what happened between tool calls. Outside-plugin gaps are labelled as unobserved time, not as model thinking or user inactivity.

Successful delivery is distinct from a verified application effect. A call ending without a transport error is not evidence that a page changed. The report retains returned verification and pending states.

The append-only event files rotate by size without deleting old evidence. A bounded background writer handles normal recording; overload falls back to synchronous spooling rather than silently discarding calls. Status exposes logging failures, pending data and fallback overhead. An abrupt process or system failure can lose queued data; missing endings, sequence gaps and write errors must remain visible as incomplete evidence. Logging failure must not block Stop or change an application-control result.

Artifacts stay in the ignored `runtime` directory. No automatic expiration or deletion is configured. `BWC_TRACE_DIR` can select an explicit diagnostic output root; `BWC_TRACE_DISABLED=1` disables recording for isolated test harnesses. Both are startup settings, not commands read from webpages.
