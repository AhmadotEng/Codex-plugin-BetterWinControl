"""Full MCP -> controller -> native helper on an owned already-running Win32 fixture."""
import ctypes as C
import importlib.util
import json
import sys
import time
from integration_test import JsonProcess, PLUGIN, TESTS

spec = importlib.util.spec_from_file_location("owned_native", TESTS/"native-input"/"acceptance.py")
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)

def main():
    fixture = native.Client([str(TESTS/"native-input/bin/Release/net10.0-windows/Fixture.exe")])
    server = None
    checks = []
    try:
        ready = fixture.receive()
        hwnd = int(ready["target"])
        assert native.owner(hwnd) == fixture.process.pid == ready["pid"]
        server = JsonProcess([sys.executable, "-u", str(PLUGIN/"scripts/mcp_server.py")])
        server.rpc("initialize", {"protocolVersion":"2024-11-05","capabilities":{}, "clientInfo":{"name":"native-mcp-fixture","version":"1"}})
        server.send({"jsonrpc":"2.0","method":"notifications/initialized"})
        server.tool("attach_window", {"hwnd":hwnd})
        caps,_ = server.tool("capabilities")
        assert caps["input"]["helperBuilt"]
        assert caps["input"]["experimental"] and not caps["input"]["sharedPhysicalInputInjection"]
        def frame():
            for _ in range(20):
                data,_ = server.tool("observe", {"max_elements":30})
                if data["inputFrame"]["available"]: return data["inputFrame"]
                time.sleep(.15)
            raise AssertionError(data["inputFrame"])
        def point(f,x,y):
            b=f["clientBounds"]
            return {"x":x+b["x"],"y":y+b["y"]}
        f=frame()
        result,_=server.tool("input",{"frame_id":f["frameId"],"steps":[{"type":"click",**point(f,40,40)}]})
        assert result["effectVerified"] is False and result["delivered"]==3
        assert fixture.call("state")["clicks"] == 1
        checks.append({"name":"MCP coordinate click changes app state", "effectVerifiedByFixture":True})
        server.rejected("input", {"frame_id":f["frameId"],"steps":[{"type":"click",**point(f,40,40)}]}, "stale_frame")
        checks.append({"name":"used frame rejected"})
        f=frame()
        server.tool("input",{"frame_id":f["frameId"],"steps":[{"type":"double_click",**point(f,40,40)}]})
        assert fixture.call("state")["doubles"]==1
        checks.append({"name":"double click reaches actual handler"})
        f=frame()
        expected="MCP λ你🙂"
        server.tool("input",{"frame_id":f["frameId"],"steps":[{"type":"click",**point(f,30,250)},{"type":"text","text":expected}]})
        assert expected in fixture.call("state")["text"]
        checks.append({"name":"MCP Unicode delivered to native Edit"})
        f=frame()
        count=fixture.call("state")["chordActions"]
        server.tool("input",{"frame_id":f["frameId"],"steps":[{"type":"click",**point(f,40,40)},
            {"type":"shortcut","keysym":"Control_L+k"}]})
        assert fixture.call("state")["chordActions"] == count+1
        checks.append({"name":"reused key parser drives an actual native Ctrl+K shortcut"})
        f=frame()
        count=fixture.call("state")["clicks"]
        server.rejected("input",{"frame_id":f["frameId"],"steps":[{"type":"click",**point(f,40,40)},
            {"type":"shortcut","keysym":"KP_Enter"}]}, "unsupported")
        assert fixture.call("state")["clicks"] == count
        checks.append({"name":"unsupported named key rejects entire sequence before earlier click"})
        f=frame()
        bounds=native.RECT()
        assert native.U.GetWindowRect(hwnd,C.byref(bounds))
        native.U.SetWindowPos.argtypes=[C.c_void_p,C.c_void_p,C.c_int,C.c_int,C.c_int,C.c_int,C.c_uint]
        assert native.U.SetWindowPos(hwnd,None,0,0,bounds.right-bounds.left+20,bounds.bottom-bounds.top,0x16)
        server.rejected("input",{"frame_id":f["frameId"],"steps":[{"type":"click",**point(f,40,40)}]}, "stale_geometry")
        checks.append({"name":"resized target rejects old frame"})
        f=frame()
        count=fixture.call("state")["clicks"]
        native.U.ShowWindow.argtypes=[C.c_void_p,C.c_int]
        native.U.IsIconic.argtypes=[C.c_void_p]
        foreground=native.U.GetForegroundWindow()
        native.U.ShowWindow(hwnd,7) # SW_SHOWMINNOACTIVE: fixture only, no activation.
        assert native.U.IsIconic(hwnd)
        server.rejected("input",{"frame_id":f["frameId"],"steps":[{"type":"click",**point(f,40,40)}]}, "stale_geometry")
        assert fixture.call("state")["clicks"]==count
        native.U.ShowWindow(hwnd,4) # SW_SHOWNOACTIVATE: restore without taking focus.
        assert not native.U.IsIconic(hwnd)
        assert native.U.GetForegroundWindow()==foreground
        checks.append({"name":"minimized target rejects old frame without delivery; restore does not activate"})
        f=frame()
        server.tool("input",{"frame_id":f["frameId"],"steps":[{"type":"key_down","vk":17},{"type":"move",**point(f,320,100)},{"type":"button_down"}]})
        assert fixture.call("state")["dragging"]
        paused,_=server.tool("pause")
        assert paused["teardown"].get("hooksRemoved") and paused["teardown"].get("moduleUnloaded"), paused
        assert native.require_unloaded(fixture.process.pid)
        assert not fixture.call("state")["dragging"]
        checks.append({"name":"MCP pause releases held input and independently unloads DLL"})
        server.tool("resume")
        f=frame()
        server.tool("input",{"frame_id":f["frameId"],"steps":[{"type":"key_down","vk":16}]})
        stopped,_=server.tool("stop")
        assert stopped["teardown"].get("hooksRemoved") and stopped["teardown"].get("moduleUnloaded"),stopped
        assert native.require_unloaded(fixture.process.pid)
        checks.append({"name":"MCP stop verifies teardown before controller exit"})
        report={"passed":True,"scope":"MCP native owned-fixture integration; not physical co-use or real-app coverage","checks":checks}
        out=TESTS/"results"/"native-mcp.json";out.parent.mkdir(exist_ok=True);out.write_text(json.dumps(report,indent=2),encoding="utf-8")
        print(json.dumps(report))
    finally:
        if server: server.close()
        if fixture.process.poll() is None:
            try: fixture.call("shutdown")
            except Exception: pass
        fixture.close()

if __name__=="__main__":main()
