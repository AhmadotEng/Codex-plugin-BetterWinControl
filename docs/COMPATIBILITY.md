# BetterWinControl compatibility and acceptance

This is an experimental implementation of system-wide background control. The current
automated gate proves defined Win32 and WPF fixture subsets. It does not establish universal Windows
compatibility or successful simultaneous physical-user input.

The local `windows-background-control@personal` test installation now launches this
repository's MCP server; its earlier controller/runtime files are preserved.
Development and tests run from this repository. Test reports are generated locally;
passing claims apply only to the recorded artifact hashes and test conditions.

| Area | Current evidence | Status / remaining work |
| --- | --- | --- |
| x64 Win32 target already running | 16 direct native fixture gates: actual hover, click, double/right click, scroll, drag, modifier chord, Unicode Edit; same-process canary and separate pad | Automated subset passed; no general app claim |
| Native event isolation | WinEvent foreground/focus/capture monitoring and periodic state samples; no acquisition in the recorded fixture runs | Passed for those runs; physical concurrent use still required |
| Covered target | Own opaque cover checked at five points; real target scroll effect | Passed for Win32 fixture |
| x86 native target | Actual x86 attachment, click, Ctrl+K, Unicode and independent DLL absence | Smoke passed; full x64-style coverage still pending |
| MCP native integration | Click/double-click/Unicode and named Ctrl+K effect readback; invalid-key preflight; used, resized and minimized frame rejection; Pause/Stop teardown independently checked | 10 integration checks passed |
| Release and lifecycle | Held key/button release; repeated release; disconnect; private revoke event; owner death; target close; independent module enumeration | 16-gate Win32 suite passed; hung dispatch reports pending and cannot safely be force-unloaded |
| Safe DLL removal | x86/x64 deterministic case with zero callback counter and instruction pointer outside DLL, but a pending return into DLL on the stack | Unload correctly withheld until return; thread/context/stack inspection limits fail closed |
| Target destruction | In-process WM_NCDESTROY permanently revokes the selected HWND before its procedure runs | Implemented; a live UI thread with no window to complete the teardown barrier can retain an inert module pending thread exit |
| Accessibility discovery | 713 distinct nodes across 15 pages; search finds item 349 beyond former ceiling; stale continuation rejection | Passed; provider failures/depth/node budgets remain explicitly incomplete |
| Existing semantic actions | Owned WPF set-value/invoke/readback/capture/Stop tests | 13 regression checks passed |
| Existing PiP/capture | Aspect ratio, resizing, hover controls, capture lifecycle and borderless-session settings | 43 regression stages passed, including pointer and freshness checks |
| PiP virtual pointer | Nine tested source/preview positions within one physical pixel; actual rendered arrow pixels and clean removal verified | Passed at current display DPI; other DPI/monitor combinations still pending |
| Static-window coordinates | A 1,242 ms-old static image required an actual new WGC session/frame, returned in 63 ms at age 1 ms; identical pixels/geometry | Passed; old images are never assigned fabricated fresh timestamps |
| 60 seconds / 100 actions with user typing | Latest visible-target attempt ran 60.062 seconds / 3,267 actions, exact phrase matched, no target foreground/focus or canary input recorded | Stillness requirement not met; user missed the instruction. Source of movement unproven. Covered pass pending; full gate not passed |
| Named shortcuts / existing parser reuse | WinApp public KeyStringParser, explicit VK/left-right mapping, text and unsupported extended-key rejection | 60 pure parser checks passed; names do not by themselves prove target shortcut effects |
| Physical versus virtual modifiers | Latest visible co-use run includes 39 physical Ctrl and 32 physical Shift cycles with target/canary modifier checks passing | Observed in that run; covered co-use pending |
| Startup/cancellation races | Three actual MCP races plus ten production C# lifecycle cases with inert OS/helper boundaries | 13 cases passed; separate native tests verify actual unload |
| Interaction-target discovery | Seven ownership fixture tests: identity/owner chain, ambiguous/cross-process candidates, priority and explicit truncation | Passed discovery only; no modal-input claim |
| Native context menus / owned dialogs | UIA enumerates same-process owned windows; input currently selects one client area | Native menu-loop and cross-thread/dialog adapters unfinished |
| WPF general pointer/key input | 10 fixture gates pass: Button, TextBox Unicode, ScrollViewer, Canvas drag, Ctrl+K, modifier sides, unchanged sibling, no foreground/capture events, safe unload | Initial physical-pointer leave failure fixed by scoped TrackMouseEvent handling; reproducer retained separately. General WPF coverage remains unproven |
| WinUI / Settings | No native-input acceptance run | Unfinished |
| Chromium/Electron | No native-input acceptance run | Unfinished |
| Gecko/browser chrome | Read-only live Zen pagination found Stremio/YouTube documents, but no usable Stremio tab-selection control or Pause button | No successful action; native-input acceptance unfinished |
| Zen companion | Existing profile, v0.2 unsigned companion installed with exact payload validation. Automatic authenticated readiness implemented and tested. Earlier v0.1 development-MCP runs verified three consecutive Stremio selections and YouTube playing-to-paused transitions; 52 metadata samples and no target foreground/focus acquisition | Tab/media subset passed under the recorded v0.1 conditions; v0.2 actual Codex acceptance recorded separately. HKLM64 native-host fallback fixed observed HKCU lookup failure. Restart persistence, hover, drag, shortcuts, browser chrome and full co-use remain unverified |
| Direct WebDriver BiDi | No endpoint/profile changes | Deferred until isolation tests pass |
| Zen automatic readiness v0.2 | Actual Codex-exposed MCP authenticated and bound three times in 207/192/42 ms; same native host survived Stop and over 70 seconds idle; read-only observation passed | Connection gate passed without toolbar or physical input. Zen remained foreground, so background tab selection was not attempted in this run; browser restart remains untested |
| Explorer file operations / cross-app drag | No real-app acceptance run | Unfinished; OLE drag/drop is not equivalent to this client-area drag implementation |
| Start/taskbar / shell surfaces | No native-input acceptance run | Unfinished |
| Secure desktop / protected processes | Windows access boundaries retained | Outside supported coverage |

Do not turn `delivered:true`, a moving PiP pointer, or a successful semantic command
into an effect-verification claim. Verify each operation using application state or
fresh observation. Each real-app scenario still requires three consecutive runs.

Current native implementation uses application-local, synchronous dispatch context.
Deferred framework processing, raw-input devices, IME composition, native menu loops,
cross-thread contexts and cross-application drag/drop require further adapters.
No fallback sends input into the shared desktop stream.

Ordinary terminal, Settings, privacy-settings and developer-console targets are no
longer rejected just for those names. This is eligibility only, not a compatibility
claim. Native identity validation does not depend on an available UIA root; semantic
actions still validate their own current element/root. The controller, Codex itself,
credential/security-consent interfaces and protected applications retain exclusions.

The next required architecture changes for native menus, modal loops, owned dialogs,
nonclient chrome and renderer ownership are specified in `native/NEXT-ADAPTERS.md`.

Relevant reports: `tests/native-input/results.json`, `native/tests/x86-results.json`,
`tests/results/native-mcp.json`, `tests/results/observation-search.json`,
`tests/results/integration.json`, `tests/capture/results.json`.

The x64 direct native suite also passed three consecutive runs, 16 checks each:
`tests/native-input/results-run1.json`, `results-run2.json`, and `results-run3.json`.
Run `python tests/native-input/interactive_co_use.py --interactive` only when the user
is ready to use the disposable pad for both full 60-second passes. It checks typed
test text locally and saves counts/results, not typed contents.
