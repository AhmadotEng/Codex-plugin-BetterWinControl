"""Tests the linked production NativeEditActions against this directory's own native controls."""
import ctypes as C
from ctypes import wintypes as W
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import probe

HERE = Path(__file__).resolve().parent
children = []; samples = []; done = threading.Event(); failure = None; checks = []
def sample():
    while not done.is_set(): samples.append(probe.metadata()); done.wait(.01)
thread = threading.Thread(target=sample, daemon=True); thread.start()
started = time.monotonic()
try:
    fixture = subprocess.Popen([sys.executable, "-B", str(HERE / "probe.py"), "--fixture", "--adapter-fixture"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    children.append(fixture)
    lines = queue.Queue(); threading.Thread(target=lambda: lines.put(fixture.stdout.readline()), daemon=True).start()
    ready = json.loads(lines.get(timeout=3)); ready["pid"] = fixture.pid
    def msg(hwnd, code, wp=0, lp=0):
        pid = W.DWORD(); probe.U.GetWindowThreadProcessId(hwnd, C.byref(pid))
        assert pid.value == fixture.pid and hwnd in ready["controls"].values()
        result = probe.UP()
        C.set_last_error(0)
        assert probe.send_timeout(hwnd, code, wp, lp, 0x23, 200, C.byref(result)), (hex(code), C.get_last_error())
        return result.value
    for name, hwnd in ready["controls"].items():
        if name in {"edit_single", "edit_multi", "richedit_multi"}:
            buf = C.create_unicode_buffer("alpha beta"); msg(hwnd, 0xC, 0, C.addressof(buf))
            msg(hwnd, 0xB1, 6, 10)
    client = subprocess.Popen(["dotnet", str(HERE / "bin/Release/net10.0-windows/NativeEditTests.dll")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW)
    children.append(client)
    out, err = client.communicate(json.dumps(ready)+"\n", timeout=5)
    assert client.returncode == 0, err
    checks.append(json.loads(out))
    for name, hwnd in ready["controls"].items():
        if name == "password":
            checks.append({"control": name, "externalTextRead": "skipped; production Supports/Insert both rejected"})
            continue
        buf = C.create_unicode_buffer(100); msg(hwnd, 0xD, len(buf), C.addressof(buf))
        expected = "alpha 世界🙂" if name in {"edit_single", "edit_multi", "richedit_multi"} else ""
        assert buf.value == expected, (name, buf.value)
        checks.append({"control": name, "observedText": buf.value, "expectedText": expected, "matched": True})
except Exception as exc:
    failure = repr(exc)
finally:
    for p in children:
        if p.poll() is None: p.kill()
        p.wait(timeout=2)
    done.set(); thread.join(timeout=1)
    distinct = lambda key: len({json.dumps(s[key]) for s in samples})
    result = {"status": "failed" if failure else "passed", "failure": failure,
        "elapsedMs": round((time.monotonic()-started)*1000), "checks": checks,
        "metadata": {"sampleCount": len(samples), "distinctForegroundHandles": distinct("foreground"),
            "distinctFocusHandles": distinct("focus"), "distinctCursorPositions": distinct("cursor"), "samples": samples},
        "children": [{"pid": p.pid, "exited": p.poll() is not None} for p in children]}
    (HERE / "adapter-results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({k:v for k,v in result.items() if k != "metadata"}))
sys.exit(1 if failure else 0)
