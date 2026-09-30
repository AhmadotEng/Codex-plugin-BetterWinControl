# Native coverage and next adapters

Audit of the frozen runtime, 2026-09-29. The intended goal remains independent
control of existing Windows application/system surfaces on the same desktop.
The current fixture results establish a useful subset, not system-wide parity.

| Surface | Current evidence / implementation | Remaining gate |
| --- | --- | --- |
| Ordinary Win32 client controls | Owned x64 suite: real clicks, double-click, wheel, drag, EDIT text, modifiers, isolation and lifecycle; x86 smoke also passes. | Real application/framework matrix and physical co-use. |
| WPF client controls | Owned Button, TextBox, ScrollViewer and Canvas effects pass after modifier and virtual mouse-leave fixes. | Other WPF controls, popups, menus, custom dispatchers and versions. |
| Native context menus | Right-button messages work in an ordinary handler. `nativeMenus:false`. | Actual menu opening, submenu tracking, selection, cancellation and teardown. |
| Owned dialogs | `Scope()` accepts a same-process owner chain, but `Hit()` only descends from the selected root. `ownedDialogPointer:false`. | Dialog surface discovery, geometry, capture, routing and modal continuation. |
| Title bar / borders | `Move` checks root client bounds; dispatch sends client `WM_MOUSE*`. | Nonclient hit-testing, caption actions, move/size loops and DPI. |
| Deferred / other-thread input | `SameThread()` rejects other threads; virtual state exists during controlled synchronous dispatch. | Correlated queue/dispatcher work without changing unrelated input. |
| Renderer-backed UI | One selected PID, one UI thread, one native session per process. | Prove both shell/chrome and content outcomes, including process/frame changes. |

Code anchors: [capability response](host.cpp#L203), [scope and hit-testing](virtual_input.cpp#L55),
[focus/active-window detours](virtual_input.cpp#L152), [dispatch](virtual_input.cpp#L245),
[synchronous transport](virtual_input.cpp#L333). Boolean action capabilities
describe available dispatch operations. `delivered:true` and
`effectVerified:false` are deliberately not application-effect verification.
The unimplemented list in [the acceptance suite](../tests/native-input/acceptance.py#L241)
must stay visible alongside passing checks.

**1. Native menus: next feasibility adapter.** A handler that enters
`TrackPopupMenu[Ex]` can keep the original WndProc invocation alive until
selection. Current `Execute()` waits synchronously for that invocation, and
the worker cannot consume another command while waiting. A nested operation
must not overwrite the single `Session.command/reply` slot. Windows also
distinguishes returned menu IDs (`TPM_RETURNCMD`) from later owner notifications.
[TrackPopupMenu contract](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-trackpopupmenu)

Required implementation: bounded immutable command records with separate
accepted/completed acknowledgements; an input mailbox that can progress while
a tracked modal invocation is open; explicit menu HWND/HMENU, UI-thread and
owner identity; scoped virtual capture and current active surface. Establish
which message-retrieval path the real USER32 menu loop uses in an owned probe
before choosing GetMessage/PeekMessage or a narrower adapter. Export hooks
alone do not prove interception of internal USER32 paths. Preserve message
filters, ordering, WM_QUIT, paint/timer traffic and PM_NOREMOVE semantics.
Cancel must dismiss the owned menu and drain active callbacks before unload.
Any code that would require global focus/capture or filtering physical input
fails this adapter's acceptance gate.

Tests: real CreatePopupMenu + TrackPopupMenuEx, both notification modes,
disabled items, nested submenus, pointer hover and keyboard navigation,
Escape/outside cancellation, pause/EOF/owner loss while open, and reopen after
detach. Verify the actual selected command ID and closed menu. Include a
same-thread unrelated window query during menu processing, a separate physical
input pad, and actual menu pixels in the preview. Keep `nativeMenus:false`
until these pass. A returned right-click acknowledgement is insufficient.

**2. Owned dialogs and surface identity.** Start with ordinary same-thread
modeless owned windows, then real modal dialogs. `DialogBoxParam` disables its
owner and runs its own message loop; merely recognizing the owner chain does
not supply input to that loop.
[DialogBoxParam contract](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-dialogboxparamw)

Required implementation: explicit selected-family surface IDs with fresh
HWND/PID/thread/owner-chain identity and destruction generations; hit-testing
the relevant owned top-level surface before descending into children; defined
physical-pixel coordinates and per-surface preview/frame binding. Return the
virtual focused surface's top-level owner from active/foreground queries,
rather than always returning the original root. Use the menu continuation
work for modal loops; later add separately scoped actors for other UI threads.
Reapply protected/credential-surface checks when a new dialog appears.

Tests: dialog outside root bounds, disabled owner, tab traversal, default and
cancel buttons, editable controls, nested modal dialog, sibling window outside
the owner chain, closure/recreation, and cancellation/unload mid-dialog.
Read back real dialog results and verify owner re-enables normally. A new
process or broker window requires separate authenticated ownership evidence;
process-tree membership alone does not extend control scope.

**3. Nonclient chrome.** Introduce an explicit window/screen coordinate mode,
signed multi-monitor coordinates, DPI conversion and geometry generations.
Use the target's real `WM_NCHITTEST` result and appropriate `WM_NC*` messages;
test the DWM caption path and move/size loops rather than treating a toolbar
button as a title bar. Native processing can lead to `WM_SYSCOMMAND` and
requires the modal-loop work above.
[Nonclient button contract](https://learn.microsoft.com/en-us/windows/win32/inputdev/wm-nclbuttondown),
[DWM custom-frame handling](https://learn.microsoft.com/en-us/windows/win32/dwm/customframe).
Tests must cover actual caption buttons, title-bar drag, border resize,
negative monitor origins, mixed DPI, stale-frame rejection and pause during
move/size, using owned windows only before any real-app gate.

**4. Deferred frameworks and renderer ownership.** The WPF correction
virtualizes a specific TrackMouseEvent request; it does not virtualize every
later dispatcher callback. A future adapter needs an authenticated operation
token propagated only through work attributable to the selected surface.
Test a queued callback and a concurrent unrelated same-thread callback;
keeping TLS virtual state enabled between requests is not acceptable.

Chromium's browser process owns UI and routes input to renderer/frame objects;
renderer processes can serve multiple documents. Successful top-level HWND
messages alone do not prove content delivery or isolation.
[Chromium architecture](https://new.chromium.org/developers/design-documents/multi-process-architecture/).
Use owned multiprocess fixtures, then an explicit application matrix covering
toolbar, content, popup, navigation/process replacement, compositor scrolling,
and independent sibling windows. Bind any framework adapter to proven
surface/frame identity. Never infer renderer scope from an executable name or
weaken sandbox/security settings to make a test pass.

**Reference boundary.** ProtoInput is a useful source of hook techniques, not
evidence that this engine already supports menus or desktop frameworks. Its
[message filter hooks](https://github.com/Ilyaki/ProtoInput/blob/dd29bf8d22349605d0e1950c1c5dc4893d72670e/src/ProtoInput/ProtoInputHooks/MessageFilterHook.cpp#L143)
wrap GetMessage/PeekMessage; its
[focus hooks](https://github.com/Ilyaki/ProtoInput/blob/dd29bf8d22349605d0e1950c1c5dc4893d72670e/src/ProtoInput/ProtoInputHooks/FocusHook.cpp#L16)
return the selected HWND broadly. Those semantics cannot replace this
project's window-scoped isolation. Stock physical-device routing, global input
locking and Explorer suspension are outside the design.

Every adapter keeps the current lifecycle, same-process canary and physical
co-use gates, adds actual outcome evidence, and reports unsupported behavior
explicitly. Raw input, IME, OLE/cross-app drag-and-drop, timed/nonclient hover,
ARM64, protected/secure surfaces and elevated targets remain distinct work;
none are established by current fixture counts.
