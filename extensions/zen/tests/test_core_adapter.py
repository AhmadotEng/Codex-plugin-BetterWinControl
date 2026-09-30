"""Controller policy fixtures plus actual Windows host/pipe transport; no Zen mutation."""
from pathlib import Path
import copy
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"native"))
import bridge
import core_adapter


class FixtureIdentity:
    def __init__(self):
        self.browser={"pid":1000,"parentPid":500,"path":r"C:\Program Files\Zen Browser\zen.exe","startTicks":638000000000000000,"commandLine":"zen.exe"}
        self.window_value={"hwnd":4321,"pid":1000,"threadId":123,"className":"MozillaWindowClass","left":100,"top":100,"width":1000,"height":800,"dpi":96}
        self.processes={1000:self.browser}
        self.siblings=[]
    def window(self,hwnd):
        if hwnd!=4321:raise core_adapter.AdapterError("selected_window_gone")
        return dict(self.window_value)
    def process(self,pid):return dict(self.processes[pid])
    def windows(self,pid):return [dict(self.window_value),*copy.deepcopy(self.siblings)]
    def argv(self,command):return core_adapter.WindowsIdentity().argv(command)
    def add_host(self,pid,parent=1000):
        self.processes[pid]={"pid":pid,"parentPid":parent,"path":sys.executable,"startTicks":self.browser["startTicks"]+100,
            "commandLine":f'"{sys.executable}" -u "{ROOT / "native" / "native_host.py"}"'}
    def state(self):
        return {"attached":True,"paused":False,"sessionEpoch":7,"window":{**self.window_value,"startTicks":self.browser["startTicks"],"process":"zen"}}


@unittest.skipUnless(os.name=="nt","Windows identity/pipe fixtures")
class CoreTests(unittest.TestCase):
    def setUp(self):
        self.identity=FixtureIdentity();self.state=self.identity.state()
        self.directory=ROOT/"tests"/"results"/("core-"+uuid.uuid4().hex)
        self.adapter=core_adapter.CoreAdapter(self.directory,lambda:copy.deepcopy(self.state),_identity=self.identity)
        self.wait_patch=mock.patch.object(core_adapter,"CONNECT_WAIT_SECONDS",.02)
        self.wait_patch.start()
    def tearDown(self):self.adapter.cancel();self.wait_patch.stop()

    def test_actual_current_process_identity_and_windows_argv(self):
        identity=core_adapter.WindowsIdentity();process=identity.process(os.getpid())
        self.assertEqual(core_adapter.canonical(process["path"]),core_adapter.canonical(sys.executable))
        self.assertGreater(process["startTicks"],core_adapter.DOTNET_FILETIME_OFFSET)
        self.assertGreater(process["parentPid"],0)
        self.assertTrue(identity.argv(process["commandLine"]))

    def test_prepare_does_not_register_or_claim_extension_installed(self):
        result=self.adapter.call("prepare",{})
        self.assertTrue(result["ok"]);self.assertFalse(result["registered"]);self.assertFalse(result["extensionInstalled"])
        self.assertTrue(self.adapter.launcher.is_file())
        self.assertEqual(self.adapter.call("pair",{})["error"],"adapter_not_connected")

    def test_pair_before_automatic_host_connect_is_pending_and_connect_is_idempotent(self):
        self.assertTrue(self.adapter.call("prepare",{})["ok"])
        first=self.adapter.call("connect",{})
        self.assertTrue(first["ok"]);self.assertTrue(first["pending"])
        self.assertEqual(first["connectionWindowSeconds"],30)
        generation,server,session,deadline=self.adapter.generation,self.adapter.server,self.adapter.session_id,self.adapter.connection_deadline
        pending=self.adapter.call("pair",{})
        self.assertTrue(pending["ok"]);self.assertFalse(pending["bound"])
        self.assertEqual(pending["status"],"waiting_for_native_host")
        self.assertGreater(pending["remainingSeconds"],0)
        self.assertFalse(pending["requiresUserAction"])
        self.assertNotIn("error",pending)
        again=self.adapter.call("connect",{})
        self.assertTrue(again["pending"])
        self.assertEqual((self.adapter.generation,self.adapter.server,self.adapter.session_id,self.adapter.connection_deadline),(generation,server,session,deadline))
        # Pending does not authorize page commands or create a binding.
        self.assertFalse(self.adapter.call("input",{"action":"select_tab","tabId":1})["ok"])
        self.assertFalse(self.adapter.bound)

    def test_listener_timeout_is_terminal_not_pending(self):
        self.assertTrue(self.adapter.call("prepare",{})["ok"])
        with mock.patch.object(core_adapter,"CONNECTION_WINDOW_SECONDS",0.05):
            self.assertTrue(self.adapter.call("connect",{})["ok"])
            end=time.monotonic()+2
            while self.adapter.error is None and time.monotonic()<end:time.sleep(.01)
        result=self.adapter.call("pair",{})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"],"native_host_connection_timed_out")
        self.assertNotIn("pending",result)
        self.assertIsNone(self.adapter.connection_deadline)

    def test_listener_failure_is_terminal_not_pending(self):
        self.assertTrue(self.adapter.call("prepare",{})["ok"])
        with mock.patch.object(bridge.BrokerServer,"accept",side_effect=PermissionError("fixture_denied")):
            self.adapter.call("connect",{})
            end=time.monotonic()+1
            while self.adapter.error is None and time.monotonic()<end:time.sleep(.01)
        result=self.adapter.call("pair",{})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"],"native_host_connection_failed")
        self.assertNotIn("pending",result)

    def test_stop_cancels_waiting_session_and_deadline(self):
        self.assertTrue(self.adapter.call("prepare",{})["ok"])
        self.assertTrue(self.adapter.call("connect",{})["pending"])
        generation=self.adapter.generation
        self.state["attached"]=False
        result=self.adapter.call("pair",{})
        self.assertEqual(result["error"],"core_not_active")
        self.assertGreater(self.adapter.generation,generation)
        self.assertIsNone(self.adapter.connection_deadline)
        self.assertIsNone(self.adapter.snapshot)
        self.assertIsNone(self.adapter.server)
        self.state["attached"]=True
        self.assertEqual(self.adapter.call("pair",{})["error"],"adapter_not_connected")

    def candidate(self,ident=1,**geometry):
        return {"windowId":ident,"pairingNonce":"nonce-"+str(ident),"expiresAt":int(time.time()*1000)+30000,
                "window":{"id":ident,"type":"normal","incognito":False,"left":100,"top":100,"width":1000,"height":800,**geometry}}

    def install_connection(self,candidates=None,on_bind=None):
        adapter=self.adapter;owner=self
        class Connection:
            closed=False
            peer={"browserPid":1000,"browserStartTicks":owner.state["window"]["startTicks"],"selectedHwnd":4321}
            def close(self):self.closed=True
            def request(self,session,epoch,op,args,**kwargs):
                if op=="pair_candidates":return {"candidates":candidates or [owner.candidate()]}
                if op=="bind":
                    if on_bind:on_bind()
                    return {"bound":True,"windowId":args["windowId"],"epoch":epoch,"window":owner.candidate(args["windowId"])["window"]}
                raise AssertionError(op)
        adapter.snapshot=adapter._state();adapter.connection=Connection()
        adapter.session_id="fixture-session"
        return adapter.connection

    def test_automatic_pair_requires_no_foreground_or_toolbar_event(self):
        self.install_connection()
        self.assertEqual(self.adapter.pairs,[])
        result=self.adapter.call("pair",{})
        self.assertTrue(result["ok"]);self.assertTrue(result["bound"])
        self.assertEqual(result["mapping"],"unique_authenticated_window_geometry")

    def test_automatic_pair_rejects_reciprocal_and_browser_ambiguity(self):
        self.install_connection()
        self.identity.siblings=[{**self.identity.window_value,"hwnd":5432,"threadId":456}]
        self.assertEqual(self.adapter.call("pair",{})["error"],"browser_window_mapping_ambiguous")
        self.identity.siblings=[]
        self.install_connection([self.candidate(1),self.candidate(2)])
        self.assertEqual(self.adapter.call("pair",{})["error"],"browser_window_mapping_ambiguous")

    def test_post_bind_geometry_change_revokes_connection(self):
        connection=self.install_connection(on_bind=lambda:self.identity.window_value.update(left=400))
        result=self.adapter.call("pair",{})
        self.assertEqual(result["error"],"window_geometry_changed_during_pairing")
        self.assertTrue(connection.closed);self.assertFalse(self.adapter.bound)
        self.assertIsNone(self.adapter.snapshot)

    def test_changed_browser_post_bind_geometry_revokes_connection(self):
        connection=self.install_connection()
        original=connection.request
        def request(*args,**kwargs):
            result=original(*args,**kwargs)
            if args[2]=="bind":result["window"]["left"]+=400
            return result
        connection.request=request
        self.assertEqual(self.adapter.call("pair",{})["error"],"browser_window_mapping_ambiguous")
        self.assertTrue(connection.closed)

    def test_identity_rejects_changed_start_pid_and_wrong_host_command(self):
        self.adapter.snapshot=self.adapter._state()
        self.identity.add_host(2000)
        self.assertIsNotNone(self.adapter._authorize(2000))
        self.identity.processes[2000]["commandLine"]+=' --unexpected'
        self.assertIsNone(self.adapter._authorize(2000))
        self.identity.add_host(2000)
        self.identity.processes[2000]["parentPid"]=1001
        self.assertIsNone(self.adapter._authorize(2000))
        self.identity.add_host(2000)
        self.identity.browser["startTicks"]+=1
        self.assertIsNone(self.adapter._authorize(2000))

    def test_pause_cancels_connection_without_new_browser_action(self):
        self.adapter.snapshot=self.adapter._state()
        class Fake:
            closed=False
            def close(self):self.closed=True
        connection=Fake();self.adapter.connection=connection
        self.state["paused"]=True
        result=self.adapter.call("observe",{})
        self.assertEqual(result["error"],"core_not_active");self.assertTrue(connection.closed)

    def test_exact_launcher_chain_and_unknown_wrapper(self):
        self.adapter.snapshot=self.adapter._state()
        self.identity.add_host(2000,1500)
        command=str(Path(os.environ["SystemRoot"])/"System32"/"cmd.exe")
        self.identity.processes[1500]={"pid":1500,"parentPid":1000,"path":command,"startTicks":self.identity.browser["startTicks"]+50,
            "commandLine":f'"{command}" /d /c "{self.adapter.launcher}"'}
        self.assertIsNotNone(self.adapter._authorize(2000))
        self.identity.processes[1500]["commandLine"]+=' & calc.exe'
        self.assertIsNone(self.adapter._authorize(2000))
        self.identity.processes[1500]["path"]="unexpected.exe"
        self.assertIsNone(self.adapter._authorize(2000))

    def test_shipped_gecko_batch_manifest_and_addon_arguments(self):
        self.adapter.snapshot=self.adapter._state()
        self.identity.add_host(2000,1500)
        command=str(Path(os.environ["SystemRoot"])/"System32"/"cmd.exe")
        parent={"pid":1500,"parentPid":1000,"path":command,"startTicks":self.identity.browser["startTicks"]+50}
        self.identity.processes[1500]=parent
        for launcher in [self.adapter.launcher, self.directory/"folder with spaces"/"betterwincontrol-zen-native.bat"]:
            self.adapter.launcher=launcher
            manifest=launcher.with_name("local.betterwincontrol.zen.json")
            def quote(value):
                text=str(value)
                return '"'+text+'"' if any(char.isspace() for char in text) else text
            # Independent transcription of Zen's installed Subprocess worker:
            # CreateProcessW(fullComspec, ['cmd.exe','/s/c','"'+args.join(' ')+'"'].join(' ')).
            raw='cmd.exe /s/c "'+' '.join([quote(launcher),quote(manifest),bridge.EXTENSION_ID])+'"'
            parent["commandLine"]=raw
            self.assertIsNotNone(self.adapter._authorize(2000))
            for invalid in [raw+' & calc.exe', raw.replace(bridge.EXTENSION_ID,"other@invalid"),
                            raw.replace("local.betterwincontrol.zen.json","other.json"),
                            raw[:-1]+' unexpected"', raw.replace('/s/c','/s/k')]:
                parent["commandLine"]=invalid
                self.assertIsNone(self.adapter._authorize(2000),"Unexpected launcher command accepted")
        parent["commandLine"]=core_adapter.gecko_batch_command(self.adapter.launcher)
        parent["parentPid"]=500
        self.assertIsNone(self.adapter._authorize(2000))

    def test_full_core_bridge_fixture_and_epoch_revocation(self):
        self.assertTrue(self.adapter.call("prepare",{})["ok"])
        self.assertEqual(self.adapter.call("connect",{})["status"],"waiting_for_native_host")
        proc=subprocess.Popen([sys.executable,"-u",str(ROOT/"native"/"native_host.py")],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            env={**os.environ,"BWC_ZEN_CONFIG":str(self.adapter.config)},creationflags=subprocess.CREATE_NO_WINDOW)
        # Authorizer runs after process startup; configure identity immediately.
        self.identity.add_host(proc.pid)
        replies=[];failures=[]
        try:
            bridge.write_native(proc.stdin,{"v":1,"type":"hello","extensionId":bridge.EXTENSION_ID})
            end=time.monotonic()+3
            while not self.adapter.connection and time.monotonic()<end:time.sleep(.01)
            self.assertIsNotNone(self.adapter.connection)
            def responder():
                try:
                    while True:
                        request=bridge.read_native(proc.stdout);replies.append(request)
                        if request.get("type")=="transport":continue
                        if request["op"]=="pair_candidates":result={"candidates":[self.candidate()]}
                        elif request["op"]=="bind":result={"bound":True,"windowId":1,"epoch":request["epoch"],"window":self.candidate()["window"]}
                        else:result={"fixture":True,"operation":request["op"]}
                        bridge.write_native(proc.stdin,{"v":1,"type":"reply","id":request["id"],"sessionId":request["sessionId"],"epoch":request["epoch"],"result":result})
                except EOFError:pass
                except Exception as ex:failures.append(type(ex).__name__)
            thread=threading.Thread(target=responder,daemon=True);thread.start()
            self.assertTrue(self.adapter.call("capabilities",{})["ok"])
            # OS window mapping is a fixture. Transport/authentication are real;
            # automatic candidates replace any foreground/toolbar event.
            self.assertTrue(self.adapter.call("pair",{})["ok"])
            generation,connection=self.adapter.generation,self.adapter.connection
            self.assertEqual(self.adapter.call("connect",{})["status"],"bound")
            self.assertEqual(self.adapter.generation,generation)
            self.assertIs(self.adapter.connection,connection)
            self.assertIsNone(self.adapter.connection_deadline)
            self.assertTrue(self.adapter.call("observe",{})["ok"])
            self.assertTrue(self.adapter.call("input",{"action":"media_pause"})["ok"])
            self.assertTrue(self.adapter.call("verify",{"actionId":"fixture"})["ok"])
            count=len(replies);self.state["sessionEpoch"]+=1
            self.assertEqual(self.adapter.call("input",{})["error"],"core_selection_changed")
            time.sleep(.1)
            self.assertIsNone(proc.poll()) # Host returns to idle after core revocation.
            self.assertFalse(any(item.get("op")=="input" for item in replies[count:]))
            proc.stdin.close();proc.wait(timeout=3);thread.join(1)
            self.assertEqual(proc.returncode,0);self.assertEqual(failures,[])
        finally:
            self.adapter.cancel()
            if proc.poll() is None:proc.kill();proc.wait(timeout=3)
            for stream in [proc.stdin,proc.stdout,proc.stderr]:stream.close()

    def test_pair_rejects_geometry_and_stale_nonce(self):
        self.install_connection([self.candidate(left=5000)])
        self.assertEqual(self.adapter.call("pair",{})["error"],"browser_window_mapping_ambiguous")
        expired=self.candidate();expired["expiresAt"]=int(time.time()*1000)-1
        self.install_connection([expired])
        self.assertEqual(self.adapter.call("pair",{})["error"],"pairing_candidate_expired")


if __name__=="__main__":unittest.main(verbosity=2)
