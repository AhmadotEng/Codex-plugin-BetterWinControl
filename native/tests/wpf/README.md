# Owned WPF input probe

This launches an ordinary .NET 10 WPF application before attaching the native
host. The fixture has a real Button, TextBox, ScrollViewer, custom Canvas drag
handler, and a second window in the same process. Its JSON endpoint only
reports state and closes the fixture; actions go through the production host.

Build and run from the repository root:

```powershell
dotnet build native/tests/wpf/Fixture.csproj -c Release
python native/tests/wpf/probe.py
```

The runner uses the shared fixture-only monitoring helpers from
`tests/native-input/acceptance.py`. Do not run alongside other visible suites
or user co-use acceptance. The report records binary hashes, independent
control outcomes, sibling events, focus/capture WinEvents, module unload,
and process cleanup. It deliberately records unmet behavior rather than
substituting UIA, foreground input or programmatic control mutation.

Two defects were reproduced and corrected in the common native engine:

* Generic Ctrl was visible through GetKeyState(VK_CONTROL) but not through
  WPF's left/right-specific queries. Generic modifiers now operate the left
  key, aggregate both sides, and send normalized key messages with the right
  scan/extended flags. Separate tests hold right Ctrl/Shift across a generic
  down/up and verify the right key remains held.
* WPF requested TrackMouseEvent(TME_LEAVE) during synthetic movement. With
  the physical pointer elsewhere, Windows immediately posted WM_MOUSELEAVE
  outside the synthetic dispatch, deactivating WPF and dropping managed
  capture. `physical-leave-reproducer.json` retains incoming message evidence
  and the LostMouseCapture call stack. Virtual client leave registration fixes
  Button clicks, wheel routing and Canvas capture across separate host calls.
  No reflection, fixture-specific production branch, atomic gesture shortcut,
  or persistent thread-wide input override was added.

Primary behavior references:

* [WPF Win32KeyboardDevice](https://github.com/dotnet/wpf/blob/main/src/Microsoft.DotNet.Wpf/src/PresentationCore/System/Windows/Input/Win32KeyboardDevice.cs)
* [WPF HwndMouseInputProvider](https://github.com/dotnet/wpf/blob/main/src/Microsoft.DotNet.Wpf/src/PresentationCore/System/Windows/InterOp/HwndMouseInputProvider.cs)
* [Windows TRACKMOUSEEVENT contract](https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-trackmouseevent)
* [Windows key message scan/extended flags](https://learn.microsoft.com/en-us/windows/win32/inputdev/about-keyboard-input)

`results.json` is evidence for these controls on this tested runtime. Menus,
popups, IME composition, timed hover, touch, arbitrary deferred application
code, and concurrent physical interaction with the same target window are
not established by this fixture. The general physical co-use acceptance is
a separate test and has not been replaced by these automated checks.
