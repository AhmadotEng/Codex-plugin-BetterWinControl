"""Recorder tests use only disposable payloads, never live browser/user state."""
import concurrent.futures
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import trace_capsule as tc


class TraceCapsuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "capsules"
        self.recorders = []

    def tearDown(self):
        for recorder in self.recorders:
            recorder.close()
        self.temp.cleanup()

    def make(self, **kwargs):
        recorder = tc.TraceCapsule(self.root, metadata={"test": True}, flush_interval=0.01, **kwargs)
        self.recorders.append(recorder)
        return recorder

    def events(self, recorder, close=True):
        if close:
            recorder.close()
        else:
            self.assertTrue(recorder.flush())
        path = Path(recorder.status()["runDir"])
        result = []
        for filename in sorted(path.glob("events-*.jsonl")):
            result.extend(json.loads(line) for line in filename.read_text(encoding="utf-8").splitlines())
        for event in result:
            if "payloadArtifact" in event["data"]:
                event["data"] = json.loads((path / event["data"]["payloadArtifact"]).read_text(encoding="utf-8"))
        return result

    def payload(self, recorder, relative):
        return json.loads((Path(recorder.status()["runDir"]) / relative).read_text(encoding="utf-8"))

    def test_lazy_and_disabled_have_no_filesystem_effect(self):
        recorder = self.make()
        self.assertFalse(self.root.exists())
        self.assertIsNone(recorder.status()["directory"])
        recorder.close()
        self.assertFalse(self.root.exists())
        noop = tc.disabled()
        with noop.begin(noop.receive("input", {})):
            with noop.span("native"):
                noop.event("diagnostic", value="test")
        noop.end(None, {})
        self.assertFalse(noop.status()["enabled"])
        self.assertTrue(noop.flush())

    def test_full_payload_exact_including_sensitive_text_unicode_and_image(self):
        recorder = self.make()
        args = {"value": "private disposable password\n日本語", "clipboard": "disposable clipboard", "x": 31,
                "arguments": {"url": "https://example.invalid/?token=disposable", "action": "set_value"}}
        result = {"content": [{"type": "image", "mimeType": "image/png", "data": "AAECAw=="},
                              {"type": "text", "text": "private disposable page"}], "isError": False}
        token = recorder.receive("browser", args, request_id=12)
        with recorder.begin(token):
            recorder.end(token, result=result)
        args["x"] = 99
        result["content"].clear()
        events = self.events(recorder)
        received = next(e for e in events if e["event"] == "tool.received")
        ended = next(e for e in events if e["event"] == "tool.end")
        saved = self.payload(recorder, received["data"]["argumentsArtifact"])
        self.assertEqual(saved["x"], 31)
        self.assertEqual(saved["value"], "private disposable password\n日本語")
        self.assertEqual(self.payload(recorder, ended["data"]["resultArtifact"])["content"][0]["data"], "AAECAw==")
        self.assertEqual(received["data"]["requestId"], 12)

    def test_never_inspects_unrelated_environment(self):
        recorder = self.make()
        with mock.patch.dict(os.environ, {"BWC_UNRELATED_TEST_SECRET": "must-not-be-collected-62cc"}):
            recorder.event("test", value="requested content")
            self.events(recorder)
        saved = b"".join(p.read_bytes() for p in Path(recorder.status()["runDir"]).rglob("*") if p.is_file())
        self.assertNotIn(b"must-not-be-collected-62cc", saved)

    def test_timestamps_repeat_fingerprints_and_logical_ids(self):
        recorder = self.make()
        for observation_id in ["first", "first", "second"]:
            token = recorder.receive("browser", {"operation": "input", "observationId": observation_id, "action": "click"})
            time.sleep(0.002)
            with recorder.begin(token):
                recorder.end(token, result={"ok": True})
        events = self.events(recorder)
        received = [e for e in events if e["event"] == "tool.received"]
        self.assertEqual(received[0]["data"]["requestFingerprint"], received[1]["data"]["requestFingerprint"])
        self.assertNotEqual(received[0]["data"]["requestFingerprint"], received[2]["data"]["requestFingerprint"])
        self.assertEqual(received[0]["data"]["logicalFingerprint"], received[2]["data"]["logicalFingerprint"])
        self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)))
        self.assertTrue(all(e["timestampUtc"].endswith("Z") and e["schemaVersion"] == 1 for e in events))
        self.assertTrue(all(e["data"]["queueDelayMs"] >= 1 for e in events if e["event"] == "tool.start"))

    def test_nested_context_and_exception_propagation(self):
        recorder = self.make()
        token = recorder.receive("browser", {})
        with recorder.begin(token):
            with recorder.span("outer") as outer:
                with self.assertRaisesRegex(ValueError, "disposable error"):
                    with recorder.span("inner"):
                        recorder.event("inside", detail="failure example")
                        raise ValueError("disposable error")
            recorder.end(token, error=ValueError("disposable error"))
        recorder.event("outside")
        events = self.events(recorder)
        inner = next(e for e in events if e["event"] == "stage.start" and e["data"]["name"] == "inner")
        self.assertEqual(inner["parentSpanId"], outer)
        self.assertEqual(inner["traceId"], token.call_id)
        failed = next(e for e in events if e["event"] == "stage.end" and e["data"]["name"] == "inner")
        self.assertEqual(failed["data"]["outcome"], "error")
        self.assertEqual(failed["data"]["error"]["message"], "disposable error")
        self.assertNotIn("traceId", next(e for e in events if e["event"] == "outside"))

    def test_concurrent_calls_keep_context_and_lossless_overflow(self):
        recorder = self.make(queue_size=1, queue_bytes=1)
        def execute(index):
            token = recorder.receive("input", {"x": index})
            with recorder.begin(token):
                with recorder.span("local"):
                    recorder.event("index", value=index)
                recorder.end(token, {"index": index})
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(execute, range(40)))
        events = self.events(recorder)
        self.assertEqual(len([e for e in events if e["event"] == "tool.end"]), 40)
        received = {e["traceId"]: self.payload(recorder, e["data"]["argumentsArtifact"])["x"] for e in events if e["event"] == "tool.received"}
        for event in events:
            if event["event"] == "index":
                self.assertEqual(received[event["traceId"]], event["data"]["value"])
        self.assertEqual(recorder.status()["droppedEvents"], 0)
        self.assertGreater(recorder.status()["synchronousFallbacks"], 0)

    def test_rotation_and_large_generic_payload_have_no_truncation(self):
        recorder = self.make(rotate_bytes=900, inline_bytes=128)
        full = "日" * 30000
        for index in range(10):
            recorder.event("native.rpc", requestId=index, result={"text": full})
        events = self.events(recorder)
        self.assertGreater(len(recorder.status()["files"]), 1)
        self.assertEqual([e["data"]["result"]["text"] for e in events if e["event"] == "native.rpc"], [full] * 10)

    def test_error_result_and_duplicate_end(self):
        recorder = self.make()
        token = recorder.receive("input", {})
        with recorder.begin(token):
            recorder.end(token, {"isError": True, "content": []})
            recorder.end(token, {"ok": True})
        ended = [e for e in self.events(recorder) if e["event"] == "tool.end"]
        self.assertEqual(len(ended), 1)
        self.assertEqual(ended[0]["data"]["outcome"], "error")

    def test_invalid_tool_names_preserve_original_and_remain_reportable(self):
        import trace_report
        recorder = self.make()
        original_names = [None, {"invalid": "disposable value"}, "", "   "]
        for index, original in enumerate(original_names):
            token = recorder.receive(original, {"x": index}, request_id=index)
            with recorder.begin(token):
                recorder.end(token, {"isError": True, "error": "unknown_tool"})
        events = self.events(recorder)
        received = [e for e in events if e["event"] == "tool.received"]
        self.assertEqual([e["data"]["requestedTool"] for e in received], original_names)
        self.assertTrue(all(e["data"]["tool"] == "<invalid tool>" for e in received))
        summary = trace_report.analyze(Path(recorder.status()["runDir"]))
        self.assertEqual(summary["counts"]["calls"], len(original_names))
        self.assertEqual(summary["counts"]["outcomes"], {"error": len(original_names)})
        self.assertEqual(summary["integrityIssues"], [])

    def test_acl_failure_does_not_write_payloads_or_break_caller(self):
        recorder = self.make()
        with mock.patch.object(tc, "_private_directory", side_effect=PermissionError("disposable ACL failure")):
            token = recorder.receive("input", {"text": "must not be written"})
            with recorder.begin(token):
                recorder.end(token, {"ok": True})
        self.assertFalse(recorder.status()["enabled"])
        self.assertGreater(recorder.status()["writeErrors"], 0)
        self.assertFalse(self.root.exists())

    def test_shared_root_creation_race_does_not_disable_logging(self):
        recorder = self.make()
        original = tc._private_directory
        def racing_create(path):
            original(path)
            if path == self.root:
                # Simulate the other process creating the common root after this
                # process's exists() check, but before its mkdir() completes.
                raise FileExistsError("disposable concurrent root creation")
        with mock.patch.object(tc, "_private_directory", side_effect=racing_create):
            recorder.event("race", value="recorded")
        events = self.events(recorder)
        self.assertTrue(recorder.status()["enabled"])
        self.assertEqual(recorder.status()["writeErrors"], 0)
        self.assertEqual(next(e for e in events if e["event"] == "race")["data"]["value"], "recorded")

    def test_disk_failure_is_explicit_and_does_not_escape(self):
        recorder = self.make(queue_bytes=1)
        with mock.patch.object(recorder, "_artifact", side_effect=OSError("disposable disk full")):
            token = recorder.receive("input", {"text": "must be accounted"})
            with recorder.begin(token):
                recorder.end(token, {"ok": True})
        events = self.events(recorder)
        self.assertGreaterEqual(recorder.status()["droppedEvents"], 2)
        self.assertFalse(recorder.flush())
        final = next(e for e in events if e["event"] == "trace.close")
        self.assertGreaterEqual(final["data"]["writeErrors"], 2)
        self.assertEqual(final["data"]["pendingEvents"], 0)

    def test_snapshot_flush_and_no_stdout(self):
        recorder = self.make()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            recorder.event("snapshot", value=1)
            first = self.events(recorder, close=False)
            self.assertFalse(recorder.status()["closed"])
            recorder.event("snapshot", value=2)
            second = self.events(recorder)
        self.assertEqual(output.getvalue(), "")
        self.assertGreater(len(second), len(first))
        self.assertGreater(recorder.status()["recordingWorkMs"], 0)
        self.assertGreater(recorder.status()["writeWorkMs"], 0)
        final = next(e for e in second if e["event"] == "trace.close")
        self.assertGreater(final["data"]["recordingWorkMs"], 0)
        self.assertGreater(final["data"]["writeWorkMs"], 0)

    def test_async_queue_and_bounded_flush_timeout(self):
        recorder = self.make()
        # Hold the writer after the first event; ordinary receive must still
        # return without waiting for disk, and flush must respect its timeout.
        recorder.event("start")
        self.assertTrue(recorder.flush())
        with recorder._write_lock:
            started = time.perf_counter()
            token = recorder.receive("input", {"text": "disposable"})
            self.assertLess(time.perf_counter() - started, 0.2)
            started = time.perf_counter()
            self.assertFalse(recorder.flush(timeout=0.01))
            self.assertLess(time.perf_counter() - started, 0.2)
        with recorder.begin(token):
            recorder.end(token, {"ok": True})
        self.assertTrue(recorder.flush())
        self.assertEqual(recorder.status()["pendingEvents"], 0)

    @unittest.skipUnless(os.name == "nt", "Windows DACL only")
    def test_windows_dacl_protected_and_contains_only_current_user_and_system(self):
        import ctypes
        from ctypes import wintypes
        recorder = self.make()
        recorder.receive("input", {"text": "private disposable"})
        self.events(recorder)
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi.GetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.c_void_p]
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        # The directory is protected; children inherit these same two principals.
        for item in [Path(recorder.status()["runDir"]), next(Path(recorder.status()["runDir"]).glob("events-*.jsonl"))]:
            size = wintypes.DWORD()
            advapi.GetFileSecurityW(str(item), 4, None, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(size.value)
            self.assertTrue(advapi.GetFileSecurityW(str(item), 4, buffer, size, ctypes.byref(size)))
            text = wintypes.LPWSTR()
            self.assertTrue(advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(buffer, 1, 4, ctypes.byref(text), None))
            try:
                sddl = text.value
                self.assertEqual(sddl.count("(A;"), 2)
                self.assertIn(";;;SY)", sddl)
                # SDDL abbreviates this machine's built-in Administrator SID LA.
                self.assertTrue(";;;S-1-5-21-" in sddl or ";;;LA)" in sddl)
                if item.is_dir():
                    self.assertIn("D:P", sddl)
            finally:
                kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))

    @unittest.skipIf(os.name == "nt", "POSIX permissions only")
    def test_posix_permissions(self):
        recorder = self.make()
        recorder.receive("input", {})
        self.events(recorder)
        for item in Path(recorder.status()["runDir"]).rglob("*"):
            self.assertEqual(item.stat().st_mode & 0o777, 0o700 if item.is_dir() else 0o600)


if __name__ == "__main__":
    unittest.main()
