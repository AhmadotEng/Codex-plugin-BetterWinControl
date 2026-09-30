# Broker integration contract, version1

## v0.2 automatic readiness (current contract)

The browser starts one native messaging host when the companion is enabled. The
host waits without reading page content or sending input when no controller owns
a session. Its single lifetime stdin reader routes only replies belonging to the
current authenticated transport generation. Broker disconnect revokes extension
sessions before a new transport can deliver commands. Browser EOF ends the host.

Controller `connect` creates an authenticated on-demand broker and attempts binding
without any toolbar gesture or foreground change. Its accept thread can query core
identity while connect waits; the MCP router releases its native-operation lock for
that wait and rejects a result if Stop changes the controller generation.

New pre-binding operation `pair_candidates` returns fresh expiring nonces and bounds
for accessible normal non-private browser windows. The core compares all candidate
windows against all visible MozillaWindowClass windows belonging to the verified
browser PID, requiring a unique match in both directions. Exact process, creation
time, HWND and thread identity are retained. Bind returns fresh browser bounds for
post-bind verification; ambiguous or changed mappings revoke the session. Titles
and foreground focus are not used as automatic window identity evidence.

Native host transport notifications `{v:1,type:"transport",state:"waiting"}` revoke
old extension bindings; `state:"connected"` permits new authenticated requests but
does not itself bind or resume anything. Core Stop returns to idle readiness.
Extension readiness disable persists until explicit re-enable. Ordinary HTTP(S)
site grants remain separately required for page operations.

The sections below preserve the v0.1 toolbar-pairing contract for historical
reference. v0.2 replaces its manual pair event and host-exits-on-broker-close
behavior; framing, HMAC authentication, selected-window ownership and fresh-action
verification remain in use.

## Controller facade (v0.1 history)

Root normally imports `native/core_adapter.py` (with its `native` directory on `sys.path`) and constructs `CoreAdapter(repo_root: Path, read_core_state: callback)`. `call(operation, arguments)` supports prepare/connect/pair/capabilities/observe/input/verify/disconnect and returns an `{ok: ...}` result. `cancel()` is thread-safe, bypasses tool serialization, cancels accept/pending I/O, and revokes state immediately. Root must call it on Pause/Stop/parent loss and any target selection change. The constructor writes nothing.

`read_core_state()` returns `{attached,paused,sessionEpoch,window:{hwnd,pid,startTicks,threadId,className,process}}`; startTicks is .NET UTC ticks since year1. The selected HWND must belong to Zen and have `MozillaWindowClass`. State and OS process/window identity are checked before/after each operation. Process identity is read from Windows handles, command lines from `NtQueryInformationProcess`, and ancestry from Toolhelp. Only exact Python `-u native_host.py`, directly parented by selected Zen or through the exact system cmd.exe + staged launcher, is allowed; unknown command arguments/wrappers fail closed. Parent process creation-time order is checked against PID reuse.

Windows launch audit against installed official Zen1.22.3b (Gecko156.0.1), `C:\Program Files\Zen Browser\omni.ja`: `modules/NativeMessaging.sys.mjs:114` supplies the manifest path and add-on ID; `modules/Subprocess.sys.mjs:146` prepends the launcher; `modules/subprocess/subprocess_win.worker.js:552–554,592–594` invokes the full system command processor with the raw command `cmd.exe /s/c "<quoted launcher> <quoted manifest> <fixed add-on ID>"`. The authorizer now recognizes exactly this generated form, including whitespace/nested quotation, and still requires the OS image, process ancestry and creation times. Wrong manifest/ID, extra commands/arguments and unknown wrappers are rejected. The prior single-launcher form remains supported separately; it is not used to loosely parse Gecko's shell tail. This was source-verified and fixture-tested, not a claim of completed live pairing. Primary reference: [Mozilla NativeMessaging source](https://searchfox.org/firefox-main/source/toolkit/components/extensions/NativeMessaging.sys.mjs), [Windows subprocess source](https://searchfox.org/firefox-main/source/toolkit/modules/subprocess/subprocess_win.worker.js).

`prepare` explicitly creates `<repo>/runtime/zen-bridge/config.json` with DPAPI/user-only ACL and stages launcher/manifest beneath that dedicated directory. It does not register HKCU, install the add-on, sign anything, or claim those steps happened. `connect` listens for up to30 seconds. `pair` consumes the latest explicit toolbar nonce only after one unambiguous fresh event matches the selected native HWND/PID/thread/start time and OS geometry (physical or DPI-scaled). Mixed-DPI geometry that cannot be corroborated fails closed; a title is never used as a window identity. Actual Zen/launcher ancestry remains an installation acceptance gate.

## Lower-level broker API

Import `native/bridge.py` by adding its directory to the controller's module search path or using `importlib.util.spec_from_file_location`. Importing writes nothing and launches nothing.

```python
bridge.write_config(absolute_config_path, local_pipe_name, secret_32_bytes)  # explicit setup only; refuses overwrite
pipe_name, secret = bridge.read_config(absolute_config_path)
server = bridge.BrokerServer(pipe_name, secret, authorize_client)
connection = server.accept(on_event=on_event, timeout=30)
result = connection.request(session_id, epoch, "capabilities", {}, timeout=5)
connection.close()  # revokes connection and cancels native pipe I/O
```

Pipe name must begin `\\.\pipe\BetterWinControl-Zen-`, with no nested/remote path. A per-user stable name is discovery only. Configuration path may be the controller's per-repository runtime directory; set `BWC_ZEN_CONFIG` to that absolute file path in the generated launcher. No secret crosses extension messaging or is returned in broker metadata.

`authorize_client(os_pid)` is mandatory. Return a metadata dictionary only after checking OS process image/command line, exact approved helper/launcher path or hash, browser ancestry, and live selected browser PID/start time; otherwise return `None`. The PID comes from `GetNamedPipeClientProcessId`, not client JSON. Parent process IDs must be guarded against PID reuse using creation times. Reject unrecognized wrapper chains. The library's callback hook supplies no default production identity policy.

`on_event(message, peer)` executes on the pipe reader thread. Store or enqueue the event; **do not call `connection.request` synchronously from this callback**. `peer` includes the OS `clientPid` and the authorizer's metadata. Callback failure closes the connection. A validated hello sets `connection.hello` (threading.Event); it is not passed to `on_event`.

Pair event:

```json
{"v":1,"type":"event","event":"pair_requested","pairingNonce":"uuid","windowId":12,"expiresAt":1790000000000,"window":{"id":12,"left":0,"top":0,"width":1200,"height":900,"type":"normal"},"tabId":15,"nativeObservation":{"available":true,"hwnd":123456,"pid":9000,"threadId":9001,"capturedAtUnixMs":1790000000000,"advisoryOnly":true}}
```

Before `bind`, revalidate the core's active selected HWND, root-owner HWND, live browser PID/start time, native-host ancestry, matching toolbar window metadata, and fresh nonce. Reject ambiguity or any foreground/window mismatch and require a new toolbar gesture. **There is no ordinary WebExtension API returning an HWND**; bounds and foreground metadata are only corroborating evidence. Do not claim unconditional mapping where the controller cannot prove it. The core must continue checking its selected HWND/PID/start-time on each mutation and close this connection if selection is revoked/changed. A browser tab moving to another window is rejected in the extension.

`connection.request(session_id, epoch, op, args, timeout)` generates unique request ID and absolute deadline. Timeout is positive and at most30 seconds; it closes the connection and raises `TimeoutError` with unknown outcome. Do not blindly retry mutations. Protocol errors raise bounded `RuntimeError` codes. Returned values are result payloads, not the outer reply envelope.

| Operation | Args | Result |
|---|---|---|
| capabilities | `{}` | supported operations and truthful backend flags |
| bind | `{windowId,pairingNonce}` | `{bound:true,windowId,epoch}` |
| observe | optional `{tabId,includePage,maxElements}` | fresh observationId, paired tabs, optional page/document/element IDs |
| input | `{observationId,action,tabId?,...}` | actionId, submitted/effect state, verifyRequired |
| verify | `{actionId}` | separate actual state/effectVerified; generic click may remain null |
| pause/resume/stop | `{}` | lifecycle state; Stop says inFlightMayFinish |

Input actions: `select_tab`; `navigate` with `url`; `click` with `elementId`; `set_value` with `elementId,value`; `scroll` with `elementId,x,y`; `media_pause`, `media_play` with `elementId`; `media_seek` with `elementId,seconds`. IDs come only from the latest observation. Observations expire after15 seconds and are consumed by an input. Page operations need a host permission granted through a user's toolbar gesture on that origin. After navigation, observe the new document before acting.

Other events: `window_closed` with windowId, `permissions_revoked`, `user_stop`. Treat native disconnect as revocation even if a stop acknowledgement was lost. Never infer completion from an accepted command alone.

Native stdio uses Firefox's 4-byte little-endian length + UTF-8 JSON. Named pipe frames are newline-delimited JSON. Both cap messages at1,000,000 bytes. Authentication uses fresh32-byte challenge and nonce, HMAC-SHA256 over NUL-separated protocol domain `BetterWinControl/ZenNative/v1`, role, lowercased pipe name, challenge, and nonce. Client/server proofs use separate roles. Authentication is bounded to5 seconds; named pipe connects and pending reads can be cancelled. The current user's malicious code can decrypt their DPAPI data and tamper with their files; this is not a security boundary against compromise of that same Windows account.
