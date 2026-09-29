"""Bounded bridge regressions using disposable hidden Python children, never apps."""
from __future__ import annotations

import concurrent.futures
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("bridge_under_test", HERE.parents[1] / "scripts" / "mcp_server.py")
bridge = importlib.util.module_from_spec(spec)
sys.dont_write_bytecode = True
spec.loader.exec_module(bridge)
children = []
checks = []


def child(prefix):
    proc = subprocess.Popen(
        [sys.executable, "-c", f"import threading; print({prefix!r}, flush=True); threading.Event().wait(15)"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding="utf-8", bufsize=1, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    children.append(proc)
    return proc


def cleanup():
    for proc in children:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def reject_queued_attach():
    native = bridge.Native()
    received_generation = native.generation
    release = threading.Event()
    starts = []
    def forbidden_start():
        starts.append(True)
        raise AssertionError("A pre-Stop attach restarted the controller")
    native._start = forbidden_start
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        blocker = pool.submit(release.wait, 3)
        queued = pool.submit(native.call, "attach_window", {"hwnd": 1}, received_generation)
        assert not queued.running(), "Test did not queue the old request"
        native.stop()
        release.set()
        blocker.result(timeout=1)
        try:
            queued.result(timeout=1)
            raise AssertionError("Pre-Stop attach was accepted")
        except RuntimeError as exc:
            assert str(exc) == "request_cancelled_by_stop", str(exc)
    assert not starts
    checks.append({"stage": "queued_pre_stop_attach_rejected", "passed": True, "controllerStartCount": len(starts)})


class ObservedPipe:
    def __init__(self, inner):
        self.inner = inner
        self.entered = threading.Event()
        self.returned = threading.Event()
    def write(self, value):
        self.entered.set()
        try:
            return self.inner.write(value)
        finally:
            self.returned.set()
    def flush(self):
        return self.inner.flush()
    def close(self):
        return self.inner.close()


def stop_blocked_pipe():
    proc = child("ready")
    assert proc.stdout.readline().strip() == "ready"
    native = bridge.Native()
    native.process = proc
    native.stopped = False
    native._start = lambda: proc
    pipe = ObservedPipe(proc.stdin)
    proc.stdin = pipe
    outcomes = []
    done = threading.Event()
    def request():
        try:
            native.call("act", {"action": "set_value", "value": "x" * 50_000}, native.generation)
            outcomes.append("unexpected_success")
        except Exception as exc:
            outcomes.append(str(exc))
        finally:
            done.set()
    worker = threading.Thread(target=request, daemon=True)
    worker.start()
    assert pipe.entered.wait(2), "Writer never started"
    assert not pipe.returned.wait(0.1), "Own child did not block stdin as intended"
    stop_done = threading.Event()
    stop_outcomes = []
    def stop():
        try:
            stop_outcomes.append(native.stop())
        except Exception as exc:
            stop_outcomes.append({"error": repr(exc)})
        finally:
            stop_done.set()
    started = time.monotonic()
    threading.Thread(target=stop, daemon=True).start()
    assert stop_done.wait(3), "Stop blocked behind stdin writer"
    elapsed_ms = round((time.monotonic() - started) * 1000)
    assert done.wait(1), "Cancelled request did not return"
    assert pipe.returned.wait(1), "Terminated child left writer blocked"
    assert proc.poll() is not None, "Stop did not terminate its own child"
    assert stop_outcomes[0].get("controllerExited") is True, stop_outcomes
    assert outcomes and "stopped" in outcomes[0], outcomes
    assert not native.pending, native.pending
    checks.append({"stage": "stop_while_stdin_blocked", "passed": True, "elapsedMs": elapsed_ms,
                   "processId": proc.pid, "processExited": True, "stopResult": stop_outcomes[0], "requestOutcome": outcomes[0]})


def broken_protocol_cleanup():
    proc = child("{")
    native = bridge.Native()
    native.process = proc
    native.stopped = False
    old_generation = native.generation
    reader = threading.Thread(target=native._read, args=(proc,), daemon=True)
    reader.start()
    reader.join(timeout=3)
    assert not reader.is_alive(), "Protocol cleanup did not finish"
    assert proc.poll() is not None, "Malformed protocol left controller alive"
    assert native.process is None and native.stopped
    assert native.generation > old_generation
    checks.append({"stage": "malformed_protocol_terminates_owned_child", "passed": True,
                   "processId": proc.pid, "processExited": True, "oldGeneration": old_generation,
                   "newGeneration": native.generation})


started = time.monotonic()
failure = None
watchdog = threading.Timer(10, cleanup)
watchdog.daemon = True
watchdog.start()
try:
    reject_queued_attach()
    stop_blocked_pipe()
    broken_protocol_cleanup()
except Exception as exc:
    failure = repr(exc)
finally:
    cleanup()
    watchdog.cancel()
    result = {"status": "failed" if failure else "passed", "failure": failure,
              "elapsedMs": round((time.monotonic() - started) * 1000), "checks": checks,
              "children": [{"processId": p.pid, "exited": p.poll() is not None} for p in children],
              "scope": "Imports actual MCP Native implementation; queued request receives acceptance-generation explicitly. Only disposable hidden Python children; no controller UI, target apps, or global input."}
    (HERE / "cancel-results.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
sys.exit(1 if failure else 0)
