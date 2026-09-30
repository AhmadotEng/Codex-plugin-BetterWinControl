# BetterWinControl implementation status

The implementation is in `<repository-root>`.
The installed plugin has not been replaced. This is a development milestone, not
completion of the requested system-wide compatibility matrix.

## Implemented

- `native/`: architecture-matched x86/x64 host and injected helper, pinned MinHook,
  independent pointer/modifier/focus/capture state during selected-window dispatch,
  private IPC and cancellation, checked hook removal and independent module-unload
  verification. No shared foreground mouse/keyboard fallback.
- `controller/NativeInputSession.cs`, `NativeInputClient.cs`, `InputIsolationMonitor.cs`:
  frame-bound ordered input, geometry/identity expiry, foreground/capture monitoring,
  startup/cancellation races, and verified cleanup before target transition or retry.
- `controller/PagedObservation.cs`: bounded accessibility search, subtree traversal
  and continuation. Provider/depth/node/window limits remain explicitly incomplete.
- `controller/InteractionTargets.cs`: native owner-chain discovery and exact identity
  evidence. Same-process siblings, ambiguous menus and cross-process candidates are
  never silently promoted to owned targets.
- `controller/CapturePreview.cs`: acknowledged virtual-pointer overlay and fresh
  input snapshots for static windows, preserving the existing proportional resizing,
  hover controls and borderless capture.
- `controller/Program.cs`, `scripts/mcp_server.py`: `input`, `capabilities`,
  `interaction_targets`, paginated `observe`, optional `browser`, and out-of-band
  cancellation. Delivery acknowledgement and verified application effects stay separate.
- `extensions/zen/`: ordinary WebExtension, native-messaging bridge, exact-window
  pairing contract, tab/navigation/form/media operations and deterministic unsigned XPI.
  Uses the existing browser configuration; no preference change, temporary
  installation or browser rebuild.

## Verification and remaining gates

See [COMPATIBILITY.md](COMPATIBILITY.md) for current results and limits. Automated
Win32/WPF fixtures, full MCP input, lifecycle, concurrency and PiP checks have passed.
These are tested subsets; they do not prove arbitrary application control.

The first attempts at coordinated human co-use stopped before completion. One exposed
a cursor-sampling test race; another recorded foreground acquisition; a third exposed
an empty-exception false-success bug in the runner. That third report is invalid as a
passing result. The runner must require both full 60-second passes and all effect,
isolation and teardown evidence. Preliminary test windows and the user typing pad have
been relabelled so only the topmost **TYPE HERE** window requests user interaction.

The latest visible-target attempt ran 60.062 seconds and 3,267 background actions,
with no target foreground/focus acquisition or new canary input recorded. The exact
test passage matched; physical Ctrl/Shift isolation was observed during 39/32 cycles.
The stillness interval recorded 8 cursor positions across 20 samples. The user said
they did not notice the instruction. This leaves that check inconclusive about the
source of the movement; it is not a passing co-use gate. The covered pass has not
yet run. Human fixture work was paused at the user's request to prioritize live Zen.

A [read-only real Zen check](ZEN-LIVE-STATUS.md) completed paginated discovery,
but found neither a usable Stremio tab-selection control nor the YouTube Pause
button. No successful browser action is claimed by that check. The companion is
unsigned; the user authorized installing/testing it under the existing profile's
already-enabled unsigned-extension configuration. Mozilla signing is optional
for that route. The readiness helper supports `--allow-existing-unsigned` while
retaining active-state, exact-ID, profile-location and payload verification.
The protected local bridge is installed. Live testing found that Zen's existing
HKCU handle could not see the registered host; the supported HKLM64 fallback
fixed discovery. The actual unsigned companion then passed three consecutive
Stremio selections and YouTube playing-to-paused transitions through development
MCP, with 52 metadata samples and no target foreground/focus acquisition. See
the live status and `tests/results/zen-live.json`. Signing/experimental and
automation preferences and the tested extension payload remain unchanged.

Do not replace the installed version until the required first input acceptance gate
passes. Actual Zen pairing and the Stremio/YouTube semantic regressions passed;
restart persistence and general input remain unverified. Native menu/modal loops,
browser renderers/chrome, WinUI, shell surfaces,
Explorer operations and cross-application drag still require implementation and real
application verification; see [native/NEXT-ADAPTERS.md](../native/NEXT-ADAPTERS.md).

## Reuse and footprint

Existing public Windows capture and UIA libraries are reused. The supported installed
Computer Use API is available, but its Windows input auto-activates targets. The
[reuse audit](COMPUTER-USE-REUSE.md) distinguishes actual shared dependencies from
OpenAI implementation source that has not been supplied for copying.

Build products stay under the repository's `.build/`, `runtime/`, `native/bin/`,
test `bin/obj/` and extension `dist/` directories. Extension development dependencies
are local and excluded from distribution. `PyYAML==6.0.2` was added only under
`.build/validation-deps` to run the plugin/skill validators. The existing compiler was
reused read-only; no compiler installation was performed.

An early .NET test build reported creating an ASP.NET development certificate; its
exact provenance was not established, so it was not deleted. Subsequent build scripts
disable automatic development-certificate generation. Browser profile data, saved
extensions, the installed plugin source and its existing cache were preserved.
