# Native virtual input backend

This is an experimental backend for an already-running, visible, ordinary
Windows application on the current default desktop. It does not launch a copy
of the target, switch desktops, collect physical input, use SendInput, restore
focus, alter the clipboard, or lock the user's devices.

Build with an existing llvm-mingw toolchain:

```powershell
$env:BWC_NATIVE_TOOLCHAIN = 'C:\path\to\llvm-mingw'
.\scripts\build-native.ps1
```

The script also accepts `-Toolchain` and `-Architecture x64|x86|all` and can find
the cross compiler on PATH. It verifies the retained MinHook 1.3.4 archive,
compiled vendor source/header hashes, and nlohmann/json 3.11.3 header. See
THIRD-PARTY.md and the original license files. No compiler is installed.

Outputs are `native/bin/{x64,x86}/BetterWinControl.NativeHost.exe` and
`VirtualInput.dll`. Host architecture must match the selected application's
process. Start hidden with `--hwnd <decimal> --owner-pid <controller-PID>`.
The owner is the controller lifetime process, not the target PID. HWND, process,
thread, desktop, and architecture are checked before attaching. Security UI and
higher-integrity processes are rejected; no protection bypass is attempted.

The host has no unsolicited stdout output. Send one JSON object per line:

```json
{"id":1,"op":"capabilities"}
{"id":2,"op":"move","x":80,"y":50}
{"id":3,"op":"button","button":"left","down":true}
{"id":4,"op":"button","button":"left","down":false}
{"id":5,"op":"double_click","button":"left"}
{"id":6,"op":"wheel","delta":120}
{"id":7,"op":"key","vk":17,"down":true}
{"id":8,"op":"text","text":"Example"}
{"id":9,"op":"release"}
{"id":10,"op":"state"}
{"id":11,"op":"detach"}
```

Move uses selected-root client coordinates. Button, wheel and double_click use
the current virtual pointer, or accept an optional paired `x`/`y` to move first.
Buttons are `left`, `right`, `middle`; key accepts virtual-key codes 1 through
255. Text is bounded at 2048 UTF-16 code units and is distinct from key presses.
Generic Shift, Ctrl and Alt operate the corresponding left key. Their generic
state aggregates the left and right keys; releasing a generic modifier does
not release an explicitly held right modifier. Key messages use generic
WPARAM with the side-specific scan code and extended-key flag.
Each response is `{id,result:{...}}` or `{id,error:{code,message}}`.
Successful commands report acknowledged pointer position, heldKeys,
heldButtons, virtualFocusHwnd, virtualCaptureHwnd and delivered.
`effectVerified` is always false: dispatch completion alone is not evidence
that an application performed the intended action. Tests read target state
independently.

The host uses a random local named pipe restricted to the current user and
verifies the connected target PID. The DLL verifies the server PID. A random,
current-user-only event supports out-of-band revocation. Its name is returned
by capabilities as `revocationEventName`; the controller may open it with
EVENT_MODIFY_STATE and signal it. EOF and owner loss also revoke the session.
The DLL watchdog checks the event and host lifetime independently of target
dispatch. Pausing must revoke or send release; neither sends system key-up.

Input is dispatched synchronously on the target UI thread. Detours virtualize
cursor/key/focus/capture APIs only inside controlled synthetic dispatch. A
nested dispatch to another window outside the selected window family uses
real state. Public USER32 setter hooks are supplemented by scoped WIN32U
setter hooks because native EDIT controls can bypass USER32 exports. These
user-mode internal entry points are a compatibility limitation requiring
Windows-version tests; no syscall numbers, kernel hooks or drivers are used.
Client `TrackMouseEvent(TME_LEAVE)` registrations made during synthetic input
are virtual, with bounded query/cancel state and leave delivery on pointer
recipient changes or release. They never register the physical pointer.
Timed hover and nonclient tracking requests inside synthetic dispatch return
`ERROR_NOT_SUPPORTED`; registrations outside dispatch are untouched.

Ordinary same-thread child controls are the initial capability. Cross-thread
windows, native nested modal/menu loops, IME composition, buffered/raw input,
owned-dialog pointer routing and deferred framework input are not advertised
as supported. Pointer capture ends when the final synthetic button is
released; persistent menu-style capture is outside this initial contract.
Only one native session is allowed per target process. This is not a claim of
universal application coverage.

Detach performs synthetic release, removes hooks, waits for active callbacks,
and unloads the DLL. The host independently enumerates target modules before
acknowledging `hooksRemoved:true,moduleUnloaded:true,detached:true`.
The callback counter alone is insufficient: its destructor can finish before
the hook epilogue returns. Teardown also completes a benign `WM_NULL` barrier
on the pinned UI thread after removing its Windows hooks, disables detours,
and briefly suspends the target process's remaining threads to inspect their
instruction pointers and active stacks for references to the DLL or MinHook
trampoline allocations. The watchdog thread has already exited. Inspection is
bounded to 512 threads, 2 MiB of active stack per thread and 100 ms of scanning;
inaccessible contexts, ambiguous stack contents or exceeded bounds retain the
inert module. This conservative check may defer unloading unnecessarily.
If a still-live UI thread has no window through which to complete its hook
barrier, unloading waits for that thread to exit. The selected HWND's
`WM_NCDESTROY` permanently revokes the session even if Windows later recycles
that numeric handle on the same process and thread.
If a target hangs inside its own callback, the session is revoked immediately
but executing code cannot be unloaded safely. The helper remains scoped to
that executing dispatch until it returns; the host reports `detach_pending`
instead of killing the target or falsely claiming removal. No new input is
accepted after revocation. Closing a target window is not itself considered
proof that its process's DLL unloaded.

Owned-fixture verification is under `tests/native-input`. Results from those
fixtures are evidence for the tested controls and Windows build, not for all
frameworks or all system surfaces.
The owned WPF fixture and the recorded mouse-leave failure that motivated its
fix are under `native/tests/wpf`; an independent x86 smoke fixture is under
`native/tests`. Tested WPF Button, TextBox, ScrollViewer and Canvas behavior
does not imply arbitrary framework, menu, popup or deferred-input coverage.
`native/tests/run-quiescence-test.ps1 -Toolchain <llvm-mingw>` tests a zero
callback counter and an external wait with a pending return into an owned
DLL. Both architectures must refuse reclamation until that call returns,
then independently verify module disappearance.
