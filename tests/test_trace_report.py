"""Offline diagnostic-report fixtures; never inspect or control a live app."""
from datetime import datetime, timedelta, timezone
import base64
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "trace_report.py"
SPEC = importlib.util.spec_from_file_location("bwc_trace_report_test", MODULE)
report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(report)


class TraceFixture:
    def __init__(self, directory):
        self.path = Path(directory)
        self.events = []

    def event(self, name, at, data=None, call=None, span=None):
        stamp = datetime(2026, 9, 30, tzinfo=timezone.utc) + timedelta(milliseconds=at)
        event = {"schemaVersion": 1, "runId": "fixture-run", "seq": len(self.events) + 1,
                 "timestampUtc": stamp.isoformat(), "monotonicMs": at, "event": name, "data": data or {}}
        if call is not None:
            event["traceId"] = call
        if span is not None:
            event["spanId"] = span
        self.events.append(event)
        return event

    def write(self):
        (self.path / "events-000001.jsonl").write_text("".join(json.dumps(event) + "\n" for event in self.events), encoding="utf-8")
        return self.path

    def artifact(self, name, value):
        path = self.path / "payloads" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path.relative_to(self.path).as_posix()


class TraceReportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.trace = TraceFixture(self.temporary.name)

    def completed_call(self, name="browser", args=None, result=None, outcome="ok"):
        f = self.trace
        f.event("trace.start", 0)
        f.event("tool.received", 100, {"tool": name, "requestId": 42, "arguments": args or {}, "requestFingerprint": "exact", "logicalFingerprint": "logical"}, call="a")
        f.event("tool.start", 120, {"tool": name}, call="a")
        f.event("tool.end", 500, {"tool": name, "outcome": outcome, "result": result}, call="a")
        f.event("trace.close", 1000, {"closed": True})
        return f

    def test_concurrent_calls_union_and_repetition_without_double_counting(self):
        f = self.trace
        f.event("trace.start", 0)
        f.event("tool.received", 100, {"tool": "browser", "requestId": 1, "arguments": {"operation": "observe"}, "requestFingerprint": "same", "logicalFingerprint": "logical"}, call="a")
        f.event("tool.start", 120, {"tool": "browser"}, call="a")
        f.event("stage.start", 140, {"name": "browser.wait", "metadata": {"backend": "zen"}}, call="a", span="s1")
        f.event("tool.received", 200, {"tool": "browser", "requestId": 2, "arguments": {"operation": "observe"}, "requestFingerprint": "same", "logicalFingerprint": "logical"}, call="b")
        f.event("tool.start", 250, {"tool": "browser"}, call="b")
        f.event("stage.end", 400, {"name": "browser.wait", "outcome": "ok"}, call="a", span="s1")
        f.event("tool.end", 600, {"tool": "browser", "outcome": "ok", "result": {"effectVerified": False}}, call="a")
        f.event("tool.end", 800, {"tool": "browser", "outcome": "ok", "result": {}}, call="b")
        f.event("trace.close", 1000)
        summary = report.analyze(f.write())
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["timing"]["toolBusyUnionMs"], 700)
        self.assertEqual(summary["timing"]["outsidePluginMs"], 300)
        self.assertEqual(summary["timing"]["execution"]["sumMs"], 1030)
        self.assertEqual(summary["timing"]["queue"]["sumMs"], 70)
        self.assertEqual(summary["timing"]["endToEnd"]["sumMs"], 1100)
        self.assertEqual(summary["repetitions"]["exact"][0]["count"], 2)
        self.assertEqual(summary["stageTiming"][0]["meanMs"], 260)
        self.assertEqual(summary["calls"][0]["effectVerifiedFlags"], [False])
        self.assertEqual(summary["slowestCalls"], ["b", "a"])

    def test_html_is_inert_and_escapes_full_request_result(self):
        malicious = '<script>alert("secret")</script><img src="https://evil.invalid/x" onerror="run()">'
        f = self.completed_call(args={"operation": "input", "text": malicious}, result={"value": malicious})
        generated = report.generate_report(f.write())
        html = Path(generated["report"]).read_text(encoding="utf-8")
        self.assertNotIn("<script>", html)
        self.assertNotIn('<img src="https://evil.invalid', html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("default-src 'none'", html)
        summary = json.loads(Path(generated["summary"]).read_text(encoding="utf-8"))
        self.assertEqual(summary["calls"][0]["arguments"]["text"], malicious)
        self.assertEqual(summary["calls"][0]["result"]["value"], malicious)

    def test_lost_tail_and_dropped_events_are_incomplete(self):
        f = self.trace
        f.event("trace.start", 0)
        f.event("tool.received", 10, {"tool": "observe", "arguments": {}}, call="a")
        f.event("tool.start", 20, {"tool": "observe"}, call="a")
        f.event("trace.dropped", 30, {"droppedCount": 2, "totalDropped": 2})
        f.event("trace.close", 50, {"droppedEvents": 2, "writeErrors": 1})
        path = f.write() / "events-000001.jsonl"
        with path.open("ab") as stream:
            stream.write(b'{"event":')
        summary = report.analyze(f.path)
        self.assertFalse(summary["complete"])
        kinds = {item["kind"] for item in summary["integrityIssues"]}
        self.assertTrue({"missing_tool_end", "malformed_record", "unterminated_tail", "dropped_events", "writeErrors"}.issubset(kinds))
        self.assertEqual(summary["counts"]["outcomes"], {"incomplete": 1})
        self.assertEqual(summary["droppedEvents"], 2)

    def test_open_snapshot_reports_in_flight_without_claiming_corruption(self):
        f = self.trace
        f.event("trace.start", 0)
        f.event("tool.received", 10, {"tool": "diagnostics", "arguments": {"report": True}}, call="a")
        f.event("tool.start", 20, {"tool": "diagnostics"}, call="a")
        f.event("stage.start", 21, {"name": "report.render"}, call="a", span="s")
        summary = report.analyze(f.write())
        self.assertFalse(summary["closed"])
        self.assertFalse(summary["complete"])
        self.assertTrue(summary["completeThroughLastEvent"])
        self.assertEqual(summary["counts"]["unfinishedCalls"], 1)
        self.assertEqual(summary["calls"][0]["outcome"], "in_progress")
        self.assertIn("OPEN SNAPSHOT", report.render_html(summary))

    def test_pending_cancelled_and_error_are_not_false_success(self):
        f = self.trace
        f.event("trace.start", 0)
        for index, (outcome, value) in enumerate((("ok", {"pending": True}), ("error", {"error": {"code": "request_cancelled_by_stop"}}), ("ok", {"isError": True}))):
            call = str(index)
            f.event("tool.received", index * 100 + 1, {"tool": "browser", "arguments": {}}, call=call)
            f.event("tool.start", index * 100 + 2, {"tool": "browser"}, call=call)
            f.event("tool.end", index * 100 + 3, {"tool": "browser", "outcome": outcome, "result": value}, call=call)
        f.event("trace.close", 400)
        summary = report.analyze(f.write())
        self.assertEqual(summary["counts"]["pendingResponses"], 1)
        self.assertEqual(summary["counts"]["outcomes"], {"ok": 1, "cancelled": 1, "error": 1})

    def test_artifact_payloads_large_event_and_local_screenshots(self):
        # Valid one-pixel PNG, retained verbatim in the original result artifact.
        encoded = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+kfV8AAAAASUVORK5CYII="
        f = self.trace
        args_ref = f.artifact("args.json", {"operation": "observe"})
        original = {"content": [{"type": "image", "mimeType": "image/png", "data": encoded}], "structuredContent": {"ok": True}}
        result_ref = f.artifact("result.json", original)
        stage_ref = f.artifact("event.json", {"name": "zen.execute", "metadata": {"detail": "exact details"}})
        f.event("trace.start", 0)
        f.event("tool.received", 1, {"tool": "browser", "argumentsArtifact": args_ref}, call="a")
        f.event("tool.start", 2, {"tool": "browser"}, call="a")
        f.event("stage.start", 3, {"payloadArtifact": stage_ref}, call="a", span="s")
        f.event("stage.end", 4, {"name": "zen.execute", "outcome": "ok"}, call="a", span="s")
        f.event("tool.end", 5, {"tool": "browser", "outcome": "ok", "resultArtifact": result_ref}, call="a")
        f.event("trace.close", 6)
        paths = report.generate_report(f.write())
        summary = json.loads(Path(paths["summary"]).read_text(encoding="utf-8"))
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["calls"][0]["operation"], "observe")
        screenshot = f.path / summary["calls"][0]["screenshots"][0]["path"]
        self.assertEqual(screenshot.read_bytes(), base64.b64decode(encoded))
        self.assertEqual(json.loads((f.path / result_ref).read_text()), original)
        html = Path(paths["report"]).read_text(encoding="utf-8")
        self.assertIn("report-assets/call-1-image-1.png", html)
        self.assertNotIn(encoded, html)
        self.assertNotIn(encoded, Path(paths["summary"]).read_text(encoding="utf-8"))

    def test_artifact_traversal_is_rejected(self):
        f = self.completed_call()
        f.events[1]["data"] = {"tool": "browser", "argumentsArtifact": "../outside.json"}
        summary = report.analyze(f.write())
        self.assertFalse(summary["completeThroughLastEvent"])
        self.assertIn("missing_or_unsafe_payload", {issue["kind"] for issue in summary["integrityIssues"]})

    def test_unassociated_lifecycle_stages_are_retained(self):
        f = self.trace
        f.event("trace.start", 0)
        f.event("stage.start", 1, {"name": "native.shutdown"}, span="s")
        f.event("stage.end", 2, {"name": "native.shutdown", "outcome": "ok"}, span="s")
        f.event("trace.close", 3)
        summary = report.analyze(f.write())
        self.assertTrue(summary["complete"])
        self.assertEqual(len(summary["unassociatedStageEvents"]), 2)

    def test_fingerprints_are_not_recomputed_from_similar_visible_arguments(self):
        f = self.trace
        f.event("trace.start", 0)
        for index, fingerprint in enumerate(("first", "second")):
            call = str(index)
            f.event("tool.received", index * 10 + 1, {"tool": "input", "arguments": {}, "requestFingerprint": fingerprint, "logicalFingerprint": "same-logical"}, call=call)
            f.event("tool.start", index * 10 + 2, {"tool": "input"}, call=call)
            f.event("tool.end", index * 10 + 3, {"tool": "input", "outcome": "ok"}, call=call)
        f.event("trace.close", 30)
        summary = report.analyze(f.write())
        self.assertEqual(summary["repetitions"]["exact"], [])
        self.assertEqual(summary["repetitions"]["logical"][0]["count"], 2)

    def test_invalid_schema_sequence_and_duplicate_end_are_reported(self):
        f = self.completed_call()
        f.events[2]["schemaVersion"] = 9
        f.events.insert(4, dict(f.events[3]))
        summary = report.analyze(f.write())
        kinds = {issue["kind"] for issue in summary["integrityIssues"]}
        self.assertTrue({"invalid_schema", "sequence_gap_or_reorder", "duplicate_event", "missing_tool_start"}.issubset(kinds))
        self.assertFalse(summary["complete"])

    def test_producer_timings_are_preserved_with_event_intervals_for_comparison(self):
        f = self.completed_call()
        f.events[2]["data"]["queueDelayMs"] = 18
        f.events[3]["data"].update({"durationMs": 350, "totalMs": 368})
        summary = report.analyze(f.write())
        self.assertEqual(summary["calls"][0]["queueMs"], 18)
        self.assertEqual(summary["calls"][0]["executionMs"], 350)
        self.assertEqual(summary["calls"][0]["totalMs"], 368)
        self.assertEqual(summary["calls"][0]["recordedEventIntervals"], {"queueMs": 20, "executionMs": 380, "totalMs": 400})

    def test_closed_unfinished_lifecycle_span_is_incomplete(self):
        f = self.trace
        f.event("trace.start", 0)
        f.event("stage.start", 1, {"name": "native.shutdown"}, span="s")
        f.event("trace.close", 3)
        summary = report.analyze(f.write())
        self.assertFalse(summary["completeThroughLastEvent"])
        self.assertIn("incomplete_or_duplicate_lifecycle_stage", {issue["kind"] for issue in summary["integrityIssues"]})


if __name__ == "__main__":
    unittest.main()
