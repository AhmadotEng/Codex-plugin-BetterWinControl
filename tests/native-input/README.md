# Native input acceptance

These tests create ordinary visible Win32 windows before attaching the native host. They use a real multiline EDIT control and custom painted hover/drag controls, a second window in the same process, and a separate disposable pad. They never inject physical input, activate a user application, or read text from user applications.

## Recorded result

All **16 implemented gates passed in three consecutive runs** after the native modifier-alias correction: 1,593 / 1,594 / 1,578 ms. Each run observed no target foreground/focus/capture acquisition and no physical cursor/modifier change. All owned processes exited. See `verification.json`; generated diagnostic details are in ignored `results-run1.json`, `results-run2.json`, and `results-run3.json`.

The subsequent scoped leave-tracking and teardown-reclamation corrections passed the same 16 gates in a final 1,703 ms regression (`results-final-runtime.json`). `verification.json` records that runtime's host/DLL hashes separately from the earlier three runs. The interactive runner requires three fresh passing runs of whichever runtime it launches.

The gates verify actual target state changes for hover, click, double-click, right-click, wheel, dragging, Ctrl+K, and Unicode insertion in the native EDIT control. The Unicode check preserves the original surrounding text. They also verify:

- Same-process canary and separate-process pad isolation; virtual modifier/focus/capture state does not leak into a canary API read outside target dispatch.
- A target fully covered by an owned window still performs the requested wheel action. Five external hit tests verify the cover.
- Repeated release while a modifier/button is held ends target dragging and clears held state.
- Detach, EOF, explicit revocation, and controlling-owner loss unload the injected DLL. Independent Toolhelp module enumeration checks this, rather than trusting a host response alone.
- EOF/revocation/owner loss deliver the held Shift release; revocation also ends an active drag. Reattachment and target-close cleanup work.

The event monitor observes foreground, focus, and capture metadata continuously, plus sampled physical cursor and modifier/button state. It does not install a global keyboard/text logger. Idle physical input in these automated runs is **not evidence of simultaneous human co-use**.

## Build and automated run

Requires Windows, .NET 10 SDK, Python 3, and a built native host. From this folder in PowerShell:

```powershell
$env:DOTNET_CLI_HOME = Join-Path $PWD '.dotnet-home'
$env:DOTNET_GENERATE_ASPNET_CERTIFICATE = 'false'
$env:DOTNET_NOLOGO = 'true'
dotnet build .\Fixture.csproj -c Release --nologo
python -B .\acceptance.py
```

`--host` can select another native-host build; the default is `native/bin/x64/BetterWinControl.NativeHost.exe`. The .NET fixture currently runs x64. The separate native x86 smoke test lives in `native/tests/`.

## Coordinated human co-use gate — not yet run

Only after arranging the two-minute exercise with the user:

```powershell
python -B .\interactive_co_use.py --interactive
```

Without `--interactive`, the runner exits before creating windows. It first requires three fresh automated acceptance passes, then opens a visible disposable pad. The user clicks its editor to start two 60-second passes: target visible, then target covered. Each pass requires at least 100 background actions with verified click, wheel, hover, and shortcut effects. Timed instructions ask the user to type a known disposable phrase, keep the pointer still briefly, hold physical Ctrl, hold physical Shift, and correct the phrase if needed.

Checks require actual interleaving of pad typing and target action cycles, observed physical modifier holds and mouse movement, a stable pointer during the hands-still interval, exact local passage matching, continued pad focus, isolated virtual modifier state, and no target focus/capture events. Target Ctrl+K and unmodified F8 actions check both `GetKeyState` and `GetAsyncKeyState`, so a physically held Ctrl/Shift must not contaminate an unmodified action. Only passage match/count results are saved; typed contents are never returned or logged. The pad is cleared between passes and all owned windows/processes close afterward. The ordinary fixture has a 60-second watchdog; this explicit interactive runner requests a bounded 240-second watchdog.

`results-interactive.json` records the outcome. A missing user action, typo, focus change, target effect failure, or skipped phase fails the gate. No interactive success is currently claimed.

## Fixture RPC for integration tests

Launch `bin/Release/net10.0-windows/Fixture.exe` with redirected UTF-8 stdin/stdout. Startup returns `{ready,pid,target,canary,edit,pad}`. Verify the reported PID owns the HWNDs before attaching. Native-host `--owner-pid` is the **controlling process lifetime PID**, not the fixture PID.

Requests are JSON lines `{id,op}`; replies contain the same `id` and `result` or `error`:

| Operation | Purpose |
| --- | --- |
| `state` | Own target counters, text, observed test keys, HWNDs and client dimensions. |
| `reset` | Clear counters, restore the draggable box, reset target EDIT to `seed`; pads become empty. |
| `canary_probe` | Read real cursor/modifier/focus/capture APIs outside virtual target dispatch. |
| `cover` | Show an owned topmost cover without activation; return its HWND. |
| `close_target` | Close only the target; keep its canary alive for cleanup verification. |
| `shutdown` | Close all windows in this fixture process and exit. |
| `instructions` | Interactive pad only: set visible `text` and optional `title`. |
| `pad_status` | Pad only: compare supplied `expected` locally, return match/length/key-message counts and focus flags, without text. |

Root client coordinates: hover/click/double-click at `(40,40)`; safe right-click and wheel at `(200,170)`; drag from `(320,100)` to `(390,160)`; native EDIT at `(30,250)`. Ctrl+K is an actual fixture shortcut requiring virtual `GetKeyState(Ctrl)`. The native `text` command inserts at the current EDIT caret.

The fixture contains native popup-menu and owned-dialog targets for future coverage, but this suite deliberately does not invoke them while the host reports those capabilities unsupported.

## Limits

This is an implemented-subset gate. It does not establish full Windows control, arbitrary app/framework compatibility, app chrome handling, native menus/modal loops, cross-thread dialog routing, cross-application drag/drop, IME/RawInput, or the PiP's pointer rendering. MCP integration and capture/PiP tests are separate. The output always leaves `fullCoveragePassed` false.
