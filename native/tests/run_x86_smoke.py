"""Owned 32-bit fixture only. No physical input is synthesized or collected."""
from __future__ import annotations
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]
HOST = ROOT / "native/bin/x86/BetterWinControl.NativeHost.exe"
FIXTURE = ROOT / "native/tests/bin/x86/NativeFixture.exe"
RESULT = Path(__file__).with_name("x86-results.json")
KERNEL = ctypes.WinDLL("kernel32", use_last_error=True)
USER = ctypes.WinDLL("user32", use_last_error=True)
KERNEL.CreateToolhelp32Snapshot.argtypes=[wintypes.DWORD,wintypes.DWORD]
KERNEL.CreateToolhelp32Snapshot.restype=wintypes.HANDLE
KERNEL.CloseHandle.argtypes=[wintypes.HANDLE]
class MODULEENTRY32W(ctypes.Structure):
    _fields_=[("dwSize",wintypes.DWORD),("th32ModuleID",wintypes.DWORD),("th32ProcessID",wintypes.DWORD),("GlblcntUsage",wintypes.DWORD),("ProccntUsage",wintypes.DWORD),("modBaseAddr",ctypes.c_void_p),("modBaseSize",wintypes.DWORD),("hModule",wintypes.HMODULE),("szModule",wintypes.WCHAR*256),("szExePath",wintypes.WCHAR*260)]
KERNEL.Module32FirstW.argtypes=[wintypes.HANDLE,ctypes.POINTER(MODULEENTRY32W)]
KERNEL.Module32NextW.argtypes=[wintypes.HANDLE,ctypes.POINTER(MODULEENTRY32W)]
USER.GetForegroundWindow.restype=wintypes.HWND
USER.GetCursorPos.argtypes=[ctypes.POINTER(wintypes.POINT)]
def module_loaded(pid):
    snap=KERNEL.CreateToolhelp32Snapshot(0x8|0x10,pid)
    assert snap != ctypes.c_void_p(-1).value, ctypes.get_last_error()
    entry=MODULEENTRY32W();entry.dwSize=ctypes.sizeof(entry)
    try:
        ok=KERNEL.Module32FirstW(snap,ctypes.byref(entry))
        assert ok, ctypes.get_last_error()
        while ok:
            if entry.szModule.lower()=="virtualinput.dll": return True
            ok=KERNEL.Module32NextW(snap,ctypes.byref(entry))
        return False
    finally: KERNEL.CloseHandle(snap)
def spawn(path,*args):
    return subprocess.Popen([str(path),*map(str,args)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding="utf-8",creationflags=subprocess.CREATE_NO_WINDOW)
def sample():
    point=wintypes.POINT();assert USER.GetCursorPos(ctypes.byref(point))
    return {"foreground":USER.GetForegroundWindow(),"cursor":[point.x,point.y]}
def main():
    fixture=host=None;checks=[];started=time.monotonic();status="failed";failure=None
    try:
        fixture=spawn(FIXTURE);info=json.loads(fixture.stdout.readline());assert info["pointerBits"]==32
        assert not module_loaded(info["pid"])
        before=sample()
        host=spawn(HOST,"--hwnd",info["hwnd"],"--owner-pid",os.getpid())
        sequence=0
        def call(op,**kwargs):
            nonlocal sequence
            sequence+=1;host.stdin.write(json.dumps({"id":sequence,"op":op,**kwargs},ensure_ascii=False)+"\n");host.stdin.flush()
            reply=json.loads(host.stdout.readline());assert reply["id"]==sequence,reply
            assert "error" not in reply,reply
            return reply["result"]
        call("capabilities");assert module_loaded(info["pid"]);checks.append("runtime_attach_32bit_module_present")
        call("move",x=60,y=50);call("button",button="left",down=True);call("button",button="left",down=False)
        call("key",vk=17,down=True);call("key",vk=75,down=True);call("key",vk=75,down=False);call("key",vk=17,down=False)
        call("button",x=30,y=145,button="left",down=True);call("button",button="left",down=False)
        phrase="Aλ你🙂";call("text",text=phrase)
        fixture.stdin.write("state\n");fixture.stdin.flush();state=json.loads(fixture.stdout.readline())
        assert state["clicks"]==1 and state["chords"]==1 and phrase in state["text"],state
        checks.append("actual_pointer_chord_and_unicode_effects")
        assert sample()==before,(before,sample());checks.append("foreground_and_cursor_samples_unchanged")
        detached=call("detach");assert detached["hooksRemoved"] and detached["moduleUnloaded"] and not module_loaded(info["pid"]),detached
        host.wait(timeout=3);checks.append("independent_module_absence_after_detach")
        fixture.stdin.write("quit\n");fixture.stdin.flush();fixture.wait(timeout=3);checks.append("owned_processes_exited")
        status="passed"
    except Exception as error: failure=repr(error)
    finally:
        if host and host.poll() is None:
            if host.stdin: host.stdin.close()
            try:host.wait(timeout=6)
            except subprocess.TimeoutExpired:host.kill();host.wait(timeout=3)
        if fixture and fixture.poll() is None:
            try:fixture.stdin.write("quit\n");fixture.stdin.flush();fixture.wait(timeout=3)
            except Exception:fixture.kill();fixture.wait(timeout=3)
        report={"status":status,"failure":failure,"checks":checks,"elapsedMs":round((time.monotonic()-started)*1000),"hostSha256":hashlib.sha256(HOST.read_bytes()).hexdigest(),"fixtureArchitecture":"x86","scope":"Owned fixture only; sparse cursor/foreground samples, not a replacement for the x64 continuous isolation suite.","ownedProcessesExited":all(p is None or p.poll() is not None for p in (host,fixture))}
        RESULT.write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8");print(json.dumps(report))
    return 0 if status=="passed" else 1
if __name__=="__main__": raise SystemExit(main())
