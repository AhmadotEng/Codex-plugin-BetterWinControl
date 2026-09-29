"""Two-controller lease lifecycle test; only creates and observes owned WPF fixtures.

Runs the controller from bin, not the published runtime. No user applications,
physical input, clipboard operations, or persistent lease files are involved.
"""
from __future__ import annotations

import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True
from integration_test import JsonProcess, PLUGIN, TESTS

CONTROLLER = PLUGIN / "controller" / "bin" / "Release" / "net10.0-windows10.0.19041.0" / "BackgroundControl.exe"
FIXTURE = TESTS / "lease_fixture" / "bin" / "Release" / "net10.0-windows" / "LeaseFixture.exe"
REPORT = TESTS / "results" / "leases.json"
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
kernel.OpenProcess.restype = ctypes.c_void_p
kernel.CloseHandle.argtypes = [ctypes.c_void_p]
kernel.CloseHandle.restype = ctypes.c_bool
kernel.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_ulonglong)] * 4
kernel.GetProcessTimes.restype = ctypes.c_bool
kernel.OpenEventW.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_wchar_p]
kernel.OpenEventW.restype = ctypes.c_void_p


def creation_ticks(pid: int) -> int:
    handle = kernel.OpenProcess(0x1000, False, pid)
    assert handle, "Own fixture process identity unavailable"
    try:
        created, exited, kernel_time, user_time = (ctypes.c_ulonglong() for _ in range(4))
        assert kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel_time), ctypes.byref(user_time))
        return created.value + 504911232000000000
    finally:
        kernel.CloseHandle(handle)


def lease_exists(name: str) -> bool:
    handle = kernel.OpenEventW(0x100000, False, name)
    if handle:
        kernel.CloseHandle(handle)
        return True
    error = ctypes.get_last_error()
    assert error == 2, f"Unexpected OpenEvent error {error}"
    return False


def wait_released(name: str, timeout: float = 3):
    deadline = time.monotonic() + timeout
    while lease_exists(name) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not lease_exists(name), "Lease was not released within the deadline"


def call(controller: JsonProcess, method: str, params: dict | None = None, error: str | None = None):
    reply = controller.rpc(method, params)
    if error:
        assert error in reply.get("error", {}).get("message", ""), reply
        return reply["error"]
    assert "result" in reply, reply
    return reply["result"]


def main():
    report = {"scope": "Two independent controller processes, one owned WPF fixture with independent windows and an owned dialog",
              "startedAtUtc": datetime.now(timezone.utc).isoformat(), "passed": False, "checks": []}
    fixtures: list[JsonProcess] = []
    controllers: list[JsonProcess] = []

    def check(name: str):
        report["checks"].append({"name": name, "passed": True})
        print("PASS " + name, flush=True)

    def start_controller():
        item = JsonProcess([str(CONTROLLER), "--rpc", "--parent-pid", str(os.getpid())])
        controllers.append(item)
        return item

    try:
        assert CONTROLLER.is_file() and FIXTURE.is_file(), "Build controller and fixture before running this test"
        report["controllerSha256"] = hashlib.sha256(CONTROLLER.with_suffix(".dll").read_bytes()).hexdigest()
        fixture = JsonProcess([str(FIXTURE)])
        fixtures.append(fixture)
        ready = fixture.receive()
        assert ready.get("event") == "ready" and ready["fixturePID"] == fixture.proc.pid
        pid, ticks = fixture.proc.pid, creation_ticks(fixture.proc.pid)
        first, second, dialog = (int(ready[name]) for name in ("primary", "independent", "ownedDialog"))
        first_lease = rf"Local\Codex.WindowsBackgroundControl.v1.{pid}.{ticks:x}.{first:x}"
        second_lease = rf"Local\Codex.WindowsBackgroundControl.v1.{pid}.{ticks:x}.{second:x}"
        a, b = start_controller(), start_controller()
        call(a, "attach_window", {"hwnd": first})
        assert lease_exists(first_lease), "Lease token must exist while attached"
        call(a, "attach_window", {"hwnd": first})
        check("same controller reattaches without dropping ownership")
        call(b, "attach_window", {"hwnd": first}, error="target_busy")
        call(b, "attach_window", {"hwnd": dialog}, error="target_busy")
        assert call(b, "state")["controller"]["attached"] is False
        check("second process cannot attach leased parent or its owned dialog")
        call(a, "attach_window", {"hwnd": dialog})
        call(b, "attach_window", {"hwnd": first}, error="target_busy")
        call(a, "attach_window", {"hwnd": first})
        check("same controller can reattach within owner family without opening contention gap")
        call(b, "attach_window", {"hwnd": second})
        assert lease_exists(first_lease) and lease_exists(second_lease)
        check("independent unowned windows in same process can be attached concurrently")
        call(b, "attach_window", {"hwnd": first}, error="target_busy")
        assert call(b, "state")["controller"]["window"]["hwnd"] == second
        call(a, "attach_window", {"hwnd": second}, error="target_busy")
        call(a, "attach_window", {"hwnd": -1}, error="invalid_target")
        assert call(a, "state")["controller"]["window"]["hwnd"] == first
        assert lease_exists(first_lease) and lease_exists(second_lease)
        observed = call(a, "observe", {"max_elements": 30, "include_screenshot": False})
        assert any(n.get("automationId") == "fixture-input" for n in observed["observation"]["elements"])
        check("failed busy or invalid reattach preserves previous target and lease")
        call(a, "stop")
        wait_released(first_lease)
        call(b, "attach_window", {"hwnd": first})
        wait_released(second_lease)
        call(a, "attach_window", {"hwnd": second})
        check("stop releases ownership and successful replacement releases prior lease")
        a.close()
        wait_released(second_lease)
        a = start_controller()
        call(a, "attach_window", {"hwnd": second})
        check("graceful controller disposal releases ownership")
        a.proc.kill()
        a.proc.wait(timeout=5)
        wait_released(second_lease)
        a = start_controller()
        call(a, "attach_window", {"hwnd": second})
        check("abrupt controller process exit releases kernel ownership")
        assert lease_exists(second_lease)
        fixture.proc.stdin.write("close-independent\n")
        fixture.proc.stdin.flush()
        wait_released(second_lease)
        assert fixture.proc.poll() is None, "Target closure test must keep fixture process alive"
        # The preview host may itself exit after the selected target closes; either way the
        # kernel lease must already be gone, before test cleanup or fixture process exit.
        check("target window closure releases lease while fixture process remains alive")
        report["passed"] = True
        return 0
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        return 1
    finally:
        for controller in controllers:
            controller.close()
        for fixture in fixtures:
            fixture.close()
        report["allOwnedProcessesExited"] = all(p.proc.poll() is not None for p in controllers + fixtures)
        report["completedAtUtc"] = datetime.now(timezone.utc).isoformat()
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
