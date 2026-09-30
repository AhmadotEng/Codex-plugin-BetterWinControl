"""Exercise continuation past the former 200-node ceiling against an owned WPF fixture."""
import json
import sys
import time
from integration_test import JsonProcess, PLUGIN, TESTS, build_fixture

def main():
    fixture = JsonProcess([str(build_fixture()), "--many-nodes"])
    server = None
    checks = []
    try:
        ready = fixture.receive()
        assert ready["fixturePID"] == fixture.proc.pid
        server = JsonProcess([sys.executable, "-u", str(PLUGIN / "scripts/mcp_server.py")])
        server.rpc("initialize", {"protocolVersion":"2024-11-05","capabilities":{}, "clientInfo":{"name":"pagination-fixture","version":"1"}})
        server.send({"jsonrpc":"2.0","method":"notifications/initialized"})
        server.tool("attach_window", {"hwnd":ready["hwnd"]})
        page,_ = server.tool("observe", {"max_elements":50, "max_nodes":75})
        all_ids = set()
        pages = 0
        while True:
            obs = page["observation"]
            for e in obs["elements"]:
                assert e["id"] not in all_ids
                all_ids.add(e["id"])
            pages += 1
            token = obs.get("continuation")
            if not token:
                assert obs["complete"], obs
                break
            assert pages < 30
            page,_ = server.tool("observe", {"max_elements":50, "max_nodes":75, "continuation":token})
        assert len(all_ids) > 350, len(all_ids)
        checks.append({"name":"continuation visits more than 350 distinct nodes","count":len(all_ids),"pages":pages})
        page,_ = server.tool("observe", {"max_elements":10,"max_nodes":90,"search":"discovery-item-349"})
        pages = 1
        found = []
        while True:
            obs = page["observation"]
            found.extend(obs["elements"])
            token = obs.get("continuation")
            if not token: break
            assert pages < 30
            page,_ = server.tool("observe", {"max_elements":10,"max_nodes":90,"continuation":token})
            pages += 1
        assert any(e["automationId"] == "discovery-item-349" for e in found), found
        checks.append({"name":"search finds late control","pages":pages})
        page,_ = server.tool("observe", {"max_elements":1,"max_nodes":1})
        stale = page["observation"]["continuation"]
        assert stale
        server.tool("pause")
        server.rejected("observe", {"continuation":stale}, "stale_continuation")
        checks.append({"name":"pause revokes continuation"})
        server.tool("resume")
        page,_ = server.tool("observe", {"max_elements":1,"max_nodes":1})
        token = page["observation"]["continuation"]
        server.tool("observe", {"search":"no-match"})
        server.rejected("observe", {"continuation":token}, "stale_continuation")
        checks.append({"name":"new query revokes old continuation"})
        server.tool("stop")
        report = {"passed":True,"checks":checks}
        out = TESTS/"results"/"observation-search.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report))
    finally:
        if server: server.close()
        if fixture.proc.poll() is None:
            fixture.proc.stdin.write("stop\n"); fixture.proc.stdin.flush()
        fixture.close()

if __name__ == "__main__": main()
