# Full-content trace update — 2026-09-30

Installed plugin version: `0.1.0+codex.20260930123631`.

## Code added or changed

All paths below are relative to `<repository-root>`.

| File | Change |
| --- | --- |
| `scripts/trace_capsule.py` | New full-content recorder: per-process capsule, exact payload artifacts, correlated events/spans, UTC and monotonic times, repeat fingerprints, bounded asynchronous writing, rotation without expiration, recording overhead and failure status, private Windows directory permissions. |
| `scripts/trace_report.py` | New standalone HTML/JSON report: chronological calls, full payload links, screenshot previews, request counts, repeated requests, outcomes, overlapping-call timing, internal stages, outside-plugin gaps and incomplete evidence. |
| `scripts/mcp_server.py` | Records incoming/outgoing MCP messages and every tool call; instruments native RPCs and queues; adds `diagnostics`; refreshes reports after Stop and normal shutdown. Records source hashes and task ID when provided by the host. |
| `extensions/zen/native/core_adapter.py` | Adds optional tracing for browser identity checks, connection waits, round trips, authorization failures, unexpected exceptions and revocation events. |
| `extensions/zen/native/bridge.py` | Adds optional post-authentication command/reply/event tracing, wire IDs/deadlines, connection identity, disconnect and reader errors. Preserves transport and authentication behavior. |
| `skills/background-control/SKILL.md` | Adds one short instruction to use diagnostics when investigating a slow/failed task, without calling it after every action. |
| `docs/TRACE-CAPSULES.md` | Documents usage, exact collection scope, storage, timing interpretation, restart boundary and crash limitations. |

Tests added: `tests/test_trace_capsule.py`, `tests/test_trace_report.py`, `tests/test_mcp_trace.py`, and `extensions/zen/tests/test_trace_transport.py`.

## Installed files

The skill and usage document were copied to `<installed-plugin-root>`. Its `.codex-plugin/plugin.json` version changed from `0.1.0+codex.20260930041209` to the version above using the plugin cachebuster helper, then `codex plugin add windows-background-control@personal` installed it.

The installed `.mcp.json` still points to the repository's `scripts/mcp_server.py`; the server implementation is loaded from that path. The installed cache's skill and usage document hashes match the repository versions. A new Codex task/server is required to load the new tools and recording code. No new Zen XPI is required by this update.

## Verification

Recorder, report and MCP integration tests passed, including exact full text/image payload preservation, malformed tool names, concurrent logging, queue overflow, disk errors, timing unions, crash/incomplete evidence and access permissions. One POSIX-only permission test is skipped on Windows. Eight transport tracing tests, fourteen existing bridge tests and sixteen existing core adapter tests passed.

A fresh MCP process launched from the installed configuration recorded **6 tool calls and 53 events**, including a deliberately invalid action. All calls had matching endings, there were zero integrity issues, and the report retained the expected error. No application controller was launched in that acceptance test.

Acceptance capsule: `<repository-root>\runtime\traces\<local-acceptance-capsule>`.

This verifies recording/reporting and compatibility fixtures. It does not establish a speed improvement in a live browser task. The next real task's capsule supplies that evidence.
