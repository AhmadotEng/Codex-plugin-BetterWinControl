"""Full post-auth transport diagnostics; disposable in-memory fixtures only."""
from contextlib import contextmanager
from pathlib import Path
import copy
import queue
import sys
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "native"))
import bridge
import core_adapter


class Recorder:
    def __init__(self):
        self.rows = []
        self.lock = threading.Lock()

    def event(self, event_name, **metadata):
        with self.lock:
            self.rows.append((event_name, copy.deepcopy(metadata)))

    @contextmanager
    def span(self, name, **metadata):
        self.event("stage.start", name=name, metadata=metadata)
        try:
            yield
        finally:
            self.event("stage.end", name=name)


class Channel:
    def __init__(self, error=None):
        self.incoming = queue.Queue()
        self.sent = []
        self.closed = False
        self.error = error
        self.incoming.put({"v": 1, "type": "hello", "extensionId": bridge.EXTENSION_ID, "version": "fixture"})

    def read(self):
        value = self.incoming.get(timeout=2)
        if isinstance(value, BaseException):
            raise value
        return value

    def send(self, message):
        self.sent.append(copy.deepcopy(message))
        response = {"v": 1, "type": "reply", "id": message["id"],
                    "sessionId": message["sessionId"], "epoch": message["epoch"]}
        response.update({"error": self.error} if self.error else {"result": {"text": "full returned value"}})
        self.incoming.put(response)

    def close(self):
        if not self.closed:
            self.closed = True
            self.incoming.put(EOFError("fixture_channel_closed"))


class TransportTraceTests(unittest.TestCase):
    def connection(self, recorder, error=None, on_event=None):
        channel = Channel(error)
        connection = bridge.BrokerConnection(channel, {"clientPid": 42}, on_event, trace=recorder)
        def cleanup():
            connection.close()
            connection.reader.join(2)
        self.addCleanup(cleanup)
        return channel, connection

    def test_exact_request_and_reply_envelopes_include_correlation(self):
        trace = Recorder()
        channel, connection = self.connection(trace)
        args = {"text": "whole request 日本語", "elementId": "fixture-1"}
        self.assertEqual(connection.request("session", 3, "input", args), {"text": "full returned value"})
        request = next(data for name, data in trace.rows if name == "zen.transport.request")
        self.assertEqual(request["message"], channel.sent[0])
        self.assertEqual(request["message"]["args"], args)
        self.assertIsInstance(request["message"]["deadlineUnixMs"], int)
        reply = next(data for name, data in trace.rows if name == "zen.transport.received" and data["message"]["type"] == "reply")
        self.assertEqual(request["connectionId"], reply["connectionId"])
        self.assertEqual(request["message"]["id"], reply["message"]["id"])
        self.assertEqual(reply["message"]["sessionId"], "session")

    def test_full_error_reply_preserved_before_public_code_is_normalized(self):
        trace = Recorder()
        error = {"code": "invalid-code with spaces", "message": "original failure detail", "evidence": {"node": 71}}
        _, connection = self.connection(trace, error=error)
        with self.assertRaisesRegex(bridge.AdapterResponseError, "^adapter_error$"):
            connection.request("session", 3, "observe")
        reply = next(data["message"] for name, data in trace.rows if name == "zen.transport.received" and "error" in data["message"])
        self.assertEqual(reply["error"], error)
        self.assertFalse(connection.closed)

    def test_unsolicited_event_and_original_reader_error_preserved(self):
        trace = Recorder()
        handled = threading.Event()
        channel, connection = self.connection(trace, on_event=lambda *args: handled.set())
        self.assertTrue(connection.hello.wait(1))
        event = {"v": 1, "type": "event", "event": "permissions_revoked", "windowId": 9, "epoch": 3}
        channel.incoming.put(event)
        self.assertTrue(handled.wait(1))
        channel.incoming.put(ValueError("original_fixture_protocol_failure"))
        connection.reader.join(2)
        self.assertFalse(connection.reader.is_alive())
        self.assertTrue(any(name == "zen.transport.received" and data["message"] == event for name, data in trace.rows))
        failure = next(data for name, data in trace.rows if name == "zen.transport.reader_error")
        self.assertEqual(failure["error"], "original_fixture_protocol_failure")
        self.assertEqual(failure["errorType"], "ValueError")
        self.assertTrue(connection.closed)

    def test_failing_sink_does_not_change_transport_results_or_errors(self):
        class Failing:
            def event(self, *args, **kwargs):
                raise RuntimeError("diagnostic_sink_failed")
        _, good = self.connection(Failing())
        self.assertEqual(good.request("session", 3, "observe"), {"text": "full returned value"})
        _, bad = self.connection(Failing(), error={"code": "original_error"})
        with self.assertRaisesRegex(bridge.AdapterResponseError, "^original_error$"):
            bad.request("session", 3, "observe")
        self.assertFalse(bad.closed)


class AdapterTraceTests(unittest.TestCase):
    def adapter(self, trace):
        adapter = core_adapter.CoreAdapter(ROOT, lambda: {}, trace=trace)
        self.addCleanup(adapter.cancel)
        return adapter

    def test_rejected_identity_reason_and_candidate_are_recorded(self):
        trace = Recorder()
        adapter = self.adapter(trace)
        adapter.session_id = "selected-session"
        with mock.patch.object(adapter, "_check", side_effect=PermissionError("wrong_browser_profile")):
            self.assertIsNone(adapter._authorize(1234))
        failure = next(data for name, data in trace.rows if name == "zen.authorization.rejected")
        self.assertEqual(failure["candidatePid"], 1234)
        self.assertEqual(failure["sessionId"], "selected-session")
        self.assertEqual(failure["error"], "wrong_browser_profile")

    def test_event_trigger_is_recorded_before_session_cancellation(self):
        trace = Recorder()
        adapter = self.adapter(trace)
        adapter.session_id = "selected-session"
        event = {"event": "permissions_revoked", "windowId": 9}
        adapter._event(event, {"clientPid": 1234})
        self.assertEqual([name for name, _ in trace.rows], ["zen.event", "zen.session.cancelled"])
        self.assertEqual(trace.rows[0][1]["message"], event)
        self.assertEqual(trace.rows[0][1]["generation"], 0)
        self.assertEqual(adapter.generation, 1)

    def test_generic_adapter_exception_records_original_detail(self):
        trace = Recorder()
        adapter = self.adapter(trace)
        with mock.patch.object(adapter, "_prepare", side_effect=OSError("original disk detail")):
            result = adapter.call("prepare")
        self.assertEqual(result["error"], "browser_adapter_failed_or_outcome_unknown")
        failure = next(data for name, data in trace.rows if name == "zen.adapter.error")
        self.assertEqual((failure["errorType"], failure["error"]), ("OSError", "original disk detail"))

    def test_failing_stage_enter_or_exit_preserves_success_and_original_exception(self):
        class Failing:
            def __init__(self, enter): self.enter = enter
            def event(self, *args, **kwargs): raise RuntimeError("sink_event_failed")
            @contextmanager
            def span(self, *args, **kwargs):
                if self.enter: raise RuntimeError("sink_enter_failed")
                try: yield
                finally: raise RuntimeError("sink_exit_failed")
        for enter in (True, False):
            adapter = self.adapter(Failing(enter))
            with adapter._stage("fixture"):
                value = 42
            self.assertEqual(value, 42)
            with self.assertRaisesRegex(ValueError, "^application_error$"):
                with adapter._stage("fixture"):
                    raise ValueError("application_error")
            with mock.patch.object(adapter, "_prepare", side_effect=OSError("application_prepare_failed")):
                self.assertEqual(adapter.call("prepare")["error"], "browser_adapter_failed_or_outcome_unknown")


if __name__ == "__main__":
    unittest.main(verbosity=2)
