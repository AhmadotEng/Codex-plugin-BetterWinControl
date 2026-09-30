# Zen live readiness — 2026-09-29

## Current setup: always-ready v0.2.0

The user requested automatic readiness instead of a toolbar gesture for each
control session. v0.2.0 is installed and active in the same existing profile;
its exact payload and existing native-host registration pass read-only validation.
One browser-owned native host stays idle between controller sessions. Each new
connection authenticates and maps the selected native window to a unique browser
window before binding. Stop revokes control while preserving readiness.

Validation: 31 JavaScript, 30 native bridge/core, 16 registration and 9 MCP routing
tests passed; web-ext lint reported zero errors, warnings or notices. These
automated results are separate from the real Codex tool test. Browser restart
persistence remains untested.

Actual Codex-exposed MCP tools then passed three automatic connections in 207,
192 and 42 ms. The third followed over 70 seconds stopped/idle. The same
browser-owned helper remained alive; every Stop revoked the session and exited
the controller. Fresh tab observation after idle succeeded. No toolbar click,
physical input or foreground change was made during these connection tests.
Evidence: `tests/results/zen-always-ready-codex.json`.

Zen remained foreground after the one-time update. The background tab-selection
test was therefore not attempted; this run establishes automatic readiness and
verified binding, not background action or isolation. Final controller state is
stopped/detached and the capture is closed, with the companion still ready.

## Historical v0.1 browser test: passed three consecutive runs

At this historical test, the unsigned v0.1.0 companion was installed and active in the existing
`<existing-zen-profile>` profile. Its payload matches the tested XPI. No
signature/experimental preference, browser binary or profile selection was changed.
Setup pinned the companion button and granted its requested `www.youtube.com`
site access. Its local options page was opened for diagnostics. Existing Discord,
A4H and uBlock extensions were preserved.

`scripts/zen-live-test.py --run --hwnd 1377402` connected the development MCP
controller to the actual extension and verified each of these operations in
three consecutive runs:

- Select existing Stremio tab 3 in Zen PID 14056, HWND 1377402.
- Select existing YouTube video tab 21, "Building a NEW kind of invisible PC".
- Start media playback and independently read back `paused=false`.
- Pause the media and independently read back `paused=true`.

Codex was foreground before the tested browser actions began. The monitor took
52 samples, observed no target foreground/focus acquisition and recorded zero
target foreground/capture events. Setup used Computer Use to install the XPI,
grant YouTube access, click the companion button and return to Codex; those setup
actions are not counted as background-control evidence. The test ended with
YouTube selected and paused, and Stop revoked the session and closed the development
controller. No injected native input helper was used in this browser-semantic test.

Evidence: `tests/results/zen-live.json`; failed and successful attempts are kept
under `tests/results/zen-live-runs/`. The transient `lastPairingResponse` error
in the successful report precedes the successful pairing; all recorded actions
ran after pairing. Multiple existing Stremio/video tabs were retained. The runner
records exact selected IDs and its selection rule, without creating test tabs.

The first live attempts exposed failed native-host discovery. Zen's existing
HKCU handle returned NAME_NOT_FOUND for the host key even though a fresh HKCU
lookup under Zen's token succeeded. Windows registry tracing established the
actual failed lookup. Registering the same manifest at the supported fallback
`HKLM\SOFTWARE\Mozilla\NativeMessagingHosts\local.betterwincontrol.zen` (64-bit)
fixed discovery. The original HKCU entry remains. Readiness now passes with
`--allow-existing-unsigned --scope machine`; this explicit scope is not a blanket
registry or permissions change. Mozilla signing was unnecessary for this user's
already-enabled local unsigned-extension configuration.

This proves live tab selection and media play/pause with the development controller.
It does not prove general pointer/keyboard input, hover, dragging, shortcuts,
browser chrome, restart persistence, the full 60-second co-use gate, or system-wide
control. The previously installed plugin remains available and has not been replaced.

## Earlier investigation and setup

The development controller was attached read-only to the existing official Zen
window (PID 14056, HWND 1377402). Before/after attachment foreground and focus
were unchanged. The session stopped normally; no native helper was injected.

The new paginated accessibility search completed without truncation for each of
Stremio, Pause and YouTube (seven pages per query). It found the inactive Stremio
document and the active YouTube document/player. It did not find a usable
Stremio tab-selection control or the YouTube Pause button. No invoke, tab switch,
native input, browser profile change or successful playback action was established
by that earlier accessibility-only check.
Unrelated tab contents are deliberately not copied into this report.

The companion extension supplies the verified route for these specific browser
operations. Its unsigned package is
`extensions/zen/dist/betterwincontrol-zen-0.1.0-unsigned.xpi`. At the time of that
initial check it was not installed or signed, and its native messaging host was
not registered. Automated extension checks do not establish real Zen
mapping, tab selection or media control.

Subsequent setup prepared protected configuration under `runtime/zen-bridge/`
and registered `HKCU\Software\Mozilla\NativeMessagingHosts\local.betterwincontrol.zen`
to `runtime/zen-bridge/native-host/local.betterwincontrol.zen.json`. Registry
read-back matched, but live discovery later required the HKLM fallback above.
The package remains unsigned. No browser
profile preference was changed.

The user corrected the earlier signing prerequisite: the existing profile
`<existing-zen-profile>` already has `extensions.experiments.enabled=true`
and `xpinstall.signatures.required=false`, with the existing Discord/A4H unsigned
extensions active. The user authorized installing and testing the companion
using that current configuration. Mozilla signing is optional for this local
route. Preserve the existing preferences, profile and saved extensions.

`extensions/zen/scripts/register_native_host.py --profile '<existing profile path>'
--allow-existing-unsigned` verifies that the installed companion is active and
enabled, has the fixed ID, is inside the selected profile, and matches the tested
payload. The command is read-only unless `--register` is supplied. Its default
policy still requires signed metadata. The approved flag does not alter browser
configuration or establish successful browser actions.

The installed companion subsequently passed active-state and exact-payload
readiness, but Zen reported `No such native application` on connection. A short
built-in Kernel-Registry ETW capture recorded PID 14056 opening the exact host
key with `STATUS_OBJECT_NAME_NOT_FOUND` (`0xC0000034`) in HKCU and both supported
HKLM fallback views. A read-only test with Zen's duplicated token could open a
fresh HKCU handle and read the manifest. Duplicating Zen's actual cached HKCU
handle instead reproduced `ERROR_FILE_NOT_FOUND` for the same key in both
registry views. The underlying cause of those different handle results was
not established. Registry paths, ACLs and browser preferences were not altered
to diagnose it.

The same manifest was then registered at the browser's supported HKLM64 host
key, with no pre-existing conflicting value and successful readback. Use
`--scope machine` with the readiness helper to check that explicit fallback;
it now passes. The filtered diagnostic record is
`runtime/zen-bridge/diagnostics/native-lookup-filtered.json`. The trace session
was stopped and the raw ETL/XML files removed; only the three matching native
host events and relevant read-only checks are retained. This establishes host
lookup evidence, not a successful browser action.

The historical v0.1 test used a toolbar gesture after native-host registration;
v0.2 replaces that step with automatic binding. Those three Stremio/YouTube runs
passed; restart persistence remains untested. Hover, coordinate dragging, keyboard
shortcuts, browser chrome and other Windows apps remain separate unfinished
general-controller tests; tab/media commands do not establish them.

`scripts/zen-live-test.py --run --hwnd <verified-current-HWND>` implements that
three-run live sequence through the real MCP and extension bridge. Its command
line/import check and actual v0.1 three-run browser sequence passed. The current
runner connects automatically, then waits for Zen to remain unfocused. A failed
effect or observed foreground/capture acquisition stops the sequence. Passing
this short semantic test does not satisfy the longer co-use or full-system gate.

Sources: [Mozilla signing](https://www.extensionworkshop.com/documentation/publish/signing-and-distribution-overview/)
and [unlisted submission](https://www.extensionworkshop.com/documentation/publish/submitting-an-add-on/).
