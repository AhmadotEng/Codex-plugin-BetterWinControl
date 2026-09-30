# BetterWinControl Zen companion

Optional browser adapter for the general Windows controller. This is an ordinary Firefox WebExtension for the existing official Zen installation. It does not rebuild Zen, change browser preferences, inject privileged browser code, or install itself. It can use an unsigned installation accepted by the user's existing browser configuration.

## v0.2 always-ready connection (current development)

The companion starts its native messaging host when the extension loads and keeps
one idle connection ready while Zen is open. A requested controller session
connects automatically, authenticates the browser-owned helper, and binds only a
unique native/browser window mapping. No per-session toolbar gesture or foreground
switch is required. Duplicate window geometry is rejected when mapping is ambiguous.

Stop closes the current controller session. The helper returns to idle readiness;
it does not resume old commands. Disabling readiness in extension options persists
until the user explicitly enables it again. Site grants are unchanged: page actions
on an ungranted origin still need the normal explicit grant.

Package: `dist/betterwincontrol-zen-0.2.0-unsigned.xpi`, same extension ID and profile.
No Windows startup service, scheduled task, browser rebuild or preference change.
Actual installation/idle/reconnect results will be recorded in `docs/ZEN-LIVE-STATUS.md`.
The sections below preserve v0.1 implementation history; their toolbar-only setup
instructions and old acceptance status are superseded by this section.

## Current deliverable

- `dist/betterwincontrol-zen-0.1.0-unsigned.xpi`: deterministic, **unsigned** add-on package. The user approved installation using the existing Zen unsigned-extension configuration. Mozilla signing is an optional distribution route; lint passing is not AMO approval.
- `extension/`: full readable source; fixed ID `zen-companion@betterwincontrol.local`, minimum Firefox engine142.
- `native/bridge.py`: stdlib-only Windows broker transport and DPAPI configuration APIs, with no import-time side effects.
- `native/core_adapter.py`: controller facade with selected-window/epoch checks, exact native-host ancestry checks, and explicit prepare/connect/pair lifecycle. Its production OS binding is implemented but still requires an installed-extension regression.
- `native/native_host.py`: native-messaging relay. `scripts/prepare_native_host.py` stages a launcher/manifest without registering it or creating production configuration.
- `scripts/register_native_host.py`: read-only readiness check by default; `--register` explicitly registers only the validated native-host manifest in the selected registry scope. `--scope user` (default) selects HKCU64; `--scope machine` explicitly selects the browser's supported HKLM64 fallback. It refuses conflicting registrations and verifies write readback. It independently reports whether the existing profile has the active extension with the exact tested payload. Signed metadata is required by default; `--allow-existing-unsigned` accepts an already-active unsigned companion under the existing browser configuration. Neither option changes preferences or installs the extension.
- Tests use mocked browser APIs, a DOM fixture, and a real separate native-host process over an actual Windows named pipe. **Official Zen installation, OS HWND binding, same-desktop input isolation, and real Stremio/YouTube behavior have not been tested by these fixtures.**

## Working scope

The toolbar button requests the current site's optional host permission and creates a 30-second pairing nonce for that browser window. The controller must independently validate the selected OS window and native-host process before binding that window. A fresh observation is required before every input.

Supported: list/select tabs in that paired window, navigate to an explicitly granted HTTP(S) site, inspect visible main-frame elements, set ordinary form values (including Unicode), semantic DOM click, scroll, and HTML media play/pause/seek. `verify` separately reads resulting state. A generic click reports an unknown effect until the controller observes an intended page change. Page URLs are returned without query or fragment; passwords/sensitive fields and visible authentication pages are excluded. This filter is precautionary, not a guarantee that observed page text contains no personal data.

DOM events are synthetic. This adapter does **not** provide trusted pointer/keyboard input, hover, drag, keyboard chords, file input, subframes, browser menus/chrome, or native dialogs. Those require another proven backend in the general controller. Direct BiDi is deliberately not enabled or implemented here; no browser debug prefs/endpoints are changed.

Pause blocks new mutations and invalidates observations. Stop, window closure, permission removal, and native disconnect revoke bindings. Resuming requires a fresh observation; restarting a stopped session requires a new toolbar nonce and higher epoch. An operation already submitted to a browser API may finish after Stop and cannot be rolled back.

## Native security and integration

See [PROTOCOL.md](PROTOCOL.md) for the callable broker interface and exact binding obligations. The Windows pipe has a protected DACL granting only the current user's SID, rejects remote clients, and uses first-instance protection. Discovery through a stable local pipe name is not authorization. Both sides authenticate fresh challenges/nonces using HMAC-SHA256 with role/protocol/pipe domain separation. A 32-byte per-install secret is stored using current-user DPAPI and a user-only protected file/directory ACL.

The broker additionally **must** validate the OS-retrieved client PID, exact expected executable/script/launcher, live process ancestry to the selected Zen PID, process creation time, and selected HWND identity. `CoreAdapter` implements that policy; it also requires a fresh30-second toolbar event and matching window geometry. Its native host's foreground metadata is advisory and never selects a target by itself. Lower-level bridge fixtures authorize only their own spawned test process. The end-to-end core fixture uses actual native messaging and pipes with simulated HWND/browser identity, so it does not prove real Zen window binding.

Production integration creates configuration explicitly with `write_config`, stages the native launcher for a fixed Python executable, and explicitly registers its manifest in the selected scope (HKCU64 by default, or HKLM64 with `--scope machine`). Host registration does not make the browser adapter ready until the browser accepts the installed add-on and the selected window is paired. The core bridge does not require Mozilla signing; browser installation policy is separate. The staging generator does not write registry entries. The supported launcher adds `cmd.exe` between Firefox and Python; the broker must validate that chain, not merely accept any Python child. [Mozilla's Windows native-messaging example](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/Native_messaging) supports this `.bat` launcher form.

## Build and checks

From this folder, using Node and Python3.11+:

```text
pnpm install --ignore-scripts --frozen-lockfile --store-dir .pnpm-store
node --test tests/adapter.test.js tests/page.test.js
python tests/test_bridge.py
python tests/test_core_adapter.py
python tests/test_mcp_browser.py
python tests/test_native_registration.py
node node_modules/web-ext/bin/web-ext.js lint --source-dir extension --output text
python scripts/build_xpi.py
```

The package contains only `extension/` files. Native scripts and development dependencies are separate. Build output records its SHA-256 in `dist/build.json`. Two independent builds are compared in the test suite.

Current validation:17 JavaScript behavior tests and23 Python tests pass, including6 fixtures that import the real root MCP router and prove unavailable/unsupported browser operations surface as MCP errors. `web-ext10.7.0 lint` reports zero errors, warnings, or notices. See [tests/RESULTS.md](tests/RESULTS.md) for scope and artifact hash. Do not package `node_modules/`, `.pnpm-store/`, `__pycache__/`, or `tests/results/` into the installed plugin; they are development/fixture files and ignored by Git.

The existing-unsigned readiness and explicit registry-scope updates additionally pass all 16 registration checks and the current 8 core-adapter fixtures; the XPI payload and hash are unchanged. These checks do not establish live browser behavior.

For optional signing, submit the XPI/source through the user's Mozilla developer account as an unlisted add-on if private distribution is desired. Preserve this fixed extension ID and the browser's existing preferences. The current authorized local route uses the user's already-enabled unsigned-extension configuration. Manifest consent declares browsing activity, website activity, and website content because native messaging transmits that data to the local controller and possibly into the Codex task; it is not accurate to declare no data transmission. No analytics or direct remote endpoint exists in this add-on. See [signing](https://extensionworkshop.com/documentation/publish/signing-and-distribution-overview/) and [Mozilla data consent](https://extensionworkshop.com/documentation/develop/best-practices-for-collecting-user-data-consents/).

See [SIGNING-AND-LIVE-TEST.md](SIGNING-AND-LIVE-TEST.md) for the exact upload file, installed native-host paths and remaining real-browser test sequence.

## Live acceptance gates, still pending

1. The browser-accepted XPI persists in the user's existing profile, connects only after toolbar pairing, and reconnects only after a fresh pairing. Check with the chosen readiness policy: signed by default, or the explicit existing-unsigned route. No extra profile or browser debug endpoint.
2. Two Zen windows: only the explicitly selected HWND/PID/start-time can bind; ambiguous mappings, wrong PID, changed process identity, stale nonce, or another user's connection fail closed.
3. While the user types and moves their mouse in another app, select a tab and operate a granted test page; selected foreground window, keyboard focus, and pointer stay with the user. Never silently fall back to foreground/global input.
4. Test Stremio navigation and YouTube pause/seek with state verification. Hover/drag/chords must report unsupported from this adapter rather than claim DOM clicks implement them; test those separately through the general input backend.
5. Test Unicode editing, navigation/reload between observe/input, removed nodes, site permission removal, tab/window closure, Pause/Stop during waits, broker death, native-host death, and restart. No late queued mutations or automatic rebind.

Reference: [`tabs.update({active:true})` does not focus a window](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/API/tabs/update); this documented behavior still needs the actual Zen/concurrent-desktop regression above. [`dispatchEvent`](https://developer.mozilla.org/en-US/docs/Web/API/EventTarget/dispatchEvent) is programmatic event dispatch, not arbitrary physical input.
