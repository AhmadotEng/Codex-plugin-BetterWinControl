"""Owned-fixture fault-injection tests for controller lifetime and cancellation.

Only processes started by this test are terminated. Cancellation briefly suspends
the owned fixture's verified GUI thread to create a blocked accessibility read;
the thread is resumed in a finally block. No user application is operated.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
import traceback

from co_use_test import BoundedProcess, configure_api, controller_children, owned_visible_windows
from integration_test import PLUGIN, TESTS, action_args


REPORT = TESTS / "results" / "lifecycle.json"


def configure_lifetime_api(kernel):
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenThread.restype = wintypes.HANDLE
    kernel.GetProcessIdOfThread.argtypes = [wintypes.HANDLE]
    kernel.GetProcessIdOfThread.restype = wintypes.DWORD
    kernel.SuspendThread.argtypes = [wintypes.HANDLE]
    kernel.SuspendThread.restype = wintypes.DWORD
    kernel.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel.ResumeThread.restype = wintypes.DWORD


class OwnedController:
    def __init__(self, kernel, user, server_pid: int):
        pids = controller_children(kernel, server_pid)
        self.kernel, self.user = kernel, user
        self.handle = None
        expected = PLUGIN / "runtime" / "BackgroundControl.exe"
        matches = []
        for pid in pids:
            handle = kernel.OpenProcess(0x100000 | 0x1000 | 0x1, False, pid)
            if not handle:
                continue
            image = ctypes.create_unicode_buffer(2048)
            size = wintypes.DWORD(len(image))
            try:
                is_expected = (kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size))
                               and Path(image.value).samefile(expected))
            except OSError:
                is_expected = False
            if is_expected:
                matches.append((pid, handle))
            else:
                kernel.CloseHandle(handle)
        if len(matches) != 1:
            for _, handle in matches:
                kernel.CloseHandle(handle)
            raise AssertionError(f"Expected one verified controller child; got {len(matches)}")
        self.pid, self.handle = matches[0]
        self.initial_windows = sorted(owned_visible_windows(user, {self.pid}))

    def exit_evidence(self, timeout: float = 3) -> dict:
        began = time.monotonic()
        result = self.kernel.WaitForSingleObject(self.handle, round(timeout * 1000))
        return {"pid": self.pid, "exited": result == 0,
                "waitMs": round((time.monotonic() - began) * 1000),
                "initialPreviewHwnds": self.initial_windows,
                "remainingVisibleHwnds": sorted(owned_visible_windows(self.user, {self.pid}))}

    def terminate(self):
        if self.kernel.WaitForSingleObject(self.handle, 0) == 0x102:
            assert self.kernel.TerminateProcess(self.handle, 77), "Own native termination failed"

    def close(self):
        if self.handle:
            self.terminate()
            self.kernel.WaitForSingleObject(self.handle, 1000)
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def finish_server(server):
    if server.proc.poll() is None:
        try:
            server.deadline = time.monotonic() + 2
            server.tool("stop")
        except Exception:
            pass
    if server.proc.poll() is None and server.proc.stdin:
        try:
            server.proc.stdin.close()
            server.proc.wait(timeout=1)
        except Exception:
            server.proc.terminate()
    if server.proc.poll() is None:
        server.proc.wait(timeout=1)


def require_exited(evidence):
    assert evidence["exited"], "Native controller orphan remained after deadline"
    assert not evidence["remainingVisibleHwnds"], "Native preview window remained after exit"


def main() -> int:
    began = time.monotonic()
    deadline = began + 45
    user, kernel = configure_api()
    configure_lifetime_api(kernel)
    fixture = None
    servers, native_handles = [], []
    report = {"scope": "Owned-fixture EOF, parent loss, controller crash, explicit recovery and pending cancellation",
              "startedAtUtc": datetime.now(timezone.utc).isoformat(), "checks": [], "passed": False,
              "limits": ["No real application is operated.",
                         "Pending cancellation uses a blocked read; cancellation of already-dispatched writes is not established.",
                         "This lifecycle subset does not establish complete macOS feature parity."]}
    try:
        fixture_exe = TESTS / "fixture" / "bin" / "Release" / "net10.0-windows" / "WindowsBackgroundControlFixture.exe"
        server_path = PLUGIN / "scripts" / "mcp_server.py"
        assert fixture_exe.is_file(), "Fixture must already be built"
        report["testedArtifacts"] = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                                     for path in (server_path, PLUGIN / "runtime" / "BackgroundControl.dll")}
        fixture = BoundedProcess([str(fixture_exe)], deadline)
        ready = fixture.receive(timeout=3)
        assert ready.get("event") == "ready" and ready.get("fixturePID") == fixture.proc.pid
        hwnd = int(ready["hwnd"])
        owner = wintypes.DWORD()
        fixture_tid = user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        assert owner.value == fixture.proc.pid, "Refuse unowned fixture window"

        def start_attached():
            server = BoundedProcess([sys.executable, "-u", str(server_path)], deadline)
            servers.append(server)
            response = server.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                                  "clientInfo": {"name": "owned-lifecycle-test", "version": "1.0"}})
            assert "result" in response
            server.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            server.tool("attach_window", {"hwnd": hwnd})
            native = OwnedController(kernel, user, server.proc.pid)
            native_handles.append(native)
            return server, native

        def passed(name, details):
            report["checks"].append({"name": name, "passed": True, "details": details})
            print(f"PASS {name}", flush=True)

        # A clean MCP client disconnect must revoke the native controller.
        server, native = start_attached()
        server.proc.stdin.close()
        server.proc.wait(timeout=3)
        evidence = native.exit_evidence()
        require_exited(evidence)
        passed("MCP EOF leaves no controller or preview orphan", evidence)

        # Abrupt loss of Python bypasses its finally/atexit code; native parent
        # monitoring must independently stop the process and its preview.
        server, native = start_attached()
        lost_at = time.monotonic()
        server.proc.kill()
        server.proc.wait(timeout=1)
        evidence = native.exit_evidence()
        evidence["parentLossToNativeExitMs"] = round((time.monotonic() - lost_at) * 1000)
        require_exited(evidence)
        passed("abrupt MCP parent loss leaves no native orphan", evidence)

        # Crash the verified native child, then check that old authority cannot
        # silently start a fresh controller. Only explicit attach may recover.
        server, native = start_attached()
        observed, _ = server.tool("observe", {"max_elements": 100, "include_screenshot": False})
        old_action = action_args(observed, "fixture-apply", "invoke")
        old_observation = observed["observation"]["observationId"]
        native.terminate()
        evidence = native.exit_evidence()
        require_exited(evidence)
        time.sleep(0.1)
        server.tool("act", old_action, expect_error=True)
        server.tool("observe", {"max_elements": 100, "include_screenshot": False}, expect_error=True)
        state, _ = server.tool("state")
        assert state["controller"]["attached"] is False and state["controller"]["stopped"] is True, "Crash did not revoke target"
        # Non-controller OS helper children may exist (for example console hosts).
        # A new controller would make state attached/running, checked above.
        server.tool("attach_window", {"hwnd": hwnd})
        recovered = OwnedController(kernel, user, server.proc.pid)
        native_handles.append(recovered)
        after, _ = server.tool("observe", {"max_elements": 100, "include_screenshot": False})
        assert after["observation"]["window"]["hwnd"] == hwnd
        assert after["observation"]["observationId"] != old_observation
        server.tool("act", old_action, expect_error=True)
        passed("controller crash revokes target; explicit attach recovers with fresh authority",
               {"crashed": evidence, "recoveredPID": recovered.pid, "freshObservation": True})
        finish_server(server)
        require_exited(recovered.exit_evidence())

        # Block only our fixture's GUI thread so this real cross-process UIA read
        # stays pending. Cancellation must terminate its native worker promptly.
        server, native = start_attached()
        server.tool("observe", {"max_elements": 100, "include_screenshot": False})
        thread = kernel.OpenThread(0x2 | 0x0800, False, fixture_tid)
        assert thread, "Could not open fixture GUI thread"
        suspended = False
        try:
            assert kernel.GetProcessIdOfThread(thread) == fixture.proc.pid, "Refuse unowned GUI thread"
            previous_count = kernel.SuspendThread(thread)
            assert previous_count != 0xFFFFFFFF, "Fixture suspend failed"
            suspended = True
            assert previous_count == 0, "Fixture was unexpectedly already suspended"
            server.next_id += 1
            pending_id = server.next_id
            server.send({"jsonrpc": "2.0", "id": pending_id, "method": "tools/call",
                         "params": {"name": "observe", "arguments": {"max_elements": 100, "include_screenshot": False}}})
            time.sleep(0.25)
            assert server.lines.empty(), "Blocked provider read was not established; cancellation cannot be claimed"
            cancelled_at = time.monotonic()
            server.send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                         "params": {"requestId": pending_id, "reason": "Owned fixture lifecycle test"}})
            response = server.receive(timeout=2)
            assert response.get("id") == pending_id
            assert response.get("error") or response.get("result", {}).get("isError"), "Cancelled read returned success"
            evidence = native.exit_evidence(timeout=2)
            evidence["cancellationToNativeExitMs"] = round((time.monotonic() - cancelled_at) * 1000)
            require_exited(evidence)
            assert evidence["cancellationToNativeExitMs"] < 2000, "Cancellation was not prompt"
        finally:
            if suspended:
                assert kernel.ResumeThread(thread) != 0xFFFFFFFF, "Could not resume owned fixture GUI thread"
            kernel.CloseHandle(thread)
        state, _ = server.tool("state")
        assert state["controller"]["attached"] is False and state["controller"]["stopped"] is True
        server.tool("observe", {"max_elements": 100, "include_screenshot": False}, expect_error=True)
        passed("pending real UIA read cancels promptly and revokes target", evidence)
        report["passed"] = True
    except Exception as exc:
        report["error"] = str(exc)
        report["traceback"] = traceback.format_exc()
        print(report["traceback"], file=sys.stderr, flush=True)
    finally:
        for server in reversed(servers):
            try:
                finish_server(server)
            except Exception:
                if server.proc.poll() is None:
                    server.proc.kill()
                    server.proc.wait(timeout=1)
        for native in native_handles:
            native.close()
        if fixture:
            if fixture.proc.poll() is None:
                try:
                    fixture.proc.stdin.write("stop\n")
                    fixture.proc.stdin.flush()
                    fixture.proc.stdin.close()
                    fixture.proc.wait(timeout=1)
                except Exception:
                    fixture.proc.terminate()
                    fixture.proc.wait(timeout=1)
            report["fixtureExited"] = fixture.proc.poll() is not None
        report["allOwnedMcpProcessesExited"] = all(server.proc.poll() is not None for server in servers)
        report["totalDurationMs"] = round((time.monotonic() - began) * 1000)
        report["completedAtUtc"] = datetime.now(timezone.utc).isoformat()
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"Lifecycle report: {REPORT}", flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
