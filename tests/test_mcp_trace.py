"""Actual MCP + full trace integration, without attaching or controlling any window."""
from pathlib import Path
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from trace_capsule import TraceCapsule
from trace_report import generate_report


def events(directory):
    return [json.loads(line) for path in sorted(directory.glob("events*.jsonl"))
            for line in path.read_text(encoding="utf-8").splitlines()]


class TraceIntegration(unittest.TestCase):
    def test_real_stdio_records_every_tool_protocol_error_repeats_and_shutdown(self):
        with tempfile.TemporaryDirectory() as temp:
            env = dict(os.environ, BWC_TRACE_DIR=temp, PYTHONDONTWRITEBYTECODE="1")
            env.pop("BWC_TRACE_DISABLED", None)
            proc = subprocess.Popen([sys.executable, "-u", str(ROOT / "scripts/mcp_server.py")],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8", env=env, cwd=ROOT)
            def request(ident, method, params):
                proc.stdin.write(json.dumps({"jsonrpc":"2.0", "id":ident, "method":method, "params":params}) + "\n")
                proc.stdin.flush()
                reply = json.loads(proc.stdout.readline())
                self.assertEqual(reply["id"], ident)
                return reply
            try:
                request(1, "initialize", {"protocolVersion":"2025-06-18"})
                listing = request(2, "tools/list", {})
                self.assertIn("diagnostics", [tool["name"] for tool in listing["result"]["tools"]])
                request(3, "tools/call", {"name":"state", "arguments":{}})
                time.sleep(.04)  # Known outside-plugin interval, never call it model thinking.
                request(4, "tools/call", {"name":"state", "arguments":{}})
                failed = request(5, "tools/call", {"name":"browser", "arguments":{"operation":"observe"}})
                self.assertTrue(failed["result"]["isError"])
                failed = request(6, "tools/call", {"name":"act", "arguments":{"value":"FULL PRIVATE TEST CONTENT"}})
                self.assertTrue(failed["result"]["isError"])
                status = request(7, "tools/call", {"name":"diagnostics", "arguments":{}})
                self.assertTrue(status["result"]["structuredContent"]["trace"]["enabled"])
                report = request(8, "tools/call", {"name":"diagnostics", "arguments":{"report":True}})
                self.assertTrue(report["result"]["structuredContent"]["reportReady"], report)
                stopped = request(9, "tools/call", {"name":"stop", "arguments":{}})
                self.assertTrue(stopped["result"]["structuredContent"]["controllerExited"])
                proc.stdin.close()
                proc.wait(timeout=15)
                self.assertEqual(proc.returncode, 0, proc.stderr.read())
            finally:
                if proc.poll() is None:
                    proc.kill(); proc.wait(timeout=5)
                proc.stdout.close(); proc.stderr.close()
            runs = list(Path(temp).iterdir())
            self.assertEqual(len(runs), 1)
            run = runs[0]
            rows = events(run)
            receives = [r for r in rows if r["event"] == "tool.received"]
            endings = [r for r in rows if r["event"] == "tool.end"]
            self.assertEqual(len(receives), 7)
            self.assertEqual(len(endings), 7)
            self.assertEqual({r["data"]["requestId"] for r in receives}, set(range(3,10)))
            self.assertEqual(sum(r["data"]["outcome"] == "error" for r in endings), 2)
            states = [r for r in receives if r["data"]["tool"] == "state"]
            self.assertEqual(states[0]["data"]["requestFingerprint"], states[1]["data"]["requestFingerprint"])
            self.assertTrue(any(r["event"] == "stage.start" and r["data"]["name"] == "browser.preflight" for r in rows))
            self.assertFalse(any(r["event"] == "native.started" for r in rows))
            private = next(r for r in receives if r["data"]["tool"] == "act")
            self.assertEqual(json.loads((run / private["data"]["argumentsArtifact"]).read_text()) ["value"], "FULL PRIVATE TEST CONTENT")
            self.assertTrue((run / "report.html").is_file())
            self.assertTrue((run / "summary.json").is_file())

    def test_full_mcp_result_including_image_survives_wrapping(self):
        # Test the real wrapper with a canned response; no UI or physical input.
        old = os.environ.get("BWC_TRACE_DISABLED")
        os.environ["BWC_TRACE_DISABLED"] = "1"
        try:
            spec = importlib.util.spec_from_file_location("trace_fixture_mcp", ROOT / "scripts/mcp_server.py")
            server = importlib.util.module_from_spec(spec); spec.loader.exec_module(server)
        finally:
            if old is None: os.environ.pop("BWC_TRACE_DISABLED", None)
            else: os.environ["BWC_TRACE_DISABLED"] = old
        with tempfile.TemporaryDirectory() as temp:
            trace = TraceCapsule(temp)
            server.TRACE = trace
            png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN1sAAAAASUVORK5CYII="
            result = {"content":[{"type":"text", "text":"exact returned page text"},
                                 {"type":"image", "mimeType":"image/png", "data":png}],
                      "structuredContent":{"clipboard":"exact clipboard", "password":"explicit-full-fixture"}, "isError":False}
            server._tool_call = lambda *args: result
            self.assertEqual(server.tool_call("observe", {"include_screenshot":True}), result)
            trace.close()
            run = Path(trace.status()["runDir"])
            row = next(r for r in events(run) if r["event"] == "tool.end")
            self.assertEqual(json.loads((run / row["data"]["resultArtifact"]).read_text()), result)
            generated = generate_report(run)
            self.assertTrue((run / "report.html").is_file(), generated)


if __name__ == "__main__":
    unittest.main(verbosity=2)
