"""Bounded co-use check: operate only our WPF fixture, sample input metadata.

No global input injection, keystroke hooks, user-app text, window titles, or user-
app screenshots. Last-input changes are an activity signal, not proof of physical
input. This measures a concrete subset and does not establish full feature parity.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time

from integration_test import JsonProcess, PLUGIN, TESTS, action_args, element


DURATION_SECONDS = 20
REPORT = TESTS / "results" / "co-use.json"


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                ("rcCaret", wintypes.RECT)]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


def configure_api():
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user.GetForegroundWindow.restype = wintypes.HWND
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId.restype = wintypes.DWORD
    user.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
    user.GetGUIThreadInfo.restype = wintypes.BOOL
    user.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
    user.GetLastInputInfo.restype = wintypes.BOOL
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.IsWindowVisible.restype = wintypes.BOOL
    kernel.GetTickCount.restype = wintypes.DWORD
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    return user, kernel


def controller_children(kernel, server_pid: int) -> set[int]:
    # Only parent/PID numbers are retained. Process names are never recorded.
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        return set()
    pids = set()
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        success = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while success:
            if entry.th32ParentProcessID == server_pid:
                pids.add(int(entry.th32ProcessID))
            success = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return pids


def owned_visible_windows(user, pids: set[int]) -> set[int]:
    windows = set()
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def inspect(hwnd, _):
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user.IsWindowVisible(hwnd):
            windows.add(int(hwnd))
        return True

    user.EnumWindows(inspect, 0)
    return windows


def metadata_sample(user, kernel, started: float) -> dict:
    hwnd = user.GetForegroundWindow()
    pid = wintypes.DWORD()
    thread_id = user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) if hwnd else 0
    gui = GUITHREADINFO()
    gui.cbSize = ctypes.sizeof(gui)
    gui_ok = bool(user.GetGUIThreadInfo(thread_id, ctypes.byref(gui))) if thread_id else False
    last = LASTINPUTINFO()
    last.cbSize = ctypes.sizeof(last)
    input_ok = bool(user.GetLastInputInfo(ctypes.byref(last)))
    now_tick = int(kernel.GetTickCount())
    return {"elapsedMs": round((time.monotonic() - started) * 1000),
            "foregroundHwnd": int(hwnd or 0), "foregroundPID": int(pid.value),
            "foregroundThreadID": int(thread_id), "guiQuerySucceeded": gui_ok,
            "activeHwnd": int(gui.hwndActive or 0), "focusHwnd": int(gui.hwndFocus or 0),
            "lastInputQuerySucceeded": input_ok,
            "lastInputTick": int(last.dwTime) if input_ok else None,
            "inputAgeMs": (now_tick - int(last.dwTime)) & 0xFFFFFFFF if input_ok else None}


class BoundedProcess(JsonProcess):
    def __init__(self, command, deadline):
        self.deadline = deadline
        super().__init__(command)

    def rpc(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        request_id = self.next_id
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        request_end = min(self.deadline, time.monotonic() + 2.0)
        while time.monotonic() < request_end:
            message = self.receive(max(0.01, request_end - time.monotonic()))
            if message.get("id") == request_id:
                return message
            if "id" in message and "method" in message:
                self.send({"jsonrpc": "2.0", "id": message["id"],
                           "error": {"code": -32601, "message": "Test client does not accept server requests"}})
        raise TimeoutError("bounded_rpc_timeout")


def failure_category(exc: Exception) -> str:
    # Never persist raw exceptions that might include observed UI content.
    message = str(exc).lower()
    for marker in ("paused", "stopped", "controller_exited", "stale_observation", "timeout"):
        if marker in message:
            return marker
    return type(exc).__name__


def main() -> int:
    started = time.monotonic()
    absolute_deadline = started + 27
    report = {"scope": "20-second owned-fixture co-use subset; no full parity claim",
              "startedAtUtc": datetime.now(timezone.utc).isoformat(), "requestedDurationSeconds": 20,
              "privacy": "Only numeric focus/process/window and last-input metadata; no keystrokes or user-app content",
              "limitations": ["Last-input activity is inferred, not guaranteed physical or attributable to the user.",
                             "A user may deliberately select the fixture or preview; this check cannot attribute that intent.",
                             "50ms sampling may miss shorter foreground transitions.",
                             "Provider/capture behavior for other applications is not established."],
              "samples": [], "cyclesVerified": 0, "previewHwnds": [], "status": "not_started"}
    server = fixture = None
    finished = threading.Event()
    sample_stop = threading.Event()
    sample_thread = None
    fixture_hwnd = 0
    cycle_results = []
    preview_handles = set()
    try:
        assert os.name == "nt"
        user, kernel = configure_api()
        fixture_exe = TESTS / "fixture" / "bin" / "Release" / "net10.0-windows" / "WindowsBackgroundControlFixture.exe"
        server_path = PLUGIN / "scripts" / "mcp_server.py"
        assert fixture_exe.is_file(), "fixture_not_built"
        report["testedArtifacts"] = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                                     for path in (server_path, PLUGIN / "runtime" / "BackgroundControl.dll")}

        def sample_loop():
            while not sample_stop.is_set():
                report["samples"].append(metadata_sample(user, kernel, started))
                sample_stop.wait(0.05)

        sample_thread = threading.Thread(target=sample_loop, daemon=True)
        sample_thread.start()
        fixture = BoundedProcess([str(fixture_exe)], absolute_deadline)
        ready = fixture.receive(timeout=3)
        assert ready.get("event") == "ready" and ready.get("fixturePID") == fixture.proc.pid
        fixture_hwnd = int(ready["hwnd"])
        owner = wintypes.DWORD()
        user.GetWindowThreadProcessId(fixture_hwnd, ctypes.byref(owner))
        assert owner.value == fixture.proc.pid, "fixture_ownership_mismatch"
        report["fixturePID"], report["fixtureHwnd"] = fixture.proc.pid, fixture_hwnd
        server = BoundedProcess([sys.executable, "-u", str(server_path)], absolute_deadline)

        def watchdog():
            if not finished.wait(max(0, absolute_deadline - time.monotonic())):
                report["watchdogTriggered"] = True
                # Popen handles identify only our two child processes; the native
                # controller also has its own parent-exit shutdown safeguard.
                for owned in (server, fixture):
                    if owned and owned.proc.poll() is None:
                        owned.proc.terminate()

        threading.Thread(target=watchdog, daemon=True).start()
        server.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                   "clientInfo": {"name": "owned-co-use-check", "version": "1.0"}})
        server.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        server.tool("attach_window", {"hwnd": fixture_hwnd})
        native_pids = controller_children(kernel, server.proc.pid)
        preview_handles = owned_visible_windows(user, native_pids)
        report["controllerPIDs"] = sorted(native_pids)
        report["previewHwnds"] = sorted(preview_handles)
        assert preview_handles, "native_preview_window_not_visible"
        active_start = time.monotonic()
        end = min(active_start + DURATION_SECONDS, absolute_deadline - 3)
        report["activeStartedElapsedMs"] = round((active_start - started) * 1000)
        report["status"] = "running"
        print("Co-use check running for 20 seconds; only the owned test fixture is controlled.", flush=True)
        while time.monotonic() < end:
            cycle = len(cycle_results) + 1
            observed, _ = server.tool("observe", {"max_elements": 100, "include_screenshot": False})
            if observed.get("observation", {}).get("paused"):
                report["status"] = "user_paused_or_stopped"
                break
            value = f"Co-use check cycle {cycle}"
            server.tool("act", action_args(observed, "fixture-input", "set_value", value=value))
            observed, _ = server.tool("observe", {"max_elements": 100, "include_screenshot": False})
            assert element(observed, "fixture-input").get("value") == value, "fixture_value_mismatch"
            server.tool("act", action_args(observed, "fixture-apply", "invoke"))
            applied = fixture.receive(timeout=min(1, max(0.01, end - time.monotonic())))
            assert applied.get("event") == "applied" and applied.get("count") == cycle and applied.get("value") == value, "fixture_counter_mismatch"
            observed, _ = server.tool("observe", {"max_elements": 100, "include_screenshot": False})
            assert element(observed, "fixture-counter").get("name") == f"Counter: {cycle}", "fixture_readback_mismatch"
            capture = observed.get("capture", {})
            cycle_results.append({"counter": cycle, "elapsedMs": round((time.monotonic() - started) * 1000),
                                  "captureActive": bool(capture.get("active")), "frameVersion": capture.get("frameVersion")})
            if capture.get("paused"):
                report["status"] = "user_paused_or_stopped"
                break
            time.sleep(min(0.35, max(0, end - time.monotonic())))
        if report["status"] == "running":
            report["status"] = "completed"
        report["activeDurationMs"] = round((time.monotonic() - active_start) * 1000)
        report["activeEndedElapsedMs"] = round((time.monotonic() - started) * 1000)
    except Exception as exc:
        category = failure_category(exc)
        report["errorCategory"] = category
        report["status"] = "user_paused_or_stopped" if category in {"paused", "stopped", "controller_exited"} else "failed"
        report["activeEndedElapsedMs"] = round((time.monotonic() - started) * 1000)
        print(f"Co-use check ended: {category}", flush=True)
    finally:
        if server and server.proc.poll() is None:
            try:
                server.deadline = min(absolute_deadline, time.monotonic() + 2)
                result, _ = server.tool("stop")
                report["controllerExited"] = result.get("controllerExited") is True
                report["targetRevoked"] = result.get("targetRevoked") is True
            except Exception:
                report["cleanupStopConfirmed"] = False
        for owned in (server, fixture):
            if not owned:
                continue
            if owned.proc.poll() is None and owned.proc.stdin:
                try:
                    if owned is fixture:
                        owned.proc.stdin.write("stop\n")
                        owned.proc.stdin.flush()
                    owned.proc.stdin.close()
                    owned.proc.wait(timeout=0.8)
                except Exception:
                    if owned.proc.poll() is None:
                        owned.proc.terminate()
            if owned.proc.poll() is None:
                try:
                    owned.proc.wait(timeout=0.8)
                except Exception:
                    owned.proc.kill()
        finished.set()
        sample_stop.set()
        if sample_thread:
            sample_thread.join(timeout=0.2)
        samples = report["samples"]
        owned_hwnds = preview_handles | ({fixture_hwnd} if fixture_hwnd else set())
        transitions = []
        input_changes = external_activity = 0
        for before, after in zip(samples, samples[1:]):
            changed_input = before["lastInputTick"] != after["lastInputTick"] and after["lastInputQuerySucceeded"]
            input_changes += int(changed_input)
            during_active = (report.get("activeStartedElapsedMs", 1 << 60) <= after["elapsedMs"]
                             <= report.get("activeEndedElapsedMs", -1))
            external_activity += int(changed_input and during_active and after["foregroundHwnd"] not in owned_hwnds)
            if before["foregroundHwnd"] != after["foregroundHwnd"] and after["foregroundHwnd"] in owned_hwnds:
                transitions.append({"elapsedMs": after["elapsedMs"], "foregroundHwnd": after["foregroundHwnd"],
                                    "inputTickChangedSincePreviousSample": changed_input,
                                    "inputAgeMs": after["inputAgeMs"],
                                    "noRecentInputSignal": not changed_input and (after["inputAgeMs"] or 0) > 250})
        report["ownedForegroundTransitions"] = transitions
        report["unexplainedOwnedForegroundTransitions"] = sum(item["noRecentInputSignal"] for item in transitions)
        report["lastInputTickChanges"] = input_changes
        report["externalForegroundInputActivitySignals"] = external_activity
        report["externalInputObserved"] = external_activity > 0
        report["cyclesVerified"] = len(cycle_results)
        report["cycles"] = cycle_results
        report["fixtureExited"] = bool(fixture and fixture.proc.poll() is not None)
        report["serverExited"] = bool(server and server.proc.poll() is not None)
        report["totalDurationMs"] = round((time.monotonic() - started) * 1000)
        report["completedAtUtc"] = datetime.now(timezone.utc).isoformat()
        report["boundedSubsetPassed"] = (report["status"] == "completed" and len(cycle_results) > 0
                                         and not report["unexplainedOwnedForegroundTransitions"]
                                         and report["fixtureExited"] and report["serverExited"]
                                         and report.get("controllerExited") is True)
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"Co-use report: {REPORT}", flush=True)
    return 0 if report["boundedSubsetPassed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
