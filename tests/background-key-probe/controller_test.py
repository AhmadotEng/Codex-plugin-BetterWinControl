"""Bounded full controller RPC insertion test; only this harness's native fixture is targeted."""
import argparse
import ctypes as C
from ctypes import wintypes as W
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import probe

HERE = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(); parser.add_argument("--controller", required=True)
args = parser.parse_args()
executable = Path(args.controller).resolve()
assert executable.is_file()
children = []; checks = []; samples = []; failure = None
sample_done = threading.Event()
def cleanup():
    for child in list(children):
        if child.poll() is None: child.kill()
        try: child.wait(timeout=1)
        except subprocess.TimeoutExpired: pass
def sample():
    while not sample_done.is_set():
        value = probe.metadata()
        for key in ("foreground", "focus"):
            pid = W.DWORD(); probe.U.GetWindowThreadProcessId(value[key], C.byref(pid)); value[key + "Pid"] = pid.value
        samples.append(value); sample_done.wait(.01)
sampler = threading.Thread(target=sample, daemon=True); sampler.start()
watchdog = threading.Timer(16, cleanup); watchdog.daemon = True; watchdog.start()
started = time.monotonic()
try:
    fixture = subprocess.Popen([sys.executable, "-B", str(HERE / "probe.py"), "--fixture", "--adapter-fixture"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW)
    children.append(fixture)
    ready_queue = queue.Queue(); threading.Thread(target=lambda: ready_queue.put(fixture.stdout.readline()), daemon=True).start()
    ready = json.loads(ready_queue.get(timeout=3))
    def own_msg(hwnd, code, wp=0, lp=0):
        pid = W.DWORD(); probe.U.GetWindowThreadProcessId(hwnd, C.byref(pid))
        assert pid.value == fixture.pid and hwnd in ready["controls"].values()
        result = probe.UP(); C.set_last_error(0)
        assert probe.send_timeout(hwnd, code, wp, lp, 0x23, 200, C.byref(result)), (hex(code), C.get_last_error())
        return result.value
    def own_text(hwnd):
        buf = C.create_unicode_buffer(100); own_msg(hwnd, 0xD, len(buf), C.addressof(buf)); return buf.value
    for name in ("edit_single", "edit_multi", "richedit_multi"):
        hwnd = ready["controls"][name]
        buf = C.create_unicode_buffer("alpha beta"); own_msg(hwnd, 0xC, 0, C.addressof(buf)); own_msg(hwnd, 0xB1, 6, 10)
    controller = subprocess.Popen([str(executable), "--rpc", "--parent-pid", str(os.getpid())],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW)
    children.append(controller)
    responses = queue.Queue()
    def read():
        for line in controller.stdout:
            try: responses.put(json.loads(line))
            except Exception: responses.put({"parseFailure": line})
    threading.Thread(target=read, daemon=True).start()
    ids = [0]
    def call(method, params=None, error=None):
        ids[0] += 1
        controller.stdin.write(json.dumps({"id": ids[0], "method": method, "params": params or {}})+"\n")
        controller.stdin.flush()
        response = responses.get(timeout=4)
        assert response.get("id") == ids[0], response
        if error:
            assert error in response.get("error", {}).get("message", ""), response
            return response["error"]
        assert "error" not in response, response
        return response["result"]
    def observe(): return call("observe", {"max_elements": 80, "include_screenshot": False})["observation"]
    def node(observation, automation_id):
        matches = [n for n in observation["elements"] if n.get("automationId") == automation_id]
        assert len(matches) == 1, (automation_id, observation)
        return matches[0]
    def action(observation, element_id, value, error=None):
        return call("act", {"observation_id": observation["observationId"], "element_id": element_id,
                            "action": "insert_text", "value": value}, error)
    call("attach_window", {"hwnd": ready["host"]})
    old = observe(); current = observe()
    supported = [node(current, str(i)) for i in (1, 2, 3)]
    assert all("insert_text" in n["patterns"] for n in supported), supported
    for n in current["elements"]:
        if n.get("automationId") in ("4", "5", "6", "7") or n.get("isPassword"):
            assert "insert_text" not in n["patterns"], n
    checks.append({"stage": "capability_advertisement", "supportedControls": 3, "protectedCapabilitiesAbsent": True})
    stale = action(old, node(old, "1")["id"], "must not insert", "stale_observation")
    checks.append({"stage": "old_observation_rejected", "error": stale["message"]})
    unknown = action(current, "e999999", "must not insert", "unknown_element")
    checks.append({"stage": "out_of_scope_element_id_rejected", "error": unknown["message"]})
    assert own_text(ready["controls"]["edit_single"]) == "alpha beta"
    for number, name in enumerate(("edit_single", "edit_multi", "richedit_multi"), 1):
        observation = observe()
        result = action(observation, node(observation, str(number))["id"], "世界🙂")
        actual = own_text(ready["controls"][name]); assert actual == "alpha 世界🙂", (name, actual, result)
        assert result.get("verified") is False, result
        checks.append({"stage": "controller_insert_text", "control": name, "observedText": actual, "actionResult": result})
        action(observation, node(observation, str(number))["id"], "must not repeat", "stale_observation")
    call("stop")
    controller.stdin.close(); controller.wait(timeout=2)
except Exception as exc:
    failure = repr(exc)
finally:
    cleanup(); watchdog.cancel(); sample_done.set(); sampler.join(timeout=1)
    own_pids = {c.pid for c in children}
    def owner(hwnd):
        pid = W.DWORD(); probe.U.GetWindowThreadProcessId(hwnd, C.byref(pid)); return pid.value
    # Also compare exact known fixture handles, since exited-process ownership is unavailable now.
    fixture_handles = {ready["host"], *ready["controls"].values()} if "ready" in locals() else set()
    own_focus = any(s["foreground"] in fixture_handles or s["focus"] in fixture_handles or
                    s["foregroundPid"] in own_pids or s["focusPid"] in own_pids for s in samples)
    if own_focus and failure is None: failure = "Own fixture received sampled foreground/focus"
    distinct = lambda key: len({json.dumps(s[key]) for s in samples})
    result = {"status": "failed" if failure else "passed", "failure": failure,
        "elapsedMs": round((time.monotonic()-started)*1000), "checks": checks,
        "controller": str(executable), "controllerSha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "metadata": {"sampleCount": len(samples), "distinctForegroundHandles": distinct("foreground"),
            "distinctFocusHandles": distinct("focus"), "distinctCursorPositions": distinct("cursor"),
            "fixtureFocusObserved": own_focus, "samples": samples},
        "children": [{"pid": c.pid, "exited": c.poll() is not None} for c in children],
        "limitations": "Own fixtures only; direct controller RPC, not MCP enumeration or existing user apps. No Zen keyboard claim."}
    (HERE / "controller-results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({k:v for k,v in result.items() if k != "metadata"}))
sys.exit(1 if failure else 0)
