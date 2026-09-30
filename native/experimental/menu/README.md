# Native menu continuation experiment

Status, 2026-09-29: design only. No menu probe has run, no application has been
injected, and the four frozen production binaries remain unchanged. Work paused
before fixture implementation when the user prioritized existing-application
testing. `nativeMenus:false` remains the correct production capability.

## First implementation gate

Use an owned ordinary Win32 fixture with `CreatePopupMenu` and the real
`TrackPopupMenuEx`. Observe its actual returned command ID with
`TPM_RETURNCMD`, then separately test the ordinary `WM_COMMAND` notification
mode. Do not substitute a direct `WM_COMMAND`, invented menu UI, or hardcoded
return value. A genuine selection has not yet been demonstrated.

Start with a thread-specific `WH_MSGFILTER` hook for `MSGF_MENU`, installed
only for the selected owner's active menu invocation. Microsoft's documented
menu integration example explains that the native modal loop does not deliver
low-level pointer/key messages directly to the ordinary owner handler. The
filter is a supported observation/interception point; whether it can supply
this project's independent input safely still requires a probe.

The frozen transport cannot simply add more messages: its `Worker` waits for
`Execute`, `Execute` synchronously waits for the selected WndProc, and the
session has one mutable command/reply pair. The experimental target needs
bounded immutable command records, separate acceptance and completion, and a
menu mailbox that remains serviceable while the original call is inside its
modal loop. Do not overwrite the active command from a second request.

Bind each mailbox item to the selected HWND/PID/thread, destruction generation,
active menu invocation, HMENU, and an unguessable operation token. Consume only
its authenticated synthetic records. Preserve unrelated queue messages,
message filters, ordering, timers, paint, `WM_QUIT`, and `PM_NOREMOVE`. Retain
the existing reentrant-window isolation: a same-thread sibling callback must
observe real input APIs even while the selected menu invocation is active.

## Hidden-fixture prerequisite

The current task permits no user-facing test window. An offscreen menu position
is insufficient because native menus can be repositioned onto a monitor.
Before calling the real tracker, prove that an owned menu window cannot become
visible or activate. A creation-time hook and a `WM_WINDOWPOSCHANGING` guard
are candidates, not established solutions. Microsoft permits changing most
`WINDOWPOS` fields there, but explicitly says some activation-related flag
changes are ignored. Do not assume changing `SWP_NOACTIVATE` provides safety.

If visibility suppression prevents real menu operation, stop and record that
result. Do not silently remove the guard or use a different desktop. Independently
observe foreground, focus, capture, cursor, and modifier metadata. Native menu
internals may bypass exported USER32 hooks; prior client-control success does
not prove the menu path avoids physical capture or focus.

## Required proof before production integration

- Actual item selection, disabled-item rejection, submenu selection, and both
  return/notification modes through real `TrackPopupMenuEx` behavior.
- Cancellation returns from the native menu loop, clears only virtual state,
  and allows a subsequent menu to open. Exercise Escape, pause, EOF, owner
  loss, and selected-window destruction with bounded deadlines.
- Unrelated same-thread windows receive their own messages and retain real
  key/cursor/focus/capture semantics during nested menu callbacks.
- No actual focus, foreground, capture, cursor, or physical modifier changes;
  no physical input collection/filtering and no `SendInput` fallback.
- The original dispatch and all menu callbacks finish before existing safe
  teardown can acknowledge hook removal/module absence. Unresolved callbacks
  produce explicit pending teardown rather than forced unload.
- A separate future visible-test authorization and real capture/preview proof
  are still needed; a hidden outcome cannot establish menu rendering coverage.

Keep all experiment sources and outputs in this directory. Do not overwrite
`native/bin`, the existing acceptance reports, or advertise menu support until
these gates pass.

## Primary references

- [TrackPopupMenuEx](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-trackpopupmenuex): real native tracking and owner contract.
- [TrackPopupMenu](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-trackpopupmenu): `TPM_RETURNCMD`, cancellation, notification timing.
- [Microsoft menu hot-tracking example](https://learn.microsoft.com/en-us/windows/win32/controls/cc-faq-iemenubar): modal loop, `WH_MSGFILTER`/`MSGF_MENU`, cancellation and hook removal.
- [Hooks overview](https://learn.microsoft.com/en-us/windows/win32/winmsg/about-hooks): thread/application filter scope and modal message processing.
- [WM_WINDOWPOSCHANGING](https://learn.microsoft.com/en-us/windows/win32/winmsg/wm-windowposchanging): supported position/visibility changes and activation-flag limitations.

These references motivate the experiment. They do not document an independent
Windows input channel or establish feasibility of the proposed adapter.
