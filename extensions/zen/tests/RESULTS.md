# Validation record

The following commands succeeded on this Windows machine during implementation:

| Check | Result | Evidence scope |
|---|---|---|
| `node --test tests/adapter.test.js tests/page.test.js` |17/17 pass | Browser API mocks and jsdom document/media fixtures |
| `python -m unittest discover -s tests -p 'test_*.py' -v` |23/23 pass |10 bridge tests,7 controller adapter tests,6 root MCP routing fixtures |
| `web-ext10.7.0 lint --source-dir extension --output text` |0 errors,0 warnings,0 notices | Firefox extension static validator; not signing/AMO approval |
| deterministic XPI build twice |matching SHA-256 | Package byte reproducibility |

Focused validation after adding the approved existing-unsigned readiness route and explicit registration scope:

| Check | Result | Evidence scope |
|---|---|---|
| `python extensions/zen/tests/test_native_registration.py -v` |16/16 pass | Default signed policy; explicit unsigned route; active/enabled, fixed-ID, selected-profile and payload checks; no profile writes; explicit HKLM selection, conflicting value preservation, readback failure and CLI behavior with fixture-only registry |
| `python extensions/zen/tests/test_core_adapter.py` |8/8 pass | Current core adapter fixtures, including native launcher arguments; simulated browser binding remains separate from live Zen |

XPI: `dist/betterwincontrol-zen-0.1.0-unsigned.xpi` (10,806 bytes).

SHA-256: `d5f2a94373a4acd1647591b62fc6007103977dea90674527cc4eb0bff0a252da`.

The native bridge test launches the actual `native_host.py` as a separate hidden Python process, exchanges native-messaging frames with it over stdin/stdout, and connects it to the actual current-user-only Windows named pipe using DPAPI-protected fixture configuration. Timeout closes the pipe and the host exits even while browser stdin remains open. Wrong HMAC, unauthorized OS PID, and cancelled accept/read are tested. This exposed and fixed a real CRT duplex pipe deadlock; production transport now uses cancellable overlapped Win32 I/O.

The controller fixture exercises prepare/connect/capabilities/pair/observe/input/verify and epoch revocation over that real transport, but simulates browser process ancestry and HWND/window geometry. Actual own-process identity/command-line reading has a separate OS test. The root MCP fixture imports the real router with an inert replacement for native operations, so it neither starts the controller nor manipulates user apps. It verifies not-ready errors, unsupported errors, stale-generation rejection, argument rejection, disconnect without native launch, and native-frame invalidation before browser input.

**Not established by these fixtures:** persistent browser installation, Mozilla signing, real Firefox launcher ancestry, live selected-Zen HWND correlation, multi-monitor DPI mapping, browser restart behavior, Stremio/YouTube website behavior, actual focus/pointer isolation during concurrent user input, or arbitrary browser mouse/keyboard support. Signing is optional under the user's approved existing unsigned-extension configuration; the other claims still need live acceptance evidence. No production host registration, live browser profile setting, production bridge configuration, or add-on installation was performed by these tests. See [the live status](../../../docs/ZEN-LIVE-STATUS.md) for separately recorded browser results.
