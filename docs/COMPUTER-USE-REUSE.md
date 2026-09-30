# Computer Use reuse audit

Read-only audit, 2026-09-29. No Windows input, capture, helper launch, private
protocol access, binary patching or dependency changes were performed.

**Conclusion:** reuse the installed Computer Use service through its supported
API where appropriate, and reuse public library components inside
BetterWinControl. The installed Windows input service is not an independent
background-input backend: its skill states SendInput is used, and its API
states input methods automatically activate their target window.

The installed plugin is available. Root already verified importing `sky` from
`@oai/sky` in the supported node_repl and its `target: "windows"` export. The
documented API exposes window discovery, UIA state, WGC screenshots, input and
activation. The audited cache supplies documentation and an icon, not a
redistributable native implementation with a verified source license. Runtime
access and permission to copy implementation internals are different claims.

Local references read in full:

* [Installed skill](<codex-home>/plugins/cache/openai-bundled/computer-use/26.903.61454/skills/computer-use/SKILL.md)
* [API contract](<codex-home>/plugins/cache/openai-bundled/computer-use/26.903.61454/docs/api.md)
* [Runtime guidance](<codex-home>/plugins/cache/openai-bundled/computer-use/26.903.61454/docs/guidance.md)

| Area | Actual reuse / reusable component | Status and boundary |
| --- | --- | --- |
| Installed Computer Use | Supported `@oai/sky`: `list_windows`, `list_apps`, `get_window`, `get_window_state` | Available in its node_repl host. Useful as an official observation/comparison service, after explicit target selection. No standalone redistribution or private helper client is established. |
| Capture | Microsoft `Microsoft.Windows.SDK.BuildTools.WinApp.UIAutomation` 0.7.0: `IWindowCapture`, `IFrameGrabber`, WGC implementation | Already a direct dependency. `CapturePreview` resolves `IWindowCapture` through `AddWinAppUiAutomation` and requires `WgcWindowCapture`; this is actual library reuse. |
| UI Automation interop | `Interop.UIAutomationClient` 10.19041.0 | Already a direct dependency. `WindowController` uses `CUIAutomation8`, cached properties and UIA pattern interfaces. This is a public interop package, not copied OpenAI engine code. |
| Image/window coordinates | WinApp `CaptureCoordinates.ToScreenPoint`, `CaptureGeometry`, public pointer geometry models | Concrete public components to evaluate for replacing duplicate geometry code. Preserve BetterWinControl's frame ID, target identity, DPI and padding validation. |
| Owned-window discovery | WinApp `IOwnedWindowFinder.FindOwnedWindows`; registered implementation `RealOwnedWindowFinder` | Public interface can be resolved through the existing DI package. Reuse its discovery implementation, then apply our PID/thread/owner-chain, credential checks and lifetime binding. Discovery does not authorize input or prove a modal adapter. |
| Key parsing | WinApp `KeyStringParser.Parse`, `KeyChord`, `TextInput` | Now used directly by `controller/KeyChordParser.cs`, with a keysym alias adapter to the native numeric-VK protocol. The adapter rejects text, multiple actions, ambiguous layout mappings and unsupported extended-key distinctions. No input injector is invoked. |
| UIA selector parsing | WinApp `IUiSelectorParser`, `UiSelector` | Public parser/models are reusable for semantic selectors. Retain our bounded traversal, observation expiry and no-focus policy. Audit any higher-level query/action service for focus or fallback behavior before adopting it. |
| Mouse/keyboard injection | sky input calls; WinApp `IKeyboardInput`, `IMouseInput`, `IPointerInput` | These are not drop-in independent input. The installed sky contract auto-activates; WinApp's packaged README explicitly describes system-wide input. Keep their injectors outside our background backend. |

Existing integration evidence: [project dependencies](../controller/BackgroundControl.csproj),
[resolved versions and content hashes](../controller/packages.lock.json),
[capture integration](../controller/CapturePreview.cs#L95),
[UIA integration](../controller/WindowController.cs#L52).
Capture's 0.7.0 borderless-session adjustment currently depends on a
version-specific reflection adapter because that package lacks the required
public session option. Public MIT WGC source offers a legitimate path to a
small maintained fork or upstream change if that adapter needs replacement.

**Verified source and licensing.** The installed WinApp NuGet metadata declares
MIT, Microsoft authorship, and source commit
`2fdd020c5b7db093e8b3e317b22bdd8135a47e89`. Its packaged README documents both
the public services and their input limitations. The Interop package's local
`LICENSE.txt` is MIT, copyright 2019 Roman. Preserve the copyright/license
notices for distributed dependencies or copied source; keep pinned versions
and the lock-file hashes. These are separate public projects; this audit does
not claim that OpenAI's helper uses their implementations.

Primary public source anchors at the installed WinApp commit:

* [Capture source](https://github.com/microsoft/WinAppCli/tree/2fdd020c5b7db093e8b3e317b22bdd8135a47e89/src/winapp-CLI/WinApp.UIAutomation/Capture)
* [Key parser source](https://github.com/microsoft/WinAppCli/blob/2fdd020c5b7db093e8b3e317b22bdd8135a47e89/src/winapp-CLI/WinApp.UIAutomation/Input/KeyStringParser.cs)
* [Owned-window interface](https://github.com/microsoft/WinAppCli/blob/2fdd020c5b7db093e8b3e317b22bdd8135a47e89/src/winapp-CLI/WinApp.UIAutomation/Input/IOwnedWindowFinder.cs)
* [Owned-window implementation](https://github.com/microsoft/WinAppCli/blob/2fdd020c5b7db093e8b3e317b22bdd8135a47e89/src/winapp-CLI/WinApp.UIAutomation/Input/RealOwnedWindowFinder.cs)
* [Selector parser interface](https://github.com/microsoft/WinAppCli/blob/2fdd020c5b7db093e8b3e317b22bdd8135a47e89/src/winapp-CLI/WinApp.UIAutomation/Services/IUiSelectorParser.cs)
* [WinApp license](https://github.com/microsoft/WinAppCli/blob/2fdd020c5b7db093e8b3e317b22bdd8135a47e89/LICENSE)
* [Interop package](https://www.nuget.org/packages/Interop.UIAutomationClient/10.19041.0)
  and [source/license repository](https://github.com/Roemer/UIAutomation-Interop)

**Concrete parser reuse implemented after this audit:**
[KeyChordParser](../controller/KeyChordParser.cs) calls the existing package's
public parser. `Control_L+Shift_L+p` becomes `[162,160,80]`, while `KP_Enter`
and literal text fail explicitly. [Pure tests](../tests/key-parser/README.md)
exercise the actual package types and production adapter without Windows UI.
The package source was not copied; the dependency is called directly.

**Follow-up audit:** keep the capture/UIA dependencies and new parser. For our
unscaled, unpadded captures, the public coordinate helper reduces to the existing
offset arithmetic; replacing it adds no capability. The library's owned-window
finder returns direct visible owners without our bounded traversal, exact identity
checks or owner-chain validation. Retain the stronger current discovery rather
than replacing it solely to increase the amount of reused code. The actual
coordinate defect was fixed by preserving the capture source separately and
invalidating input on DWM frame, DPI and minimized-state changes.

The native virtual-input transport remains necessary for independent input.
No OpenAI implementation source or binary was copied.
