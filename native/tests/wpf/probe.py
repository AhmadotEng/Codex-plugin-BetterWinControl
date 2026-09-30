"""Capability research against only the owned WPF fixture."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

HERE=Path(__file__).resolve().parent
REPO=HERE.parents[2]
spec=importlib.util.spec_from_file_location("native_acceptance",REPO/"tests/native-input/acceptance.py")
helpers=importlib.util.module_from_spec(spec);spec.loader.exec_module(helpers)
HOST=REPO/"native/bin/x64/BetterWinControl.NativeHost.exe"
FIXTURE=HERE/"bin/Release/net10.0-windows/WpfInputFixture.exe"

def main():
    fixture=host=None;monitor=helpers.Monitor();checks=[];info=None;before=None;after=None;failure=None;started=time.monotonic();detached=False
    try:
        fixture=helpers.Client([str(FIXTURE)]);ready=fixture.receive(timeout=6);assert ready.get("ready"),ready;info=ready["result"]
        assert helpers.owner(info["hwnd"])==fixture.process.pid
        before=fixture.call("state");monitor.start();monitor.phase="native_wpf"
        host=helpers.Client([str(HOST),"--hwnd",str(info["hwnd"]),"--owner-pid",str(os.getpid())]);capabilities=host.call("capabilities",timeout=6)
        def state():return fixture.call("state",timeout=4)
        def click(point):
            host.call("move",**point);host.call("button",button="left",down=True);host.call("button",button="left",down=False)
        def record(name,action,verify):
            initial=state();error=None
            try: action()
            except Exception as exception:error=repr(exception)
            time.sleep(.15)
            final=state();ok=error is None and verify(initial,final)
            checks.append({"name":name,"passed":ok,"error":error,"before":initial,"after":final})
            # No fallback to a foreground route or automatic framework mutation.
        record("wpf_button_click",lambda:click(info["points"]["button"]),lambda a,b:b["clicks"]==a["clicks"]+1)
        def typing():click(info["points"]["text"]);host.call("text",text="Aλ你🙂")
        record("wpf_textbox_unicode",typing,lambda a,b:"Aλ你🙂" in b["text"])
        def wheel():host.call("move",**info["points"]["scroll"]);host.call("wheel",delta=-120)
        record("wpf_scrollviewer_wheel",wheel,lambda a,b:b["scrollOffset"]>a["scrollOffset"])
        def drag():
            host.call("move",**info["points"]["box"]);host.call("button",button="left",down=True);host.call("move",**info["points"]["dragDestination"]);host.call("button",button="left",down=False)
        record("wpf_canvas_drag",drag,lambda a,b:b["drags"]==a["drags"]+1 and (b["boxX"],b["boxY"])!=(a["boxX"],a["boxY"]))
        def chord():
            click(info["points"]["text"])
            for vk,down in ((17,True),(75,True),(75,False),(17,False)):host.call("key",vk=vk,down=down)
        record("wpf_keyboard_ctrl_k",chord,lambda a,b:b["keyChords"]==a["keyChords"]+1)
        def right_control():
            for vk,down in ((163,True),(17,True),(17,False),(75,True),(75,False),(163,False)):host.call("key",vk=vk,down=down)
        record("right_ctrl_survives_generic_release",right_control,lambda a,b:b["keyChords"]==a["keyChords"]+1 and any(e["key"]=="K" and e["control"] and e["rightControl"] and not e["leftControl"] for e in b["inputEvidence"][len(a["inputEvidence"]):]))
        def right_shift():
            for vk,down in ((161,True),(16,True),(16,False),(118,True),(118,False),(161,False)):host.call("key",vk=vk,down=down)
        record("right_shift_survives_generic_release",right_shift,lambda a,b:any(e["key"]=="F7" and e["shift"] and e["rightShift"] and not e["leftShift"] for e in b["inputEvidence"][len(a["inputEvidence"]):]))
        host.call("release");after=state();reply=host.call("detach",timeout=6);assert reply["hooksRemoved"] and reply["moduleUnloaded"];helpers.require_unloaded(fixture.process.pid);detached=True
        checks.append({"name":"independent_unload","passed":True})
        checks.append({"name":"same_process_sibling_unchanged","passed":after["canaryInputs"]==before["canaryInputs"]})
    except Exception as exception:failure=repr(exception)
    finally:
        if monitor.ready.is_set():monitor.stop()
        if host:host.close()
        if fixture:
            try:
                if fixture.process.poll() is None:
                    helpers.require_unloaded(fixture.process.pid)
                    fixture.call("quit");fixture.process.wait(timeout=4)
            except Exception as exception:
                if failure is None:failure=repr(exception)
            fixture.close()
    own_events=[e for e in monitor.events if info and e["pid"]==info["pid"]]
    target_focus=[s for s in monitor.samples if info and (s["foregroundPid"]==info["pid"] or s["focusPid"]==info["pid"])]
    checks.append({"name":"no_owned_focus_or_capture_events","passed":not own_events and not target_focus,"events":own_events})
    report={"status":"completed" if failure is None else "failed","failure":failure,"checks":checks,"supportedByFixture":[c["name"] for c in checks if c["passed"]],"unmet":[c["name"] for c in checks if not c["passed"]],"elapsedMs":round((time.monotonic()-started)*1000),"hostSha256":hashlib.sha256(HOST.read_bytes()).hexdigest(),"dllSha256":hashlib.sha256((HOST.parent/"VirtualInput.dll").read_bytes()).hexdigest(),"detached":detached,"samples":len(monitor.samples),"ownWinEvents":own_events,"ownedProcessesExited":all(p is None or p.process.poll() is not None for p in (fixture,host)),"limits":"Owned WPF fixture only; absent behavior is not generalized to every WPF application. No foreground fallback or programmatic control mutation."}
    (HERE/"results.json").write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    print(json.dumps({key:value for key,value in report.items() if key!="checks"},ensure_ascii=False))
    return 0 if failure is None else 1
if __name__=="__main__":raise SystemExit(main())
