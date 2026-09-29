# Live CapturePreview integration test

This harness links the actual plugin-owned `controller/CapturePreview.cs`. It captures only its own WPF fixture, briefly shows the actual preview window, and invokes the real preview buttons through their WPF automation providers. It sends no physical input and performs no clipboard or user-app operations.

The capture-border and aspect-lock run retained in `results.json` passed all 34 stages in 4800 ms. The initial passing run remains in `results-initial.json`:

- Initial blue capture: 160×120, frame version 1.
- Fresh green content under an independently verified own covering window: version 2.
- Static resize recovery: 224×152 red capture, version 3. Further source changes to 120×224 portrait and back to 224×152 landscape updated the preview ratio. Geometry checks wait for the intentional capture-refresh debounce to finish.
- PNG snapshot from a worker thread succeeded.
- The Windows borderless-capture access request returned `Allowed`, and the real `GraphicsCaptureSession.IsBorderRequired` property read back `false` after the initial frame, source resize, portrait change, landscape restoration and explicit restart. The fallback/failure notice was empty in all five checks. Source resize also created a new session, so the checks cover reapplying the setting.
- The preview has no native title bar, keeps `ShowActivated=false` and `WS_EX_NOACTIVATE`, and configures a six-pixel `WindowChrome` resize border.
- Actual native `WM_SIZING` messages exercised all eight side/corner cases in both landscape and portrait. The returned rectangles preserved the source ratio and the opposite edge/corner anchors. Each constrained rectangle was applied only to the test process's preview with `SetWindowPos` using `SWP_NOACTIVATE | SWP_NOZORDER`; no physical resize drag was injected.
- Deliberately sub-minimum proposals retained the ratio and honored both minimum dimensions: 280×190 landscape and 280×523 portrait at the tested 96 DPI.
- Synthetic WPF `MouseLeave` hid the control overlay with opacity zero and hit testing disabled. Synthetic `MouseEnter` revealed it with opacity one and hit testing enabled. Each transition settled before observation; the test did not move the physical pointer.
- The image fills both preview-surface dimensions within one physical pixel at the initial size, after every source-ratio change, after all 16 native sizing cases, and at minimum size. `Stretch.Uniform` and zero image margins remain in use; the preview frame matches the image instead of introducing letterboxing or cropping. The initial 160×120 capture used a 520×390 preview; the final 224×152 capture used 280×190.
- Real pause/resume/stop button callbacks passed.
- Controls also hid on synthetic pointer leave while paused; the persistent paused badge remained visible in the actual visual render.
- Closing the preview invoked stop.
- Minimizing the fixture produced a minimized/unavailable label and removed its image.
- Closing the fixture produced `IsClosed=true` and `Active=false`.
- Actual WPF renders produced `preview-idle.png`, `preview-hover.png`, and `preview-paused-idle.png`. The idle and hover image bytes differ. `preview-content.png` remains a copy of the hover-state visual. These render the preview itself without capturing the desktop.

All 168 input-metadata samples had the same foreground handle, focus handle and cursor position; no own window received sampled focus. The harness only read this metadata and injected no pointer or keyboard input. All own windows closed and the test process exited. The emitted commands were `pause`, `resume`, `stop`, `stop`, `stop`. See `verification.json` for the post-run process check.

Capture PNG evidence is saved as `initial.png`, `occluded.png`, `resized.png`, `portrait.png`, `landscape_restored.png`, `restarted.png`, and `closure_session.png`; these contain only the own fixture. Border checks verify OS permission and actual capture-session configuration, not visual absence of the OS outline; another capturing app or Windows policy can still require it. Hover verification uses synthetic routed events and semantic button invocation, not physical pointer interaction. Native resize-message tests do not establish the entire physical dragging experience or mixed-DPI behavior. Metadata sampling does not prove the absence of shorter focus transitions or establish simultaneous physical-user activity. This test does not establish behavior for every application or protected content.

The launcher and WPF fixture each have an 18-second watchdog. Run from this directory after building:

```powershell
pwsh -NoProfile -File './run-tests.ps1'
```

Build requirements: .NET SDK 10 with WPF; target `net10.0-windows10.0.22000.0` with supported OS floor `10.0.19041.0`; `Microsoft.Windows.SDK.BuildTools.WinApp.UIAutomation` 0.7.0 and `Microsoft.Extensions.DependencyInjection` 10.0.5. The new SDK target exposes the borderless-capture API; it does not make that API available on older Windows versions. The retained package lock records transitive versions. The initial build reused the historical probe's package directory as a read-only NuGet fallback and placed new restore metadata and outputs here.

## Host integration

Construct `CapturePreview(dispatcher, onCommand)` on the WPF dispatcher. `Start(hwnd,title)`, `Stop()`, `SetPaused(bool)`, `SetStatus(string)`, and `Dispose()` are dispatcher-only. `Snapshot(includeImage)` uses immutable frozen image data and can run off-thread. Its image is PNG base64; `AgeMs=-1` means no available frame.

The pause button sends `pause` or `resume`; the host should actually pause/resume automation and acknowledge with `SetPaused`. The image continues live while automation is paused. Stop-button and preview-close events stop local capture and send `stop`; target closure also sends `stop`. `Stop()` preserves `IsClosed` until a subsequent `Start`, allowing the host to report closure accurately. Explicit Stop closes the plugin-owned preview. The borderless surface supports user-initiated dragging outside buttons; native edge resizing keeps the capture's aspect ratio. Controls appear as overlays while hovering; paused or attention state can remain visible as a compact badge without persistent buttons.

## Bridge cancellation regressions

`python -B ./cancel_audit.py` imports the real MCP bridge and tests its `Native` implementation with disposable hidden Python subprocesses. The retained `cancel-results.json` passed all three checks in 172 ms: a queued pre-Stop attach with its acceptance generation cannot restart a controller; Stop terminates an actual child while its unread stdin blocks a writer; malformed controller protocol terminates the owned child and invalidates prior requests. Both children exited. This test does not launch the controller or manipulate an application window.
