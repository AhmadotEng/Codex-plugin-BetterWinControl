"""Bounded reporting/validation regression tests. Never creates windows or input."""
import io
import json
import unittest
from unittest.mock import patch

import interactive_co_use as gate


def complete_report():
    return {
        "automatedRuns": [{"run": i, "passed": True, "requiredChecksPassed": True,
                           "lifecycleVerified": True} for i in range(1, 4)],
        "passes": [{"pass": i, "covered": i == 2, "status": "passed", "observedSeconds": 60.001,
                    "actions": 100, "verifiedActionCycles": 9, "interleavedTypingCycles": 3,
                    "physicalCtrlCycles": 3, "physicalShiftCycles": 3, "stableCanaryComparisons": 10,
                    "stableCanaryFocusComparisons": 10, "exactDisposablePassage": True,
                    "physicalCursorPositions": 3, "handsStillSamples": 20, "handsStillCursorPositions": 1,
                    "handsStillEvidence": {"verified": True},
                    "visibleVerifiedAtFivePoints": i == 1, "coverVerifiedAtFivePoints": i == 2}
                   for i in (1, 2)],
        "lifecycle": {k: True for k in ("released", "detached", "hooksRemoved", "independentModuleAbsent", "hostExited")},
        "allOwnedProcessesExited": True, "monitorStarted": True, "monitorStopped": True,
        "metadataSampleCount": 100, "targetFocusOrCaptureEvents": [],
        "targetForegroundSamples": 0, "targetFocusSamples": 0,
    }


class ReportingTests(unittest.TestCase):
    def test_complete_evidence_only_success(self):
        report = complete_report()
        self.assertEqual(gate.finalize_report(report), 0)
        self.assertEqual(report["status"], "owned_fixture_co_use_passed")
        self.assertFalse(report["fullCoveragePassed"])

    def test_empty_assertion_never_passes(self):
        report = complete_report()
        try:
            raise AssertionError()
        except AssertionError as exc:
            gate.record_failure(report, exc, "test")
        self.assertEqual(gate.finalize_report(report), 1)
        self.assertIn("AssertionError", report["failure"])
        self.assertTrue(report["failures"][0]["traceback"])
        self.assertTrue(report["failures"][0]["message"])

    def test_legacy_falsey_failure_never_passes(self):
        for value in ("", None, False, 0):
            with self.subTest(value=value):
                report = complete_report()
                report["failure"] = value
                self.assertEqual(gate.finalize_report(report), 1)

    def test_partial_and_extra_passes_rejected(self):
        for count in (0, 1, 3):
            report = complete_report()
            report["passes"] = (report["passes"] * 2)[:count]
            self.assertEqual(gate.finalize_report(report), 1)

    def test_duration_action_count_and_required_evidence(self):
        cases = {"status": "running", "observedSeconds": 59.9999, "actions": 99,
                 "interleavedTypingCycles": 2, "physicalCtrlCycles": 2, "physicalShiftCycles": 2,
                 "stableCanaryComparisons": 9, "stableCanaryFocusComparisons": 9,
                 "exactDisposablePassage": False, "physicalCursorPositions": 2,
                 "handsStillSamples": 19, "handsStillCursorPositions": 2,
                 "visibleVerifiedAtFivePoints": False}
        for key, value in cases.items():
            with self.subTest(key=key):
                report = complete_report()
                report["passes"][0][key] = value
                self.assertEqual(gate.finalize_report(report), 1)

    def test_every_lifecycle_field_required(self):
        for key in complete_report()["lifecycle"]:
            report = complete_report()
            del report["lifecycle"][key]
            self.assertEqual(gate.finalize_report(report), 1)

    def test_cleanup_monitor_and_isolation_are_required(self):
        for key, value in {"allOwnedProcessesExited": False, "monitorStarted": False,
                           "monitorStopped": False, "metadataSampleCount": 0,
                           "targetFocusOrCaptureEvents": [{"pid": 1}],
                           "targetForegroundSamples": 1, "targetFocusSamples": 1}.items():
            report = complete_report()
            report[key] = value
            self.assertEqual(gate.finalize_report(report), 1)

    def test_empty_legacy_failure_in_saved_report_is_rejected(self):
        path = gate.ROOT / "results-interactive-failed-1790725604.json"
        if not path.exists():
            self.skipTest("Historical local failed report is not shipped")
        report = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(report["failure"], "")
        self.assertEqual(report["status"], "owned_fixture_co_use_passed")
        self.assertEqual(gate.finalize_report(report), 1)
        self.assertIn("exactly_two_passes_required", report["completionErrors"])

    def test_failure_preserves_numeric_evidence_and_running_pass(self):
        report = {"passes": [{"status": "running"}]}
        try:
            gate.require(False, "canary_focus_capture_leak", "Canary differs", focus=10, capture=20)
        except gate.GateFailure as exc:
            gate.record_failure(report, exc, "cycle")
        self.assertEqual(report["passes"][0]["status"], "failed")
        self.assertEqual(report["failures"][0]["evidence"], {"focus": 10, "capture": 20})
        self.assertNotIn(gate.PASSAGE, json.dumps(report))

    def test_require_active_with_python_optimization(self):
        with self.assertRaises(gate.GateFailure):
            gate.require(False, "required", "Required gate")

    def test_prerequisite_cannot_trust_false_pass_status(self):
        result = {"status": "implemented_subset_passed", "failure": "", "checks": []}
        with self.assertRaises(gate.GateFailure):
            gate.prerequisite_evidence(result)

    def test_rpc_failure_does_not_expose_payload_or_text(self):
        client = object.__new__(gate.MetadataClient)
        client.index = 0
        client.process = type("Process", (), {"stdin": io.StringIO()})()
        private = "DO NOT SAVE DISPOSABLE INPUT"
        client.receive = lambda timeout: {"id": 999, "text": private}
        report = {}
        try:
            client.call("state")
        except gate.GateFailure as exc:
            gate.record_failure(report, exc, "rpc")
        self.assertNotIn(private, json.dumps(report))
        self.assertEqual(report["failures"][0]["code"], "rpc_identity_mismatch")

    def test_failed_pass_still_records_release_detach_unload(self):
        host, fixture = CleanupHost(), CleanupHost()
        report = {"failure": "GateFailure: phrase mismatch"}
        with patch.object(gate, "require_unloaded", return_value=True) as unloaded:
            gate.cleanup_native(host, fixture, report)
        unloaded.assert_called_once_with(fixture.process.pid)
        self.assertEqual(host.calls, ["release", "state", "detach"])
        self.assertEqual(fixture.process.poll(), None)
        self.assertTrue(all(report["lifecycle"][key] for key in
                            ("released", "detached", "hooksRemoved", "independentModuleAbsent", "hostExited")))
        self.assertEqual(report["failure"], "GateFailure: phrase mismatch")

    def test_release_failure_does_not_skip_detach_or_independent_inspection(self):
        host, fixture = CleanupHost(fail_release=True), CleanupHost()
        report = {}
        with patch.object(gate, "require_unloaded", return_value=True):
            gate.cleanup_native(host, fixture, report)
        self.assertEqual(host.calls, ["release", "detach"])
        self.assertEqual(report["failures"][0]["stage"], "native_release")
        self.assertTrue(report["lifecycle"]["detached"])
        self.assertTrue(report["lifecycle"]["independentModuleAbsent"])

    def test_exited_target_cannot_prove_module_unload(self):
        host, fixture = CleanupHost(), CleanupHost()
        fixture.process.exited = True
        report = {}
        with patch.object(gate, "require_unloaded") as unloaded:
            gate.cleanup_native(host, fixture, report)
        unloaded.assert_not_called()
        self.assertFalse(report["lifecycle"]["independentModuleAbsent"])

    def test_reaction_motion_excluded_only_before_fixed_window(self):
        samples = cursor_samples()
        for sample in samples:
            if 100 <= sample["t"] < 102:
                sample["cursor"] = [int(sample["t"]*100), 50]
        result = gate.stillness_evidence(samples, 100, 90, cursor_action_times())
        self.assertTrue(result["verified"])
        self.assertGreater(result["settlingCursorPositions"], 1)
        self.assertEqual(result["cursorPositions"], 1)
        self.assertEqual((result["measurementStartSeconds"], result["measurementEndSeconds"]), (12, 16))

    def test_any_measured_motion_refuses_pass_without_quiet_window_search(self):
        samples = cursor_samples()
        # A single excursion then nearly four seconds quiet still cannot pass.
        next(sample for sample in samples if sample["t"] == 102.02)["cursor"] = [99, 80]
        result = gate.stillness_evidence(samples, 100, 90, cursor_action_times())
        self.assertFalse(result["verified"])
        self.assertEqual(result["cursorPositions"], 2)
        self.assertEqual(len(result["cursorTransitions"]), 3)
        self.assertIn("Inconclusive", result["attribution"])

    def test_stillness_window_uses_acknowledgement_not_nominal_phase_second(self):
        samples = cursor_samples()
        next(sample for sample in samples if sample["t"] == 102.1)["cursor"] = [99, 80]
        self.assertFalse(gate.stillness_evidence(samples, 100, 90, cursor_action_times())["verified"])
        self.assertTrue(gate.stillness_evidence(samples, 100.4, 90, cursor_action_times())["verified"])

    def test_stillness_requires_valid_dense_sampling_and_background_actions(self):
        samples = cursor_samples()
        sparse = samples[::25]
        self.assertFalse(gate.stillness_evidence(sparse, 100, 90, cursor_action_times())["verified"])
        self.assertFalse(gate.stillness_evidence(samples, 100, 90, [])["verified"])
        next(sample for sample in samples if sample["t"] == 103)["cursorQuery"] = False
        self.assertFalse(gate.stillness_evidence(samples, 100, 90, cursor_action_times())["verified"])


def cursor_samples():
    return [{"t": 99+i/50, "cursor": [20, 30], "cursorQuery": True} for i in range(500)]


def cursor_action_times():
    return [102.01+i/10 for i in range(45)]


class CleanupProcess:
    def __init__(self):
        self.exited, self.pid = False, 123
    def poll(self):
        return 0 if self.exited else None


class CleanupHost:
    def __init__(self, fail_release=False):
        self.process, self.calls, self.fail_release = CleanupProcess(), [], fail_release
    def call(self, op):
        self.calls.append(op)
        if op == "release" and self.fail_release:
            raise gate.GateFailure("fixture_release_failed", "Fixture release failure")
        return {"state": {"heldKeys": [], "heldButtons": [], "virtualCaptureHwnd": "0"},
                "detach": {"detached": True, "hooksRemoved": True, "moduleUnloaded": True}}.get(op, {})
    def eof(self):
        self.process.exited = True
    def close(self):
        self.process.exited = True


if __name__ == "__main__":
    unittest.main()
