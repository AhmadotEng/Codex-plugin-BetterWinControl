# Native background text insertion evidence

The production `NativeEditActions.cs` now supports **plain-text insertion into the current selection of an explicitly observed native `EDIT` or `RICHEDIT50W` control**. It does not provide Firefox/Zen keyboard control or generic background keystrokes.

## Empirical results

`probe.py` created an ordinary-style, offscreen, nonactivating native fixture in a separate process. Every message target was checked against its own PID, HWND list, class and ancestry. The run completed in 969 ms, generated 36 native change notifications, and closed its process. All 63 sampled foreground, focus and cursor values remained unchanged. The fixture did not use `WS_EX_NOACTIVATE`; it used `SW_SHOWNOACTIVATE` and `WS_EX_TOOLWINDOW`.

| Message operation | Single-line EDIT | Multiline EDIT | Msftedit RICHEDIT50W |
| --- | --- | --- | --- |
| EM_SETSEL + EM_REPLACESEL | `alpha beta` → `alpha 世界` | Same | Same |
| WM_CHAR Unicode, including surrogate pair | `Aλ你🙂` | Same | Same |
| Left then Delete at index 3 | `abcd` → `abd`, caret 2 | Same | Same |
| Home then Right | Caret 1 | Same | Same |
| WM_CHAR Backspace at index 3 | `abcd` → `abd` | Same | No change |
| WM_KEYDOWN/UP Enter at index 1 | No change | No change | `a\r\nb` |
| WM_CHAR CR at index 1 | No change | `a\r\nb` | No change |

These measured differences prohibit treating message delivery as universal keyboard equivalence. The key experiments were guarded against held physical and target-thread modifiers. Production insertion uses no key messages or keyboard-state assumptions. A deliberately blocked own window procedure returned `ERROR_TIMEOUT` (1460) after 79 ms with an 80 ms bound. Raw evidence is `results.json`.

`adapter_test.py` linked the actual production helper. Its final run passed in 672 ms: all three eligible controls independently read `alpha 世界🙂` after replacing their existing selection; wrong PID, unrelated root, wrong class, password, read-only, disabled, protected RichEdit policy, malformed UTF-16, NUL and oversized strings were rejected. A blocked fixture caused production `Supports` to return false after 202 ms for its 200 ms budget. All 44 sampled foreground/focus/cursor values remained unchanged and both owned processes exited. The first harness failure is retained as `adapter-results-initial.json`: external `WM_GETTEXT` on its own password control failed after the mutation tests had passed; the corrected harness avoids password text reads. `adapter-results-before-timeout.json` preserves the earlier passing run.

`controller_test.py` then tested the complete current controller executable through its actual RPC interface. The 891 ms run passed capability advertisement, stale-observation rejection, unknown-element rejection, insertion and independent text readback for all three controls, and invalidation after every action. The controller honestly returned `verified=false`, since EM_REPLACESEL itself has no success flag. Both owned processes exited and were independently checked absent. Across 58 samples, foreground and focus each had one distinct value and neither fixture nor preview received sampled focus. The cursor had 29 distinct positions; the test contains no pointer setter, and these samples do not attribute the movement. Evidence is `controller-results.json`.

Sampling cannot exclude transitions between samples. All action tests used disposable fixtures; no real browser, user document, clipboard, physical input stream, injected code or alternate desktop was used.

## Production contract

```csharp
bool NativeEditActions.Supports(nint elementHwnd, nint rootHwnd, int expectedProcessId,
    bool isPasswordOrProtected, bool isReadOnly);
void NativeEditActions.Insert(nint elementHwnd, nint rootHwnd, int expectedProcessId,
    bool isPasswordOrProtected, bool isReadOnly, string text,
    Action? beforeMutation = null);
```

The controller supplies its pinned observed element HWND, validated root/PID and accessibility protection flags. The helper independently requires a live same-process root/descendant, stable target thread, exact Unicode class, enabled state, no native password/read-only style or password character, and no RichEdit `ENM_PROTECTED` policy. Insert validates up to 50,000 UTF-16 code units, rejects NUL/unpaired surrogates and multiline input into single-line controls, invokes the controller's revocation guard immediately before mutation, and sends only undoable `EM_REPLACESEL` with a total 200 ms message budget. It does not select text, focus a control, send keys or use the clipboard. Follow-up observation is required; timeout means the outcome is uncertain and must not be retried blindly.

RichEdit event-mask rejection is conservative policy screening, not proof that every possible protected range or host callback has been inspected. Native controls can be customized by their owning application. This bounded adapter therefore retains the controller's protected-element guards and reports actual content observations rather than inferring success from the class name.

## Primary-source findings

Microsoft documents [EM_REPLACESEL](https://learn.microsoft.com/en-us/windows/win32/controls/em-replacesel) as replacing the selection, inserting at the caret when no selection exists, and optionally preserving undo. [EM_SETSEL](https://learn.microsoft.com/en-us/windows/win32/controls/em-setsel) supports both edit families. Those documents do not establish behavior for custom browser widgets. Copies of the Microsoft source pages are retained in `sources/`.

[SendMessageTimeoutW](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendmessagetimeoutw) invokes a specific HWND's procedure and marshals system messages below WM_USER; same-queue calls ignore the timeout. Our adapter is a separate process and uses this system-message range for its text pointer. RichEdit's event-mask getter has only scalar parameters/results. A timeout does not certify that an operation never happened.

[WM_CHAR](https://learn.microsoft.com/en-us/windows/win32/inputdev/wm-char) carries UTF-16 units for Unicode windows, including surrogate pairs. The ordinary physical-input route involves TranslateMessage; directly sent messages have different sequencing. Microsoft's [keyboard-input guidance](https://learn.microsoft.com/en-us/windows/win32/inputdev/using-keyboard-input) explains the distinction between virtual-key and character messages.

Firefox is different. Its [address-bar implementation](https://github.com/mozilla-firefox/firefox/blob/main/browser/components/urlbar/content/UrlbarInputBase.mjs#L179) contains a DOM input. [nsWindow.cpp](https://github.com/mozilla-firefox/firefox/blob/main/widget/windows/nsWindow.cpp#L6245) dispatches characters through `NativeKey` with current modifier state. [KeyboardLayout.cpp](https://github.com/mozilla-firefox/firefox/blob/main/widget/windows/KeyboardLayout.cpp#L2706) supports some standalone character messages, but also accounts for preceding messages and IME state; its keydown handling has an IME-redirection branch that calls `SendInput`. Mozilla's [IME architecture guide](https://firefox-source-docs.mozilla.org/editor/IMEHandlingGuide.html) describes focused-document/element routing. The browser HWND therefore does not identify an independently addressable native EDIT for an arbitrary DOM field. This is a source-based reason to keep Firefox/Zen outside this native adapter, not a claim that every possible browser background integration is impossible. No messages were sent to Zen.

Downloaded primary-source snapshots and their SHA-256 hashes are retained in `sources/`; linked upstream branches can change.

## Commands

```powershell
python -B ./probe.py
dotnet build NativeEditTests.csproj -c Release --verbosity minimal
python -B ./adapter_test.py
python -B ./controller_test.py --controller C:/Users/Administrator/plugins/windows-background-control/runtime/BackgroundControl.exe
```

Build used .NET 10 without additional NuGet dependencies. Local build metadata was scoped to this directory. The fixtures have a 12-second self-close timer; the full-controller test has a 16-second owned-process watchdog and explicit final cleanup.
