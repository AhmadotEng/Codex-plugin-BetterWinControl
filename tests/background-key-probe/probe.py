"""Cross-process native-control experiment. Every message target belongs to our fixture."""
from __future__ import annotations
import ctypes as C
from ctypes import wintypes as W
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
U = C.WinDLL("user32", use_last_error=True)
K = C.WinDLL("kernel32", use_last_error=True)
UP = C.c_size_t
LP = C.c_ssize_t
WNDPROC = C.WINFUNCTYPE(LP, W.HWND, W.UINT, UP, LP)

class WNDCLASS(C.Structure):
    _fields_ = [("style", W.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", C.c_int),
                ("cbWndExtra", C.c_int), ("hInstance", W.HINSTANCE), ("hIcon", W.HANDLE),
                ("hCursor", W.HANDLE), ("hbrBackground", W.HANDLE),
                ("lpszMenuName", W.LPCWSTR), ("lpszClassName", W.LPCWSTR)]

class GUIINFO(C.Structure):
    _fields_ = [("cbSize", W.DWORD), ("flags", W.DWORD), ("hwndActive", W.HWND),
                ("hwndFocus", W.HWND), ("hwndCapture", W.HWND), ("hwndMenuOwner", W.HWND),
                ("hwndMoveSize", W.HWND), ("hwndCaret", W.HWND), ("rcCaret", W.RECT)]

def api(dll, name, restype, *args):
    fn = getattr(dll, name); fn.restype = restype; fn.argtypes = args; return fn

create = api(U, "CreateWindowExW", W.HWND, W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD,
             C.c_int, C.c_int, C.c_int, C.c_int, W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID)
defproc = api(U, "DefWindowProcW", LP, W.HWND, W.UINT, UP, LP)
api(U, "RegisterClassW", W.ATOM, C.POINTER(WNDCLASS))
api(U, "ShowWindow", W.BOOL, W.HWND, C.c_int)
api(U, "DestroyWindow", W.BOOL, W.HWND)
api(U, "PostQuitMessage", None, C.c_int)
api(U, "GetMessageW", W.BOOL, C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT)
api(U, "TranslateMessage", W.BOOL, C.POINTER(W.MSG))
api(U, "DispatchMessageW", LP, C.POINTER(W.MSG))
api(U, "SetTimer", UP, W.HWND, UP, W.UINT, W.LPVOID)
api(U, "GetForegroundWindow", W.HWND)
api(U, "GetGUIThreadInfo", W.BOOL, W.DWORD, C.POINTER(GUIINFO))
api(U, "GetCursorPos", W.BOOL, C.POINTER(W.POINT))
api(U, "GetWindowThreadProcessId", W.DWORD, W.HWND, C.POINTER(W.DWORD))
api(U, "IsWindow", W.BOOL, W.HWND)
api(U, "IsChild", W.BOOL, W.HWND, W.HWND)
api(U, "EnableWindow", W.BOOL, W.HWND, W.BOOL)
api(U, "GetClassNameW", C.c_int, W.HWND, W.LPWSTR, C.c_int)
api(U, "GetWindowLongPtrW", LP, W.HWND, C.c_int)
api(U, "GetKeyState", C.c_short, C.c_int)
api(U, "GetAsyncKeyState", C.c_short, C.c_int)
api(U, "MapVirtualKeyW", W.UINT, W.UINT, W.UINT)
send_timeout = api(U, "SendMessageTimeoutW", LP, W.HWND, W.UINT, UP, LP, W.UINT, W.UINT, C.POINTER(UP))
api(K, "GetModuleHandleW", W.HMODULE, W.LPCWSTR)
api(K, "LoadLibraryW", W.HMODULE, W.LPCWSTR)

MODIFIERS = (0x10, 0x11, 0x12, 0x5B, 0x5C)

def fixture():
    notifications = [0]
    @WNDPROC
    def wndproc(hwnd, msg, wp, lp):
        if msg == 0x8001:
            return sum((1 << i) for i, vk in enumerate(MODIFIERS) if U.GetKeyState(vk) < 0)
        if msg == 0x8002:
            time.sleep(0.4)  # Deliberately hung own fixture, for the timeout check.
            return 1
        if msg == 0x8003:
            return notifications[0]
        if msg == 0x111 and ((wp >> 16) & 0xFFFF) == 0x300:
            notifications[0] += 1
        if msg == 0x113:
            U.DestroyWindow(hwnd)
            return 0
        if msg == 2:
            U.PostQuitMessage(0)
            return 0
        return defproc(hwnd, msg, wp, lp)
    instance = K.GetModuleHandleW(None)
    wc = WNDCLASS(lpfnWndProc=wndproc, hInstance=instance, lpszClassName="OwnBackgroundKeyProbe")
    assert U.RegisterClassW(C.byref(wc)), C.get_last_error()
    assert K.LoadLibraryW("Msftedit.dll"), C.get_last_error()
    # TOOLWINDOW, ordinary activation style; positioned entirely offscreen.
    host = create(0x80, wc.lpszClassName, "Own disposable fixture", 0x00CF0000,
                  -20000, -20000, 400, 300, None, None, instance, None)
    assert host
    controls = {}
    definitions = [
        ("edit_single", "EDIT", 0x80),
        ("edit_multi", "EDIT", 0x4 | 0x40 | 0x1000),
        ("richedit_multi", "RICHEDIT50W", 0x4 | 0x40 | 0x1000),
    ]
    if "--adapter-fixture" in sys.argv:
        definitions += [("readonly", "EDIT", 0x800), ("password", "EDIT", 0x20),
                        ("disabled", "EDIT", 0), ("protected_rich", "RICHEDIT50W", 0x4)]
    for index, (name, cls, style) in enumerate(definitions):
        hwnd = create(0, cls, "", 0x50000000 | style, 0, index*70, 350, 60, host,
                      index+1, instance, None)
        assert hwnd, (name, C.get_last_error())
        controls[name] = int(hwnd)
        if name == "disabled": U.EnableWindow(hwnd, False)
        if name == "protected_rich":
            result = UP()
            assert send_timeout(hwnd, 0x445, 0, 0x00200000, 0x1 | 0x2 | 0x20, 200, C.byref(result))
    U.SetTimer(host, 1, 12000, None)
    U.ShowWindow(host, 4)  # SW_SHOWNOACTIVATE. No focus-restoring operation exists here.
    print(json.dumps({"host": int(host), "controls": controls}), flush=True)
    msg = W.MSG()
    while U.GetMessageW(C.byref(msg), None, 0, 0) > 0:
        U.TranslateMessage(C.byref(msg)); U.DispatchMessageW(C.byref(msg))

def metadata():
    fg = U.GetForegroundWindow()
    gui = GUIINFO(cbSize=C.sizeof(GUIINFO)); U.GetGUIThreadInfo(0, C.byref(gui))
    pt = W.POINT(); U.GetCursorPos(C.byref(pt))
    return {"foreground": int(fg or 0), "focus": int(gui.hwndFocus or 0), "cursor": [pt.x, pt.y]}

def probe():
    started = time.monotonic()
    proc = None; samples = []; operations = []; failure = None; checks = []
    finish_sampling = threading.Event()
    def sample_loop():
        while not finish_sampling.is_set():
            samples.append(metadata()); finish_sampling.wait(0.01)
    sampler = threading.Thread(target=sample_loop, daemon=True); sampler.start()
    try:
        proc = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "--fixture"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW)
        lines = queue.Queue()
        threading.Thread(target=lambda: lines.put(proc.stdout.readline()), daemon=True).start()
        ready = json.loads(lines.get(timeout=3))
        host, controls = ready["host"], ready["controls"]
        allowed = {host, *controls.values()}
        def validate(hwnd, host_only=False):
            pid = W.DWORD()
            U.GetWindowThreadProcessId(hwnd, C.byref(pid))
            assert hwnd in allowed and pid.value == proc.pid and U.IsWindow(hwnd), "nonfixture target rejected"
            if not host_only:
                assert U.IsChild(host, hwnd)
                buf = C.create_unicode_buffer(100); U.GetClassNameW(hwnd, buf, len(buf))
                assert buf.value.casefold() in {"edit", "richedit50w"}, buf.value
            assert U.GetForegroundWindow() not in allowed, "Fixture received foreground; aborting"
            gui = GUIINFO(cbSize=C.sizeof(GUIINFO)); U.GetGUIThreadInfo(0, C.byref(gui))
            assert gui.hwndFocus not in allowed, "Fixture received focus; aborting"
        def msg(hwnd, code, wp=0, lp=0, *, timeout=200, host_only=False, expect_timeout=False):
            validate(hwnd, host_only)
            value = UP(); C.set_last_error(0)
            before = time.monotonic()
            ok = send_timeout(hwnd, code, wp, lp, 0x1 | 0x2 | 0x20, timeout, C.byref(value))
            elapsed = round((time.monotonic()-before)*1000)
            if expect_timeout:
                return {"ok": bool(ok), "error": C.get_last_error(), "elapsedMs": elapsed}
            assert ok, (hex(code), C.get_last_error(), elapsed)
            return value.value
        def text(hwnd):
            buf = C.create_unicode_buffer(4096)
            msg(hwnd, 0xD, len(buf), C.addressof(buf))
            return buf.value
        def select(hwnd, start, end): msg(hwnd, 0xB1, start, end)
        def replace(hwnd, value):
            buf = C.create_unicode_buffer(value); msg(hwnd, 0xC2, 1, C.addressof(buf))
        def reset(hwnd, value):
            select(hwnd, 0, -1); replace(hwnd, value)
        def selection(hwnd):
            packed = msg(hwnd, 0xB0)
            return [packed & 0xFFFF, (packed >> 16) & 0xFFFF]
        def modifier_guard():
            assert not any(U.GetAsyncKeyState(vk) < 0 for vk in MODIFIERS), "Physical modifier held; key experiment aborted"
            assert msg(host, 0x8001, host_only=True) == 0, "Fixture thread modifier held; key experiment aborted"
        def key(hwnd, vk):
            modifier_guard()
            scan = U.MapVirtualKeyW(vk, 0)
            bits = 1 | (scan << 16) | ((1 << 24) if vk in (0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2E) else 0)
            msg(hwnd, 0x100, vk, bits); msg(hwnd, 0x101, vk, bits | 0xC0000000)
        def chars(hwnd, value):
            modifier_guard()
            encoded = value.encode("utf-16-le")
            for i in range(0, len(encoded), 2):
                msg(hwnd, 0x102, int.from_bytes(encoded[i:i+2], "little"), 1)
        assert not (U.GetWindowLongPtrW(host, -20) & 0x08000000), "Unexpected NOACTIVATE style"
        checks.append({"stage": "ordinary_style_offscreen_fixture", "noActivateStyle": False, "hostPid": proc.pid})
        for name, hwnd in controls.items():
            for stage, action in [
                ("replace_selection", lambda: (reset(hwnd, "alpha beta"), select(hwnd, 6, 10), replace(hwnd, "世界"))),
                ("wm_char_unicode", lambda: (reset(hwnd, "A"), select(hwnd, 1, 1), chars(hwnd, "λ你🙂"))),
                ("left_then_delete", lambda: (reset(hwnd, "abcd"), select(hwnd, 3, 3), key(hwnd, 0x25), key(hwnd, 0x2E))),
                ("home_then_right", lambda: (reset(hwnd, "abcd"), select(hwnd, 3, 3), key(hwnd, 0x24), key(hwnd, 0x27))),
                ("wm_char_backspace", lambda: (reset(hwnd, "abcd"), select(hwnd, 3, 3), chars(hwnd, "\b"))),
                ("keydown_enter_only", lambda: (reset(hwnd, "ab"), select(hwnd, 1, 1), key(hwnd, 0xD))),
                ("wm_char_enter", lambda: (reset(hwnd, "ab"), select(hwnd, 1, 1), chars(hwnd, "\r"))),
            ]:
                before = metadata(); action(); time.sleep(0.015)
                operations.append({"control": name, "stage": stage, "text": text(hwnd),
                    "selection": selection(hwnd), "before": before, "after": metadata()})
        checks.append({"stage": "own_change_notifications", "count": msg(host, 0x8003, host_only=True)})
        timeout = msg(host, 0x8002, timeout=80, host_only=True, expect_timeout=True)
        assert not timeout["ok"] and timeout["elapsedMs"] < 300, timeout
        checks.append({"stage": "blocked_own_wndproc_timeout", **timeout})
        time.sleep(0.45)
        # ERRORONEXIT can make this return zero when the own window is destroyed.
        msg(host, 0x10, host_only=True, expect_timeout=True)
        proc.wait(timeout=2)
    except Exception as exc:
        failure = repr(exc)
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill(); proc.wait(timeout=2)
        finish_sampling.set(); sampler.join(timeout=1)
        if proc is not None:
            stderr = proc.stderr.read()
            if stderr and not failure: failure = stderr
        distinct = lambda key: len({json.dumps(s[key]) for s in samples})
        result = {"status": "completed" if failure is None else "failed", "failure": failure,
            "elapsedMs": round((time.monotonic()-started)*1000), "checks": checks, "operations": operations,
            "metadata": {"sampleCount": len(samples), "distinctForegroundHandles": distinct("foreground"),
                "distinctFocusHandles": distinct("focus"), "distinctCursorPositions": distinct("cursor"), "samples": samples},
            "fixtureProcessId": proc.pid if proc else None, "fixtureExited": proc is None or proc.poll() is not None,
            "limitations": "Only own native EDIT and Msftedit RICHEDIT50W fixtures. Message delivery is not physical keyboard equivalence or all-app support. Metadata sampling can miss shorter transitions; no simultaneous-user activity proof."}
        (HERE / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        print(json.dumps({k:v for k,v in result.items() if k not in {"metadata", "operations"}}, ensure_ascii=False))
        return 1 if failure else 0

if __name__ == "__main__":
    if "--fixture" in sys.argv: fixture()
    else: sys.exit(probe())
