# Codex plugin - BetterWinControl

A local Codex plugin that controls a selected Windows application through accessibility providers and shows a real, movable live preview. The controller uses the existing desktop and does not inject physical mouse or keyboard input.

This is a working development version, **not a completed claim of macOS feature parity**. The preview belongs to this plugin; it is not Codex's built-in Computer Use preview.

## Installed setup

- Plugin package identity: `windows-background-control@personal`, base version `0.1.0`.
- Repository display name: `Codex plugin - BetterWinControl` (URL-safe slug: `Codex-plugin-BetterWinControl`).
- Source checkout: the repository root containing this README.
- Installed copies are managed by the Codex personal plugin marketplace.
- MCP launches `scripts/mcp_server.py` with the `python` command; it starts `runtime/BackgroundControl.exe` on demand after a local build.
- Build output and runtime binaries are intentionally ignored; run the build first on each checkout.
- New Codex tasks pick up installed plugin tools. Existing tasks can test the same MCP bridge directly during development.

There is no network listener, startup registration, background service, browser rebuild or browser-profile edit. Closing the MCP connection or pressing Stop ends the controller. It also watches its parent process so an abandoned session can exit.

## Tools

| Tool | Behavior |
| --- | --- |
| `list_windows` | Lists visible application windows and selection eligibility. |
| `attach_window` | Selects an explicit HWND and opens its live preview without foregrounding the target. |
| `observe` | Returns a bounded accessibility tree, a fresh observation ID and optional captured PNG. Password values are excluded. |
| `act` | Uses supported invoke, set-value, toggle, select, expand, collapse and scroll providers. `insert_text` handles validated native Edit/RichEdit selections. Re-observe after each action. |
| `pause` / `resume` | Pauses or resumes the attached target; observations from before the transition cannot be reused. A provider call already in progress may finish. |
| `stop` | Revokes the target, invalidates queued commands and terminates the controller/preview. Reattachment must be explicit. |
| `state` | Reports target, pause and capture state. |
| `clipboard_state` / `clipboard_read` | Explicitly inspect shared clipboard metadata or read text for an authorized clipboard task. Neither is used implicitly by background typing. |
| `clipboard_write` | Replaces shared clipboard formats with requested text only when its sequence still matches. A newer user copy causes a conflict instead of an overwrite. No automatic restoration. |

The preview is borderless with no permanent title bar or footer. Hovering reveals the title, a top-right close/Stop button and a bottom Pause/Resume button over dark gradients; they fade out when the pointer leaves. A small paused/attention badge stays visible when relevant. Drag the preview surface to move it. Dragging any side or corner resizes both dimensions proportionally to the captured window, with no added letterboxing or cropping. The preview also adapts when the captured window changes shape. Its size limits account for monitor work area and DPI. Capture remains visible while automation is paused. Minimized/hidden/unavailable windows are labeled; the controller does not restore them. Resize recovery restarts only the selected window's capture session when necessary.

The yellow outline around the captured application is drawn by Windows Graphics Capture, which is built into Windows. On supported Windows versions the plugin requests borderless-capture access, then sets `GraphicsCaptureSession.IsBorderRequired = false` for each capture session, including restarts. A denied/unsupported request leaves capture working and reports that the outline remains. Windows can still draw a border if another capturing application requires it. No global capture/privacy policy is changed. The pinned WinApp UIAutomation 0.7.0 dependency has no public session option, so a narrowly checked adapter obtains its private `_session`; this adapter must be reviewed if the dependency changes.

## Verified behavior

- `tests/background-key-probe/README.md`: production native Edit/RichEdit insertion tests and actual controller RPC readback, with ownership/protection, Unicode, timeout and stale-observation checks. This is not browser keyboard coverage.

Generated test reports and screenshots from local validation are intentionally excluded from this repository. Run the test scripts to produce fresh reports under ignored output directories.

The tests are evidence for their recorded scope and artifact hashes. Before/after or periodic focus samples cannot exclude every short transition. Some runs recorded cursor movement while foreground/focus stayed unchanged; those runs do not establish that the cursor was stationary or attribute its movement. An application provider can itself change focus; observed acquisition causes an automatic pause. No generic all-app guarantee follows from the passing tests.

## Remaining work

General key/chord input, hover and drag, broader app coverage, simultaneous user typing, persistent app permissions, native clipboard integration, and complete Codex restart/uninstall verification remain incomplete or unverified. Unsupported actions return an error rather than silently using foreground input. Protected authentication/security and terminal/Codex UI controls require user takeover.

Pet attachment and Locked Use are documented optional Mac features; remote initiation depends on the host. These remain in the comparison inventory. Access to Codex's private native preview implementation is not a prerequisite for this plugin's core background-control experience.

## Source and build

| File | Responsibility |
| --- | --- |
| `controller/WindowController.cs` | Target identity, scoped UIA observation/actions, stale-state and pause/revocation checks. |
| `controller/CapturePreview.cs` | Direct WGC capture, actual preview UI, resize handling and controls. |
| `controller/Program.cs` | MTA controller worker, WPF dispatcher, parent lifetime and local RPC. |
| `controller/WindowLease.cs` | Exclusive ownership of selected window scopes across controller processes. |
| `controller/NativeEditActions.cs` | Bounded, validated native Edit/RichEdit text insertion; no browser message injection or global input. |
| `controller/ClipboardBridge.cs` | Explicit shared-text clipboard access and sequence-conflict checks; no hidden paste/restore. |
| `scripts/mcp_server.py` | MCP framing, argument validation, selected-session lifetime, timeouts and cancellation. |
| `skills/background-control/SKILL.md` | How Codex should operate the tools and report their limits. |
| `.mcp.json` / `.codex-plugin/plugin.json` | Local launch and plugin metadata. |

Build with installed .NET SDK 10 and Python 3.11+. The Windows SDK target is 22000 to expose the borderless-capture API; the supported OS floor remains 19041, with the newer API guarded at runtime:

```powershell
pwsh -NoProfile -File .\scripts\build.ps1 -Locked
python .\tests\integration_test.py
python .\tests\capture\cancel_audit.py
```

Project dependency versions and hashes are pinned in `controller/packages.lock.json`. Python runtime code uses only the standard library. Build caches are confined to `.build/` and test/build output directories under this plugin; published runtime files are under `runtime/`. These generated directories are excluded from version control.

An optional bulk cleanup of generated build directories was rejected by automatic approval review with only `blocked by policy` as the reason. Those generated files were retained, along with all source and test evidence. This rejection did not prevent development or installation.

Use Codex's plugin uninstall action to disable/remove the installed plugin. Source and test evidence are separate from the installed copy and should be preserved unless explicitly included in a cleanup request.

Clipboard writes replace shared clipboard formats only at the explicitly requested sequence. A hard cancellation between Windows clearing and setting data cannot be rolled back atomically; interrupted-write results therefore report an unknown outcome and never automatically retry or restore older user data. The invisible clipboard-owner window runs on the pumped preview UI thread, while clipboard operations run on the controller worker.
