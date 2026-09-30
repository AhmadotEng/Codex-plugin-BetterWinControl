# Always-ready Zen companion 0.2.0

The existing official Zen profile now has companion 0.2.0 installed and enabled.
It starts its local native connection automatically. A Codex request connects to
the selected window without a toolbar click. Stop revokes the current control
session and leaves the companion idle for the next request.

## What changed

- `extensions/zen/extension/background.js`: startup connection, reconnect backoff,
  transport-generation cancellation and persistent readiness setting.
- `extensions/zen/extension/adapter.js` and `protocol.js`: fresh pairing candidates
  and window geometry for automatic, verified binding.
- `extensions/zen/extension/options.html` and `options.js`: explicit readiness
  enable/disable. Disabling persists until the user enables it again.
- `extensions/zen/extension/manifest.json`: version 0.2.0 and updated description;
  extension identity and required permissions preserved.
- `extensions/zen/native/native_host.py`: one browser-owned helper waits for
  authenticated controller connections and survives normal controller Stop.
- `extensions/zen/native/core_adapter.py`: automatic pairing with a unique match
  between browser windows and the selected native window, checked before and after
  binding. Ambiguous matches are rejected.
- `extensions/zen/native/bridge.py`: canceled listener and pipe teardown fixes.
- `scripts/mcp_server.py`: connection runs outside the native-operation lock so the
  authentication callback can read controller state. Concurrent Stop still cancels
  the connection. Capability descriptions reflect automatic readiness.
- `extensions/zen/scripts/register_native_host.py`: expected XPI version is read
  from the current manifest instead of being hardcoded to 0.1.0.
- `scripts/zen-live-test.py`, companion tests, plugin skill and setup documentation:
  updated for automatic connections and separate connection/effect verification.

## Installed files

- Profile: `<existing-zen-profile>`.
- Add-on: `extensions\zen-companion@betterwincontrol.local.xpi` in that profile.
- Built package: `extensions/zen/dist/betterwincontrol-zen-0.2.0-unsigned.xpi`.
- SHA256: `9461923ffd894895d38704f9213b3da8adf1f6edfd9e728ed8a1b264940fca20`.
- Local plugin cache: `0.1.0+codex.20260930041209` (instruction/documentation refresh; initial v0.2 live-test cache was `0.1.0+codex.20260930032849`).
- Plugin source: `<installed-plugin-root>`.
- Development server: `<repository-root>\scripts\mcp_server.py`.

The old XPI is preserved. Existing browser preferences, profile selection,
site grants and native-host registry entries were retained. No Windows startup
service or scheduled task was added. The helper runs with the open browser.

## Validation and limits

31 JavaScript tests, 30 native bridge/core tests, 16 registration tests and nine MCP
routing tests passed. web-ext lint reported no errors, warnings or notices. Plugin
and skill validators passed. Read-only installed-add-on validation confirms active
version 0.2.0, matching payload and ready native-host registration.

Real Codex tools passed three automatic connections in **207, 192 and 42 ms**.
The third followed over 70 seconds stopped/idle; the same helper remained ready.
Fresh tab observation succeeded. Every Stop revoked control and closed PiP.
Evidence is recorded in `tests/results/zen-always-ready-codex.json`.

Zen remained foreground after setup, so no background tab-selection action was
attempted in this run. Connection readiness, verified tab selection and background
input isolation are separate checks.

Browser restart persistence has not been tested. Identical/ambiguous window bounds
are rejected; another browser profile racing for the same local pipe can require
reconnection. A new site's page operations can still require a one-time site grant.
These changes do not establish general Windows pointer/keyboard compatibility.

## Test prompt

Use a new Codex task with the updated plugin:

> Use BetterWinControl and its ready Zen companion to select my existing Stremio tab in the background. Verify the selected tab, then stop control. Leave my mouse and keyboard alone.

No companion-toolbar click or repeated installation is part of this test.

