"""Actual root MCP routing with an inert native fixture: never starts the controller."""
from pathlib import Path
import atexit
import importlib.util
import sys
import threading
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"native"))
import core_adapter


class NativeFixture:
    def __init__(self):
        self.operation=threading.RLock();self.generation=0;self.calls=[];self.on_revoke=None
    def call(self,operation,args):
        self.calls.append(operation)
        if operation=="state":return {"controller":{"attached":False,"paused":True,"sessionEpoch":0}}
        if operation=="invalidate_frame":return {"invalidated":True}
        raise AssertionError("Unexpected native operation: "+operation)


class RoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path=ROOT.parents[1]/"scripts"/"mcp_server.py"
        spec=importlib.util.spec_from_file_location("bwc_mcp_browser_fixture",path)
        cls.server=importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.server)
        # Import creates an inert Native object. Replace it before any tool call;
        # remove its atexit callback so fixture teardown cannot invoke native work.
        atexit.unregister(cls.server.NATIVE.stop)
    def setUp(self):
        self.server.NATIVE=NativeFixture();self.server.BROWSER=None
    def tearDown(self):
        if self.server.BROWSER and hasattr(self.server.BROWSER,"cancel"):self.server.BROWSER.cancel()

    def test_actual_core_not_ready_is_mcp_error_with_evidence(self):
        result=self.server.tool_call("browser",{"operation":"observe","arguments":{}})
        self.assertTrue(result["isError"])
        error=result["structuredContent"]["error"]
        self.assertEqual(error["code"],"browser_adapter_error")
        self.assertEqual(error["evidence"]["error"],"core_not_active")
        self.assertEqual(self.server.NATIVE.calls,["state"])
        self.assertIsInstance(self.server.BROWSER,core_adapter.CoreAdapter)
        self.assertIsNotNone(self.server.NATIVE.on_revoke)

    def test_browser_input_invalidates_native_frame_before_dispatch(self):
        events=self.server.NATIVE.calls
        class Adapter:
            def call(self,operation,args):
                events.append("browser:"+operation)
                return {"ok":True,"result":{"effectVerified":False,"verifyRequired":True}}
        self.server.BROWSER=Adapter()
        result=self.server.tool_call("browser",{"operation":"input","arguments":{"action":"media_pause"}})
        self.assertFalse(result["isError"])
        self.assertEqual(events,["invalidate_frame","browser:input"])
        self.assertFalse(result["structuredContent"]["result"]["effectVerified"])

    def test_connect_worker_can_verify_native_state_without_deadlock(self):
        native=self.server.NATIVE
        completed=threading.Event()
        workers=[]
        class Adapter:
            def call(self,operation,args):
                def authorize():
                    with native.operation:
                        native.call("state",{})
                        completed.set()
                worker=threading.Thread(target=authorize,daemon=True)
                workers.append(worker);worker.start()
                if not completed.wait(1):
                    raise RuntimeError("connect_blocked_native_identity_check")
                return {"ok":True,"bound":True}
        self.server.BROWSER=Adapter()
        result=self.server.tool_call("browser",{"operation":"connect"})
        for worker in workers:worker.join(1)
        self.assertFalse(result["isError"],result)
        self.assertEqual(native.calls,["state"])

    def test_stop_during_connect_cancels_late_result(self):
        native=self.server.NATIVE
        cancelled=[]
        class Adapter:
            def call(self,operation,args):
                native.generation+=1
                return {"ok":True,"bound":True}
            def cancel(self):cancelled.append(True)
        self.server.BROWSER=Adapter()
        result=self.server.tool_call("browser",{"operation":"connect"})
        self.assertTrue(result["isError"])
        self.assertIn("request_cancelled_by_stop",str(result))
        self.assertEqual(cancelled,[True])

    def test_unsupported_adapter_result_is_not_silent_success(self):
        class Adapter:
            def call(self,*args):return {"ok":False,"error":"unsupported_input","bound":True}
        self.server.BROWSER=Adapter()
        result=self.server.tool_call("browser",{"operation":"input","arguments":{"action":"drag"}})
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"]["message"],"unsupported_input")

    def test_pending_pairing_is_not_wrapped_as_mcp_failure_or_bound_success(self):
        class Adapter:
            def call(self,*args):return {"ok":True,"bound":False,"pending":True,"status":"waiting_for_native_host","remainingSeconds":118,"requiresUserAction":"toolbar_click_in_selected_zen_window"}
        self.server.BROWSER=Adapter()
        result=self.server.tool_call("browser",{"operation":"pair","arguments":{}})
        self.assertFalse(result["isError"])
        evidence=result["structuredContent"]
        self.assertTrue(evidence["pending"]);self.assertFalse(evidence["bound"])
        self.assertEqual(evidence["status"],"waiting_for_native_host")
        self.assertEqual(evidence["remainingSeconds"],118)
        self.assertNotIn("effectVerified",evidence)

    def test_stale_mcp_generation_does_not_reach_adapter(self):
        self.server.NATIVE.generation=5
        result=self.server.tool_call("browser",{"operation":"observe"},expected_generation=4)
        self.assertTrue(result["isError"])
        self.assertEqual(self.server.NATIVE.calls,[])
        self.assertIsNone(self.server.BROWSER)

    def test_unknown_browser_operation_is_rejected_before_native(self):
        result=self.server.tool_call("browser",{"operation":"eval","arguments":{"code":"anything"}})
        self.assertTrue(result["isError"])
        self.assertEqual(self.server.NATIVE.calls,[])
        self.assertIsNone(self.server.BROWSER)

    def test_disconnect_succeeds_without_launching_native(self):
        result=self.server.tool_call("browser",{"operation":"disconnect"})
        self.assertFalse(result["isError"])
        self.assertTrue(result["structuredContent"]["disconnected"])
        self.assertEqual(self.server.NATIVE.calls,[])


if __name__=="__main__":unittest.main(verbosity=2)
