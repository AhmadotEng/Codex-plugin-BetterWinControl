"""Owned native-window input acceptance. No physical input injection or user text reads.

Results are individual capability gates; an unsupported/full-input gap is never counted
as success. Interactive 60-second user typing is deliberately not launched here.
"""
from __future__ import annotations
import argparse, ctypes as C, ctypes.wintypes as W, hashlib, json, os, pathlib, queue
import subprocess, sys, threading, time, traceback

ROOT = pathlib.Path(__file__).resolve().parent
REPO = ROOT.parent.parent
U = C.WinDLL("user32", use_last_error=True)
K = C.WinDLL("kernel32", use_last_error=True)
U.GetForegroundWindow.restype = W.HWND
U.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
U.IsWindow.argtypes = [W.HWND]
U.GetAsyncKeyState.argtypes = [C.c_int]
U.GetAsyncKeyState.restype = C.c_short

class POINT(C.Structure): _fields_ = [("x", W.LONG), ("y", W.LONG)]
class RECT(C.Structure): _fields_ = [("left", W.LONG), ("top", W.LONG), ("right", W.LONG), ("bottom", W.LONG)]
class GUI(C.Structure):
    _fields_ = [("size", W.DWORD),("flags",W.DWORD),("active",W.HWND),("focus",W.HWND),("capture",W.HWND),
                ("menu",W.HWND),("moveSize",W.HWND),("caret",W.HWND),("caretRect",RECT)]
U.GetGUIThreadInfo.argtypes=[W.DWORD,C.POINTER(GUI)]
U.GetCursorPos.argtypes=[C.POINTER(POINT)]
U.GetWindowRect.argtypes=[W.HWND,C.POINTER(RECT)]
U.WindowFromPoint.argtypes=[POINT];U.WindowFromPoint.restype=W.HWND
class MODULE(C.Structure):
    _fields_=[("size",W.DWORD),("id",W.DWORD),("pid",W.DWORD),("globalUse",W.DWORD),("processUse",W.DWORD),
              ("base",C.POINTER(C.c_byte)),("baseSize",W.DWORD),("module",W.HMODULE),("name",W.WCHAR*256),("path",W.WCHAR*260)]
K.CreateToolhelp32Snapshot.argtypes=[W.DWORD,W.DWORD];K.CreateToolhelp32Snapshot.restype=W.HANDLE
K.Module32FirstW.argtypes=[W.HANDLE,C.POINTER(MODULE)];K.Module32NextW.argtypes=[W.HANDLE,C.POINTER(MODULE)]
K.CloseHandle.argtypes=[W.HANDLE]
K.OpenEventW.argtypes=[W.DWORD,W.BOOL,W.LPCWSTR];K.OpenEventW.restype=W.HANDLE
K.SetEvent.argtypes=[W.HANDLE]
K.GetTickCount64.restype=C.c_ulonglong

def owner(hwnd):
    pid=W.DWORD();U.GetWindowThreadProcessId(hwnd,C.byref(pid));return pid.value

def injected_module_loaded(pid):
    snapshot=K.CreateToolhelp32Snapshot(0x18,pid)
    if snapshot in (None,C.c_void_p(-1).value):raise C.WinError(C.get_last_error())
    try:
        entry=MODULE(size=C.sizeof(MODULE));more=K.Module32FirstW(snapshot,C.byref(entry))
        if not more and C.get_last_error()!=18:raise C.WinError(C.get_last_error())
        while more:
            if entry.name.lower()=="virtualinput.dll":return True
            more=K.Module32NextW(snapshot,C.byref(entry))
            if not more and C.get_last_error()!=18:raise C.WinError(C.get_last_error())
        return False
    finally:K.CloseHandle(snapshot)

def require_unloaded(pid):
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        if not injected_module_loaded(pid):return True
        time.sleep(.03)
    raise AssertionError("VirtualInput.dll remains loaded in own fixture after detach/host loss")

class Client:
    def __init__(self, command):
        self.process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                      text=True,encoding="utf-8",creationflags=subprocess.CREATE_NO_WINDOW)
        self.output=queue.Queue();self.errors=[];self.index=0;self.closed=False
        threading.Thread(target=self.read,daemon=True).start()
        threading.Thread(target=lambda:self.errors.extend(self.process.stderr.read().splitlines()),daemon=True).start()
    def read(self):
        try:
            for line in self.process.stdout:
                try:self.output.put(json.loads(line))
                except Exception:self.output.put({"protocolError":line[:300]})
        finally:self.output.put({"eof":True})
    def receive(self,timeout=3):return self.output.get(timeout=timeout)
    def call(self,op,timeout=3,**args):
        self.index+=1;self.process.stdin.write(json.dumps({"id":self.index,"op":op,**args},ensure_ascii=False)+"\n");self.process.stdin.flush()
        response=self.receive(timeout)
        if response.get("id")!=self.index:raise RuntimeError("RPC response identity/protocol mismatch: "+repr(response))
        if "error" in response:raise RuntimeError("RPC error: "+repr(response["error"]))
        return response.get("result",response)
    def eof(self):
        if self.process.stdin and not self.process.stdin.closed:self.process.stdin.close()
        self.process.wait(timeout=4)
    def close(self):
        if self.process.poll() is None:
            try:self.eof()
            except Exception:self.process.kill();self.process.wait(timeout=3)

class Monitor:
    def __init__(self):self.events=[];self.samples=[];self.stopping=threading.Event();self.ready=threading.Event();self.hooks=[];self.phase="startup";self.failure=None
    def start(self):
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()
        if not self.ready.wait(3):raise RuntimeError(self.failure or "Metadata monitor did not become ready within three seconds")
    def run(self):
        try:self._run()
        except BaseException as exc:self.failure=f"{type(exc).__name__}: {exc}"
        finally:
            U.UnhookWinEvent.argtypes=[W.HANDLE]
            for hook in self.hooks:
                if not U.UnhookWinEvent(hook):self.failure=self.failure or "UnhookWinEvent failed during metadata monitor cleanup"
            self.hooks.clear()
    def _run(self):
        callback_type=C.WINFUNCTYPE(None,W.HANDLE,W.DWORD,W.HWND,W.LONG,W.LONG,W.DWORD,W.DWORD)
        def callback(hook,event,hwnd,obj,child,tid,timestamp):
            self.events.append({"event":event,"hwnd":int(hwnd or 0),"pid":owner(hwnd),"object":obj,"child":child,"thread":tid,"time":timestamp,"observedPhase":self.phase})
        self.callback=callback_type(callback)
        U.SetWinEventHook.argtypes=[W.DWORD,W.DWORD,W.HMODULE,callback_type,W.DWORD,W.DWORD,W.DWORD]
        U.SetWinEventHook.restype=W.HANDLE
        # Metadata only: foreground, focus and capture state. No keyboard/text hook.
        for event in (3,8,9,0x8005):
            hook=U.SetWinEventHook(event,event,None,self.callback,0,0,0)
            if not hook:raise C.WinError(C.get_last_error())
            self.hooks.append(hook)
        self.ready.set();message=W.MSG()
        while not self.stopping.is_set():
            while U.PeekMessageW(C.byref(message),None,0,0,1):U.TranslateMessage(C.byref(message));U.DispatchMessageW(C.byref(message))
            gui=GUI(size=C.sizeof(GUI));query=bool(U.GetGUIThreadInfo(0,C.byref(gui)));point=POINT();cursor_query=bool(U.GetCursorPos(C.byref(point)))
            foreground=int(U.GetForegroundWindow() or 0)
            self.samples.append({"t":time.monotonic(),"foreground":foreground,"foregroundPid":owner(foreground),"focus":int(gui.focus or 0),"focusPid":owner(gui.focus),
                                 "capture":int(gui.capture or 0),"guiQuery":query,"cursorQuery":cursor_query,"cursor":[point.x,point.y],
                                 "physicalHeld":[v for v in (1,2,4,0x10,0x11,0x12) if U.GetAsyncKeyState(v)&0x8000]})
            self.stopping.wait(.008)
    def stop(self):
        self.stopping.set();self.thread.join(3)
        if self.thread.is_alive():raise RuntimeError("Metadata monitor thread did not stop")
        if self.failure:raise RuntimeError("Metadata monitor failed: "+self.failure)

def main():
    parser=argparse.ArgumentParser();parser.add_argument("--host",type=pathlib.Path,default=REPO/"native/bin/x64/BetterWinControl.NativeHost.exe")
    parser.add_argument("--result",type=pathlib.Path,default=ROOT/"results.json");args=parser.parse_args()
    report={"scope":"Own ordinary Win32 fixture plus same-process canary and separate unfocused pad; no user applications or physical input injection",
            "startedAt":time.time(),"checks":[],"unimplemented":[],"interactiveCoUseRun":False,
            "limitations":["No arbitrary-app or complete system-control claim.","The separate pad is not foregrounded by this automated harness.",
                           "Virtual pointer preview is not exercised by native-host-only tests.","User physical modifier co-use requires the separately coordinated interactive gate."]}
    clients=[];monitor=Monitor();host=None;fixture=None;started=time.monotonic()
    def check(name,fn):
        monitor.phase=name;start_tick=K.GetTickCount64()
        try:
            evidence=fn();report["checks"].append({"name":name,"status":"passed","startTick":start_tick,"endTick":K.GetTickCount64(),"evidence":evidence})
        except Exception as exc:report["checks"].append({"name":name,"status":"failed","startTick":start_tick,"endTick":K.GetTickCount64(),"error":f"{type(exc).__name__}: {exc}"});raise
    try:
        assert args.host.is_file(),"Native host has not been built"
        executable=ROOT/"bin/Release/net10.0-windows/Fixture.exe";assert executable.is_file(),"Build Fixture.csproj first"
        fixture=Client([str(executable)]);clients.append(fixture);ready=fixture.receive();assert ready.get("ready")
        target,canary,edit=int(ready["target"]),int(ready["canary"]),int(ready["edit"])
        assert ready["pid"]==fixture.process.pid and all(owner(h)==fixture.process.pid for h in (target,canary,edit))
        pad=Client([str(executable),"--pad"]);clients.append(pad);pad_ready=pad.receive();assert pad_ready.get("ready") and pad_ready["pid"]==pad.process.pid
        report["fixture"]={"pid":fixture.process.pid,"target":target,"canary":canary,"editor":edit,"padPid":pad.process.pid}
        monitor.start()
        def attach(lifetime_owner=None):
            current=Client([str(args.host),"--hwnd",str(target),"--owner-pid",str(lifetime_owner or os.getpid())]);clients.append(current);return current
        host=attach();capabilities=host.call("capabilities");report["capabilities"]=capabilities
        assert injected_module_loaded(fixture.process.pid),"Own fixture did not load native input module"
        report["hostSha256"]=hashlib.sha256(args.host.read_bytes()).hexdigest()
        def state():return fixture.call("state")
        def move(x,y):return host.call("move",x=x,y=y)
        def button(x,y,down,which="left"):return host.call("button",button=which,down=down)
        def click(x,y,which="left"):move(x,y);button(x,y,True,which);return button(x,y,False,which)
        def wheel(x,y,delta):move(x,y);return host.call("wheel",delta=delta)
        def key(vk,down):return host.call("key",vk=vk,down=down)
        def verify(expected,**exact):
            actual=state()
            for name,value in exact.items():assert actual[name]==value,(name,value,actual[name])
            assert actual[expected],(expected,actual)
            return {k:actual[k] for k in set(exact)|{expected}}
        check("native_hover_changes_actual_target_state",lambda:(move(190,180),move(40,40),verify("hovered"))[-1])
        check("left_click_changes_counter",lambda:(click(40,40),verify("clicks",clicks=1))[-1])
        check("double_click_changes_counter",lambda:(move(40,40),host.call("double_click",button="left"),verify("doubles",doubles=1))[-1])
        check("right_click_changes_counter",lambda:(click(200,170,"right"),verify("rightClicks",rightClicks=1))[-1])
        check("wheel_changes_target_scroll_state",lambda:(wheel(200,170,120),verify("wheelSteps",wheelSteps=1))[-1])
        check("drag_changes_target_position_and_releases",lambda:(move(320,100),button(320,100,True),move(390,160),button(390,160,False),verify("drags",drags=1,boxX=370,boxY=140,dragging=False))[-1])
        def chord():
            move(400,190);click(400,190);key(0x11,True);key(0x4B,True);key(0x4B,False);key(0x11,False)
            current=state();assert current["chordActions"]==1,current
            assert any(k.get("vk")==0x4B and k.get("control") for k in current["keyObservations"]),current
            return {"chordActions":current["chordActions"],"keyStateWasVirtualCtrl":True}
        check("modifier_chord_changes_app_state",chord)
        def unicode():
            expected="Aλ你🙂e\u0301مرحبا"
            click(30,250);host.call("text",text=expected)
            current=state();assert expected in current["text"] and current["text"].replace(expected,"",1)=="seed",current
            native_state=host.call("state")
            assert int(native_state["virtualCaptureHwnd"])==0,("Native EDIT retained virtual capture after mouse-up",native_state)
            return {"ownTestText":current["text"],"editKeyMessages":current["editKeys"],"captureReleasedAfterClick":True,"nativeState":native_state}
        check("unicode_text_reaches_native_edit",unicode)
        def scope():
            current=state();pad_state=pad.call("state")
            assert current["canaryInputs"]==0,current
            assert pad_state["text"]=="" and pad_state["editKeys"]==0,"separate pad contaminated"
            return {"sameProcessCanaryInputs":current["canaryInputs"],"separatePadUnchanged":True}
        check("same_process_canary_and_separate_pad_isolation",scope)
        def occluded():
            covered=fixture.call("cover");bounds=RECT();assert U.GetWindowRect(target,C.byref(bounds));time.sleep(.05)
            points=[(bounds.left+8,bounds.top+8),(bounds.right-8,bounds.top+8),(bounds.left+8,bounds.bottom-8),
                    (bounds.right-8,bounds.bottom-8),((bounds.left+bounds.right)//2,(bounds.top+bounds.bottom)//2)]
            assert all(int(U.WindowFromPoint(POINT(x,y)) or 0)==covered["cover"] for x,y in points),"Own cover does not actually occlude target"
            before=state()["wheelSteps"];wheel(200,170,-120)
            current=state();assert current["wheelSteps"]==before-1,current
            return {"ownCoverVerifiedAtFivePoints":True,"wheelBefore":before,"wheelAfter":current["wheelSteps"]}
        check("covered_target_still_receives_scoped_action",occluded)
        def held_release():
            key(0x11,True);move(390,160);button(390,160,True)
            # The user may move the real pointer or press a modifier between RPCs.
            # Compare only bracketed stable intervals, retaining uncertain ones as
            # explicit inconclusive samples rather than calling movement a leak.
            tid=U.GetWindowThreadProcessId(target,None)
            def physical_snapshot():
                point=POINT();assert U.GetCursorPos(C.byref(point))
                gui=GUI(size=C.sizeof(GUI));assert U.GetGUIThreadInfo(tid,C.byref(gui))
                return (point.x,point.y,bool(U.GetAsyncKeyState(0x11)&0x8000),int(gui.focus or 0),int(gui.capture or 0))
            stable_samples=0;inconclusive_samples=0;attempts=0
            while attempts<100 and stable_samples<3:
                attempts+=1;before_probe=physical_snapshot()
                canary_probe=fixture.call("canary_probe")
                after_probe=physical_snapshot()
                if before_probe!=after_probe:
                    inconclusive_samples+=1;time.sleep(.002);continue
                expected_x,expected_y,expected_ctrl,expected_focus,expected_capture=before_probe
                assert canary_probe["controlDown"]==expected_ctrl,"Virtual Ctrl leaked into same-process canary API reads during a stable bracket"
                assert canary_probe["focus"]==expected_focus and canary_probe["capture"]==expected_capture,"Virtual focus/capture leaked into canary during a stable bracket"
                assert (canary_probe["cursorX"],canary_probe["cursorY"])==(expected_x,expected_y),"Virtual cursor leaked into canary during a stable bracket"
                stable_samples+=1
            sampling={"attempts":attempts,"stableMatchedSamples":stable_samples,"inconclusiveMovingSamples":inconclusive_samples}
            report["canaryPhysicalSampling"]=sampling
            assert stable_samples>=3,"inconclusive_physical_input_sampling: no three stable before/probe/after intervals within 100 attempts"
            first=host.call("release");second=host.call("release");current=host.call("state")
            assert not current.get("heldKeys") and not current.get("heldButtons"),current
            assert not state()["dragging"],"Target remains in a drag after release"
            return {"sameProcessCanaryApiIsolation":True,"physicalSampling":sampling,"firstRelease":first,"repeatedRelease":second,"state":current}
        check("release_is_idempotent_while_keys_and_button_held",held_release)
        def detached():
            result=host.call("detach");assert result.get("detached") and result.get("hooksRemoved"),result;host.eof()
            return {"reported":result,"independentModuleAbsent":require_unloaded(fixture.process.pid)}
        check("detach_reports_removed_hooks_and_exits",detached)
        host=attach();host.call("state")
        def eof_held():
            host.call("key",vk=0x10,down=True);host.eof()
            absent=require_unloaded(fixture.process.pid)
            releases=[k for k in state()["keyObservations"] if k.get("vk")==0x10]
            assert releases and releases[-1].get("down") is False,"EOF did not deliver the held Shift release to target"
            return {"exitCode":host.process.returncode,"independentModuleAbsent":absent,"targetReceivedShiftRelease":True}
        check("eof_while_modifier_held_exits",eof_held)
        host=attach();access=host.call("capabilities")
        def event_revoke_held():
            move(390,160);button(390,160,True);key(0x10,True);assert state()["dragging"]
            event=K.OpenEventW(2,False,access["revocationEventName"]);assert event,"Cannot open own advertised revocation event"
            try:assert K.SetEvent(event)
            finally:K.CloseHandle(event)
            absent=require_unloaded(fixture.process.pid);current=state()
            releases=[k for k in current["keyObservations"] if k.get("vk")==0x10]
            assert releases and releases[-1].get("down") is False,"Revocation did not deliver held Shift release"
            assert not current["dragging"],"Revocation left target dragging"
            outcome=host.call("detach");host.eof()
            return {"targetReceivedShiftRelease":True,"targetDragEnded":True,"independentModuleAbsent":absent,"detachAfterRevoke":outcome}
        check("out_of_band_revocation_releases_target_state_and_detaches",event_revoke_held)
        lifetime=Client([sys.executable,"-c","import time;time.sleep(60)"]);clients.append(lifetime)
        host=attach(lifetime.process.pid);host.call("state")
        def owner_loss():
            key(0x10,True);lifetime.process.kill();lifetime.process.wait(timeout=2);host.process.wait(timeout=5)
            absent=require_unloaded(fixture.process.pid);releases=[k for k in state()["keyObservations"] if k.get("vk")==0x10]
            assert releases and releases[-1].get("down") is False,"Owner loss did not release Shift"
            return {"ownedLifetimeProcessExited":True,"hostExited":host.process.poll() is not None,"moduleAbsent":absent,"targetReceivedShiftRelease":True}
        check("controlling_owner_process_loss_releases_and_unloads",owner_loss)
        host=attach();host.call("state")
        def target_closed():
            fixture.call("close_target");host.eof();assert not U.IsWindow(target),"Own target remained open"
            return {"targetExists":False,"exitCode":host.process.returncode}
        check("target_close_then_host_eof_exits",target_closed)
        report["unimplemented"] += ["Native context-menu interaction and owned-dialog routing await explicit host support/contract.",
                                    "Root nonclient chrome, cross-app drag/drop and real application matrix not yet established.",
                                    "No interactive physical-user co-use run was performed."]
    except Exception as exc:
        report["failure"]=f"{type(exc).__name__}: {exc}";report["traceback"]=traceback.format_exc(limit=4)
    finally:
        if monitor.ready.is_set():
            try:monitor.stop()
            except Exception as exc:report["monitorCleanupError"]=f"{type(exc).__name__}: {exc}"
        for client in reversed(clients):
            try:
                if client.process.poll() is None and client is not host:client.call("shutdown",timeout=1)
            except Exception:pass
            client.close()
        report["processes"]=[{"pid":c.process.pid,"exited":c.process.poll() is not None,"exitCode":c.process.returncode,"stderr":c.errors[:8]} for c in clients]
    own_pids={c.process.pid for c in clients};events=monitor.events;samples=monitor.samples
    own_events=[e for e in events if e["pid"] in own_pids and e["event"] in (3,8,0x8005)]
    report["inputIsolation"]={"events":events,"unexpectedOwnFocusOrCaptureEvents":own_events,"sampleCount":len(samples),
        "cursorPositions":len({tuple(s["cursor"]) for s in samples}),"physicalModifierStates":len({tuple(s["physicalHeld"]) for s in samples}),
        "ownForegroundSamples":sum(s["foregroundPid"] in own_pids for s in samples),
        "ownFocusSamples":sum(s["focusPid"] in own_pids for s in samples),
        "note":"Only numeric focus/capture events, physical cursor positions and modifier/button state read; no global keystrokes or user text recorded. User movement, if present, cannot automatically be attributed to the controller."}
    report["elapsedMs"]=round((time.monotonic()-started)*1000)
    if own_events or report["inputIsolation"]["ownForegroundSamples"] or report["inputIsolation"]["ownFocusSamples"]:
        report["failure"]=report.get("failure","Target acquired real focus/capture")
    if any(not p["exited"] for p in report["processes"]):report["failure"]=report.get("failure","Owned process survived test cleanup")
    if report.get("monitorCleanupError"):report["failure"]=report.get("failure",report["monitorCleanupError"])
    if len(report["checks"])!=16 or any(c["status"]!="passed" for c in report["checks"]):
        report["failure"]=report.get("failure","Incomplete native gate set: all 16 implemented checks must pass")
    report["status"]="failed" if "failure" in report else "implemented_subset_passed"
    report["fullCoveragePassed"]=False
    args.result.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"status":report["status"],"failure":report.get("failure"),"elapsedMs":report["elapsedMs"],"checks":len(report["checks"])}))
    return 1 if "failure" in report else 0

if __name__=="__main__":raise SystemExit(main())
