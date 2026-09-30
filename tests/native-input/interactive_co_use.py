"""Explicit, disposable, two-minute user co-use gate. Never injects physical input.

Run only after the user agrees to type in the visible owned test pad. Three complete
automated native acceptance runs must pass before the interactive windows open.
"""
from __future__ import annotations

import argparse
import ctypes as C
import json
import math
import os
import pathlib
import subprocess
import sys
import time
import traceback

from acceptance import Client, GUI, Monitor, POINT, RECT, ROOT, REPO, U, owner
from acceptance import injected_module_loaded, require_unloaded

PASSAGE = "background control stays separate"
DURATION = 60
STILL_SETTLE_SECONDS = 2.0
STILL_MEASURE_SECONDS = 4.0

PREREQUISITE_CHECKS = (
    "native_hover_changes_actual_target_state", "left_click_changes_counter",
    "double_click_changes_counter", "right_click_changes_counter", "wheel_changes_target_scroll_state",
    "drag_changes_target_position_and_releases", "modifier_chord_changes_app_state",
    "unicode_text_reaches_native_edit", "same_process_canary_and_separate_pad_isolation",
    "covered_target_still_receives_scoped_action", "release_is_idempotent_while_keys_and_button_held",
    "detach_reports_removed_hooks_and_exits", "eof_while_modifier_held_exits",
    "out_of_band_revocation_releases_target_state_and_detaches",
    "controlling_owner_process_loss_releases_and_unloads", "target_close_then_host_eof_exits",
)


class GateFailure(AssertionError):
    def __init__(self, code, message, **evidence):
        super().__init__(message)
        self.code, self.evidence = code, evidence


def require(condition, code, message, **evidence):
    # Explicit checks continue working under python -O. Evidence is selected numeric
    # metadata only; never pass fixture state, pad text, RPC payloads or local variables.
    if not condition:
        raise GateFailure(code, message, **evidence)


def record_failure(report, exc, stage):
    message = str(exc) or "(exception supplied no message)"
    detail = {"type": type(exc).__name__, "message": message, "stage": stage,
              "traceback": [f"{frame.filename}:{frame.lineno} in {frame.name}"
                            for frame in traceback.extract_tb(exc.__traceback__)]}
    # No source lines/locals: instructions contain the disposable test phrase.
    if isinstance(exc, GateFailure):
        detail.update(code=exc.code, evidence=exc.evidence)
    report.setdefault("failures", []).append(detail)
    report.setdefault("failure", f"{detail['type']}: {message}")
    if not report["failure"]:
        report["failure"] = f"{detail['type']}: {message}"
    for item in report.get("passes", []):
        if item.get("status") == "running":
            item.update(status="failed", failureCode=detail.get("code", detail["type"]))


def prerequisite_evidence(result):
    checks = result.get("checks", [])
    by_name = {c.get("name"): c for c in checks}
    require("failure" not in result and not result.get("monitorCleanupError"),
            "prerequisite_failure_present", "Automated prerequisite contains a recorded failure")
    require(result.get("status") == "implemented_subset_passed" and
            set(by_name) == set(PREREQUISITE_CHECKS) and len(checks) == len(PREREQUISITE_CHECKS) and
            all(c.get("status") == "passed" for c in checks),
            "prerequisite_incomplete", "Automated prerequisite did not pass every required check",
            checkCount=len(checks), passedCount=sum(c.get("status") == "passed" for c in checks))
    evidence = lambda name: by_name[name].get("evidence", {})
    detach = evidence("detach_reports_removed_hooks_and_exits")
    eof = evidence("eof_while_modifier_held_exits")
    revoke = evidence("out_of_band_revocation_releases_target_state_and_detaches")
    loss = evidence("controlling_owner_process_loss_releases_and_unloads")
    require(detach.get("independentModuleAbsent") is True and
            detach.get("reported", {}).get("hooksRemoved") is True and
            eof.get("independentModuleAbsent") is True and eof.get("targetReceivedShiftRelease") is True and
            revoke.get("independentModuleAbsent") is True and revoke.get("targetReceivedShiftRelease") is True and
            revoke.get("targetDragEnded") is True and loss.get("moduleAbsent") is True and
            loss.get("hostExited") is True and loss.get("targetReceivedShiftRelease") is True,
            "prerequisite_lifecycle_missing", "Automated prerequisite lacks required release/unload evidence")
    require(bool(result.get("processes")) and all(p.get("exited") is True for p in result["processes"]),
            "prerequisite_process_survived", "Automated prerequisite did not verify all owned processes exited")
    return {"requiredChecksPassed": True, "lifecycleVerified": True, "checkNames": list(PREREQUISITE_CHECKS)}


def stillness_evidence(samples, notice_at, pass_started, action_times):
    """One predeclared window after prompt acknowledgement, never a quiet-window search."""
    if notice_at is None:
        return {"verified": False, "reason": "stillness_prompt_not_acknowledged"}
    measured_from = notice_at + STILL_SETTLE_SECONDS
    measured_until = measured_from + STILL_MEASURE_SECONDS
    selected = [sample for sample in samples if measured_from <= sample["t"] < measured_until]
    settling = [sample for sample in samples if notice_at <= sample["t"] < measured_from]
    times = [sample["t"] for sample in selected]
    positions = {tuple(sample["cursor"]) for sample in selected}
    observed_span = times[-1] - times[0] if times else 0
    boundaries = [measured_from, *times, measured_until]
    max_gap = max(b-a for a, b in zip(boundaries, boundaries[1:]))
    actions = sum(measured_from <= t < measured_until for t in action_times)
    transitions = []
    previous = None
    for sample in selected:
        position = tuple(sample["cursor"])
        if position != previous:
            transitions.append({"passSeconds": round(sample["t"]-pass_started, 4),
                                "cursor": list(position)})
            previous = position
    queries_valid = bool(selected) and all(sample.get("cursorQuery") is True for sample in selected)
    verified = (queries_valid and len(selected) >= 20 and len(positions) == 1 and observed_span >= 3.5 and
                max_gap <= .25 and actions >= 11)
    return {"verified": verified, "promptAcknowledgedAtSeconds": notice_at-pass_started,
            "settleSeconds": STILL_SETTLE_SECONDS, "measurementSeconds": STILL_MEASURE_SECONDS,
            "measurementStartSeconds": measured_from-pass_started, "measurementEndSeconds": measured_until-pass_started,
            "samples": len(selected), "cursorPositions": len(positions), "observedSeconds": observed_span,
            "allCursorQueriesSucceeded": queries_valid,
            "maxSampleGapSeconds": max_gap, "backgroundActions": actions,
            "settlingSamples": len(settling), "settlingCursorPositions": len({tuple(s["cursor"]) for s in settling}),
            "cursorTransitions": transitions,
            "attribution": "Stable measured cursor" if verified else
                "Inconclusive: physical movement, controller movement, or missing sampling cannot be distinguished by cursor positions alone"}


def completion_errors(report):
    errors = []
    if "failure" in report or report.get("failures"):
        errors.append("recorded_exception_or_failure")
    runs = report.get("automatedRuns", [])
    if len(runs) != 3 or any(r.get("run") != i or r.get("passed") is not True or
                             r.get("requiredChecksPassed") is not True or r.get("lifecycleVerified") is not True
                             for i, r in enumerate(runs, 1)):
        errors.append("three_complete_prerequisites_required")
    passes = report.get("passes", [])
    if len(passes) != 2:
        errors.append("exactly_two_passes_required")
    for index, item in enumerate(passes, 1):
        valid = (item.get("pass") == index and item.get("status") == "passed" and
                 item.get("covered") is (index == 2) and item.get("observedSeconds", 0) >= DURATION and
                 item.get("actions", 0) >= 100 and item.get("verifiedActionCycles", 0) >= 1 and
                 item.get("interleavedTypingCycles", 0) >= 3 and item.get("physicalCtrlCycles", 0) >= 3 and
                 item.get("physicalShiftCycles", 0) >= 3 and item.get("stableCanaryComparisons", 0) >= 10 and
                 item.get("stableCanaryFocusComparisons", 0) >= 10 and
                 item.get("exactDisposablePassage") is True and item.get("physicalCursorPositions", 0) >= 3 and
                 item.get("handsStillSamples", 0) >= 20 and item.get("handsStillCursorPositions") == 1 and
                 item.get("handsStillEvidence", {}).get("verified") is True and
                 item.get("coverVerifiedAtFivePoints" if index == 2 else "visibleVerifiedAtFivePoints") is True)
        if not valid:
            errors.append(f"pass_{index}_incomplete")
    lifecycle = report.get("lifecycle", {})
    if any(lifecycle.get(key) is not True for key in
           ("released", "detached", "hooksRemoved", "independentModuleAbsent", "hostExited")):
        errors.append("release_detach_unload_evidence_required")
    if report.get("allOwnedProcessesExited") is not True:
        errors.append("owned_process_cleanup_unverified")
    if report.get("monitorStarted") is not True or report.get("monitorStopped") is not True or report.get("metadataSampleCount", 0) <= 0:
        errors.append("metadata_monitor_incomplete")
    if report.get("targetFocusOrCaptureEvents") != [] or report.get("targetForegroundSamples") != 0 or report.get("targetFocusSamples") != 0:
        errors.append("real_focus_isolation_unverified_or_violated")
    return errors


def finalize_report(report):
    report["completionErrors"] = completion_errors(report)
    report["status"] = "failed" if report["completionErrors"] else "owned_fixture_co_use_passed"
    # This owned-fixture gate never establishes full application/input coverage.
    report["fullCoveragePassed"] = False
    return 0 if report["status"] == "owned_fixture_co_use_passed" else 1


def cleanup_native(host, fixture, report):
    """Retain separate acknowledgements and independent proof even after a failed pass."""
    if host is None:
        return
    lifecycle = report.setdefault("lifecycle", {})
    if host.process.poll() is None:
        try:
            host.call("release")
            lifecycle["releaseAcknowledged"] = True
            released = host.call("state")
            lifecycle["heldKeyCountAfterRelease"] = len(released.get("heldKeys", []))
            lifecycle["heldButtonCountAfterRelease"] = len(released.get("heldButtons", []))
            lifecycle["captureHwndAfterRelease"] = int(released.get("virtualCaptureHwnd", -1))
            lifecycle["released"] = (released.get("heldKeys") == [] and released.get("heldButtons") == [] and
                                     lifecycle["captureHwndAfterRelease"] == 0)
            require(lifecycle["released"], "release_incomplete", "Native helper retained or did not report virtual held input after release")
        except BaseException as exc:
            record_failure(report, exc, "native_release")
        # Detach must still be attempted if release/state failed.
        try:
            detached = host.call("detach")
            lifecycle["detached"] = detached.get("detached") is True
            lifecycle["hooksRemoved"] = detached.get("hooksRemoved") is True
            lifecycle["detachAcknowledgement"] = {key: detached.get(key) is True for key in
                                                  ("detached", "hooksRemoved", "moduleUnloaded")}
            require(lifecycle["detached"] and lifecycle["hooksRemoved"],
                    "detach_unverified", "Native helper did not confirm detach and hook removal")
        except BaseException as exc:
            record_failure(report, exc, "native_detach")
        try:
            host.eof()
        except BaseException as exc:
            record_failure(report, exc, "native_eof")
    try:
        host.close()
    except BaseException as exc:
        record_failure(report, exc, "native_process_cleanup")
    lifecycle["hostExited"] = host.process.poll() is not None
    # Inspect while the target remains alive: process exit is not independent DLL
    # unload evidence, and must not be substituted for it.
    if fixture is not None and fixture.process.poll() is None:
        try:
            lifecycle["independentModuleAbsent"] = require_unloaded(fixture.process.pid) is True
            report["independentModuleAbsent"] = lifecycle["independentModuleAbsent"]
        except BaseException as exc:
            lifecycle["independentModuleAbsent"] = False
            record_failure(report, exc, "independent_module_inspection")
    else:
        lifecycle["independentModuleAbsent"] = False
        lifecycle["moduleInspectionUnavailable"] = "Owned target already exited before independent module inspection"


class MetadataClient(Client):
    def call(self, op, timeout=3, **args):
        # The shared Client includes response repr() in protocol errors. A malformed
        # pad/fixture response could contain text, so reject it without logging payloads.
        self.index += 1
        self.process.stdin.write(json.dumps({"id": self.index, "op": op, **args}, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        response = self.receive(timeout)
        require(isinstance(response, dict) and response.get("id") == self.index,
                "rpc_identity_mismatch", "Owned RPC response did not match its request", requestId=self.index)
        require("error" not in response, "rpc_remote_error", "Owned RPC returned an error; payload omitted for privacy", requestId=self.index)
        return response.get("result", response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interactive", action="store_true",
                        help="Required: the user is ready for two 60-second co-use passes.")
    parser.add_argument("--host", type=pathlib.Path,
                        default=REPO / "native/bin/x64/BetterWinControl.NativeHost.exe")
    parser.add_argument("--result", type=pathlib.Path, default=ROOT / "results-interactive.json")
    args = parser.parse_args()
    if not args.interactive:
        parser.error("Requires explicit --interactive and a coordinated user typing test.")

    report = {
        "scope": "Owned ordinary Win32 target and dedicated user pad on the existing desktop",
        "startedAt": time.time(), "automatedRuns": [], "passes": [],
        "fullCoveragePassed": False,
        "privacy": "Only local exact-match results and counts for the known disposable passage are saved. "
                   "No pad contents or global user keystrokes are recorded.",
        "limitations": ["Does not exercise the PiP or MCP integration.",
                        "Does not establish arbitrary application compatibility.",
                        "Cursor movement during user activity cannot by itself identify its origin."],
    }
    clients = []
    host = fixture = pad = None
    monitor = Monitor()
    try:
        # Required repeated gate, including unload/owner-loss recovery, before co-use.
        for index in range(1, 4):
            result_path = ROOT / f"results-interactive-prerequisite-{index}.json"
            run = subprocess.run([sys.executable, "-B", str(ROOT / "acceptance.py"),
                                  "--host", str(args.host), "--result", str(result_path)],
                                 capture_output=True, text=True, encoding="utf-8", timeout=25,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
            if run.returncode:
                raise AssertionError(f"Automated prerequisite {index} failed; inspect {result_path.name}")
            result = json.loads(result_path.read_text(encoding="utf-8"))
            verified = prerequisite_evidence(result)
            report["automatedRuns"].append({"run": index, "checks": len(result["checks"]),
                                             "elapsedMs": result["elapsedMs"], "passed": True,
                                             "hostSha256": result["hostSha256"], **verified})
            print(json.dumps({"automatedPrerequisitePassed": index}), flush=True)

        executable = ROOT / "bin/Release/net10.0-windows/Fixture.exe"
        fixture = MetadataClient([str(executable), "--watchdog-seconds", "240"])
        clients.append(fixture)
        ready = fixture.receive()
        target = int(ready["target"])
        require(ready["ready"] and ready["pid"] == fixture.process.pid,
                "fixture_identity", "Target fixture readiness/PID did not match its launched process")
        require(owner(target) == fixture.process.pid, "fixture_hwnd_owner", "Target HWND owner does not match fixture PID",
                hwnd=target, actualPid=owner(target), expectedPid=fixture.process.pid)
        pad = MetadataClient([str(executable), "--interactive-pad", "--watchdog-seconds", "240"])
        clients.append(pad)
        pad_ready = pad.receive()
        require(pad_ready["ready"] and pad_ready["pid"] == pad.process.pid,
                "pad_identity", "Pad readiness/PID did not match its launched process")
        require(owner(int(pad_ready["edit"])) == pad.process.pid,
                "pad_editor_owner", "Pad editor HWND does not belong to the launched pad")

        def instructions(text, title):
            pad.call("instructions", text=text, title=title)

        def pad_status():
            return pad.call("pad_status", expected=PASSAGE)

        instructions(
            "Two 60-second passes: the target is visible, then covered.\r\n\r\n"
            "Click the empty editor below to begin. This pad is disposable.\r\n"
            "Type only the displayed test phrase, move your mouse inside this pad, and follow the timed Ctrl/Shift prompts.\r\n"
            "Do not click the background target or its canary. No passwords or private text.",
            "Background control test: click the editor to begin")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            state = pad_status()
            if state["editorFocused"] and state["foreground"]:
                break
            time.sleep(.1)
        else:
            raise AssertionError("User did not focus the disposable pad within 60 seconds; no interactive pass ran")

        monitor.start()
        report["monitorStarted"] = True
        host = MetadataClient([str(args.host), "--hwnd", str(target), "--owner-pid", str(os.getpid())])
        clients.append(host)
        capabilities = host.call("capabilities")
        require(injected_module_loaded(fixture.process.pid), "module_not_loaded", "Native input module did not load into the owned target")
        report["capabilities"] = capabilities

        for pass_index, covered in enumerate((False, True), 1):
            bounds = RECT()
            require(U.GetWindowRect(target, C.byref(bounds)), "target_bounds_query", "GetWindowRect failed for the owned target",
                    hwnd=target, win32Error=C.get_last_error())
            points = [(bounds.left + 8, bounds.top + 8), (bounds.right - 8, bounds.top + 8),
                      (bounds.left + 8, bounds.bottom - 8), (bounds.right - 8, bounds.bottom - 8),
                      ((bounds.left + bounds.right) // 2, (bounds.top + bounds.bottom) // 2)]
            if covered:
                cover = fixture.call("cover")
                time.sleep(.05)
                observed = [int(U.WindowFromPoint(POINT(x, y)) or 0) for x, y in points]
                require(all(hwnd == cover["cover"] for hwnd in observed), "target_not_covered",
                        "Owned cover did not cover the target at all five test points", observedHwnds=observed, coverHwnd=cover["cover"])
            else:
                observed = [owner(U.WindowFromPoint(POINT(x, y))) for x, y in points]
                require(all(pid == fixture.process.pid for pid in observed), "visible_target_covered",
                        "Visible pass target is already covered", observedPids=observed, targetPid=fixture.process.pid)
            pad.call("reset")  # Discards only the dedicated, known test passage between passes.
            before = fixture.call("state")
            started = time.monotonic()
            phase = None
            previous_second = -1
            previous_pad_keys = 0
            actions = cycles = interleaved_typing = canary_checks = canary_focus_checks = 0
            physical_ctrl_cycles = physical_shift_cycles = 0
            still_notice_at = None
            action_times = []
            for_pass = {"pass": pass_index, "covered": covered, "durationSeconds": DURATION,
                        "status": "running", "coverVerifiedAtFivePoints": covered,
                        "visibleVerifiedAtFivePoints": not covered,
                        "canaryInputBaseline": before["canaryInputs"], "phaseTransitions": [],
                        "canaryBaselineNote": "Pre-pass input excluded; any new canary input makes this pass fail or inconclusive, never a pass."}
            report["passes"].append(for_pass)
            while time.monotonic() - started < DURATION:
                elapsed = time.monotonic() - started
                second = int(elapsed)
                if second < 12:
                    new_phase = "typing"
                    prompt = f"Type this phrase exactly (no Enter):\r\n{PASSAGE}\r\nMove your mouse inside this pad while you type."
                elif second < 15:
                    new_phase = "prepare_hands_still"
                    prompt = f"Prepare to stop moving in {15-second} seconds.\r\nFinish your current key or mouse movement.\r\nNext, keep both hands still until the CTRL prompt."
                elif still_notice_at is None or time.monotonic() < still_notice_at + STILL_SETTLE_SECONDS + STILL_MEASURE_SECONDS:
                    new_phase = "hands_still"
                    still_age = 0 if still_notice_at is None else time.monotonic() - still_notice_at
                    if still_age < STILL_SETTLE_SECONDS:
                        prompt = f"HANDS OFF NOW — {math.ceil(STILL_SETTLE_SECONDS-still_age)}s to settle.\r\nRelease all keys. Stop moving the mouse.\r\nKeep both hands still until the CTRL prompt."
                    else:
                        remaining = max(1, math.ceil(STILL_SETTLE_SECONDS+STILL_MEASURE_SECONDS-still_age))
                        prompt = f"KEEP STILL — measuring: {remaining}s left.\r\nDo not touch the keyboard or mouse yet.\r\nWait for the CTRL prompt before moving again."
                elif second < 30:
                    new_phase = "physical_ctrl"
                    prompt = "Hold the physical CTRL key now; do not type.\r\nMove your mouse inside this pad.\r\nKeep holding Ctrl until the prompt changes."
                elif second < 40:
                    new_phase = "physical_shift"
                    prompt = "Release CTRL. Hold the physical SHIFT key now; do not type.\r\nMove your mouse inside this pad.\r\nKeep holding Shift until the prompt changes."
                else:
                    new_phase = "finish_typing"
                    prompt = f"Release all modifiers. Correct the phrase below if necessary:\r\n{PASSAGE}\r\nNo Enter or extra spaces. Move your mouse inside this pad."
                if second != previous_second or new_phase != phase:
                    instructions(f"PASS {pass_index}/2 — TARGET {'COVERED' if covered else 'VISIBLE'} — {60-second}s remaining\r\n\r\n{prompt}",
                                 f"Co-use pass {pass_index}/2: {new_phase.replace('_', ' ')} ({60-second}s)")
                    prompt_acknowledged = time.monotonic()
                    if new_phase == "hands_still" and still_notice_at is None:
                        still_notice_at = prompt_acknowledged
                    previous_second = second
                if new_phase != phase:
                    phase = new_phase
                    for_pass["phaseTransitions"].append({"phase": phase, "promptAcknowledgedAtSeconds": prompt_acknowledged-started})
                    monitor.phase = f"interactive_{pass_index}_{phase}"
                    print(json.dumps({"pass": pass_index, "phase": phase}), flush=True)

                def action(op, **fields):
                    nonlocal actions
                    result = host.call(op, **fields)
                    actions += 1
                    action_times.append(time.monotonic())
                    return result

                action("move", x=190, y=180)
                action("move", x=40, y=40)
                action("button", button="left", down=True)
                action("button", button="left", down=False)
                action("wheel", delta=120)
                action("key", vk=0x11, down=True)
                action("key", vk=0x4B, down=True)
                action("key", vk=0x4B, down=False)
                action("key", vk=0x11, down=False)
                action("key", vk=0x77, down=True)
                action("key", vk=0x77, down=False)
                current = fixture.call("state")
                cycles += 1
                diagnostic = {key: current[key] for key in ("clicks", "wheelSteps", "chordActions", "plainKeyActions", "hovered", "canaryInputs")}
                diagnostic.update(cycle=cycles, actions=actions, elapsedSeconds=round(elapsed, 3), phase=phase,
                                  canaryInputBaseline=before["canaryInputs"], canaryInputDelta=current["canaryInputs"]-before["canaryInputs"])
                for_pass["lastCycle"] = diagnostic
                for_pass.update(actions=actions, verifiedActionCycles=cycles-1, observedSeconds=time.monotonic()-started)
                for key, code in (("clicks", "target_click_missing"), ("wheelSteps", "target_wheel_missing"),
                                  ("chordActions", "target_chord_missing"), ("plainKeyActions", "target_plain_key_contaminated")):
                    require(current[key] == before[key] + cycles, code, f"Target {key} count does not match completed action cycles",
                            actual=current[key], expected=before[key]+cycles, cycle=cycles)
                require(current["hovered"], "target_hover_missing", "Target hover state is false after its hover action", **diagnostic)
                require(current["canaryInputs"] == before["canaryInputs"], "new_canary_input",
                        "Same-process canary received new input during the pass; physical pointer contact or virtual leakage must be distinguished before retry", **diagnostic)
                chord = next((k for k in reversed(current["keyObservations"]) if k.get("vk") == 0x4B and k.get("down")), None)
                require(chord is not None, "target_chord_observation_missing", "Target did not record its virtual Ctrl+K key-down")
                diagnostic["chordModifiers"] = {key: chord[key] for key in ("control", "asyncControl", "shift", "asyncShift", "alt")}
                require(chord["control"] and chord["asyncControl"], "virtual_ctrl_missing", "Virtual Ctrl absent inside target shortcut", **diagnostic["chordModifiers"])
                require(not chord["shift"] and not chord["asyncShift"] and not chord["alt"], "virtual_chord_contaminated", "Physical modifier contaminated virtual target shortcut", **diagnostic["chordModifiers"])
                plain_key = next((k for k in reversed(current["keyObservations"]) if k.get("vk") == 0x77 and k.get("down")), None)
                require(plain_key is not None, "plain_key_observation_missing", "Target did not record its unmodified F8 key-down")
                diagnostic["plainKeyModifiers"] = {key: plain_key[key] for key in ("control", "shift", "alt", "asyncControl", "asyncShift")}
                require(not any(diagnostic["plainKeyModifiers"].values()), "plain_key_modifier_leak", "Physical modifier leaked into unmodified virtual input", **diagnostic["plainKeyModifiers"])

                def physical_focus():
                    gui = GUI(size=C.sizeof(GUI))
                    tid = U.GetWindowThreadProcessId(target, None)
                    ok = bool(U.GetGUIThreadInfo(tid, C.byref(gui)))
                    require(ok, "gui_thread_query_failed", "GetGUIThreadInfo failed for the target thread",
                            threadId=tid, hwnd=target, win32Error=C.get_last_error())
                    return (int(gui.focus or 0), int(gui.capture or 0))

                focus_before = physical_focus()
                physical_before = tuple(bool(U.GetAsyncKeyState(v) & 0x8000) for v in (0x11, 0x10))
                canary = fixture.call("canary_probe")
                physical_after = tuple(bool(U.GetAsyncKeyState(v) & 0x8000) for v in (0x11, 0x10))
                focus_after = physical_focus()
                diagnostic["canaryProbe"] = {key: canary[key] for key in ("asyncControlDown", "asyncShiftDown", "focus", "capture")}
                diagnostic.update(physicalModifiersBefore=physical_before, physicalModifiersAfter=physical_after,
                                  physicalFocusCaptureBefore=focus_before, physicalFocusCaptureAfter=focus_after)
                if physical_before == physical_after:
                    require((canary["asyncControlDown"], canary["asyncShiftDown"]) == physical_after,
                            "canary_modifier_leak", "Virtual modifier leaked into canary during a stable physical bracket", **diagnostic)
                    canary_checks += 1
                    if phase == "physical_ctrl" and physical_after[0]: physical_ctrl_cycles += 1
                    if phase == "physical_shift" and physical_after[1]: physical_shift_cycles += 1
                if focus_before == focus_after:
                    require((canary["focus"], canary["capture"]) == focus_after,
                            "canary_focus_capture_leak", "Canary focus/capture APIs differ from real target-thread state during a stable bracket", **diagnostic)
                    canary_focus_checks += 1
                state = pad_status()
                diagnostic["padFocus"] = {key: state[key] for key in ("editorFocused", "foreground", "editKeys")}
                require(state["editorFocused"] and state["foreground"], "pad_lost_focus", "Disposable pad lost real focus during target action", **diagnostic["padFocus"])
                if state["editKeys"] > previous_pad_keys:
                    interleaved_typing += 1
                previous_pad_keys = state["editKeys"]
                for_pass.update(verifiedActionCycles=cycles, interleavedTypingCycles=interleaved_typing,
                                physicalCtrlCycles=physical_ctrl_cycles, physicalShiftCycles=physical_shift_cycles,
                                stableCanaryComparisons=canary_checks, stableCanaryFocusComparisons=canary_focus_checks,
                                padKeyMessageCount=state["editKeys"])
                time.sleep(.03)

            ended = time.monotonic()
            final_pad = pad_status()
            samples = [s for s in monitor.samples if started <= s["t"] <= ended]
            stillness = stillness_evidence(samples, still_notice_at, started, action_times)
            # Save all measured metadata BEFORE validation; a phrase mismatch must
            # not discard modifier/interleaving/isolation evidence already gathered.
            for_pass.update(actions=actions, verifiedActionCycles=cycles,
                            interleavedTypingCycles=interleaved_typing, physicalCtrlCycles=physical_ctrl_cycles,
                            physicalShiftCycles=physical_shift_cycles, stableCanaryComparisons=canary_checks,
                            stableCanaryFocusComparisons=canary_focus_checks,
                            exactDisposablePassage=bool(final_pad["exactPassage"]), padKeyMessageCount=final_pad["editKeys"],
                            physicalCursorPositions=len({tuple(s["cursor"]) for s in samples}),
                            handsStillCursorPositions=stillness.get("cursorPositions", 0), handsStillSamples=stillness.get("samples", 0),
                            handsStillEvidence=stillness,
                            observedSeconds=ended-started)
            require(ended-started >= DURATION, "duration_short", "Pass lasted less than 60 seconds", observedSeconds=ended-started)
            require(actions >= 100, "action_count_short", "Fewer than 100 background actions in a pass", actions=actions)
            require(final_pad["exactPassage"], "passage_mismatch", "Dedicated disposable phrase did not match exactly; contents were not saved")
            require(interleaved_typing >= 3, "typing_not_interleaved", "Insufficient observed interleaving of user typing and background action cycles", cycles=interleaved_typing)
            require(physical_ctrl_cycles >= 3 and physical_shift_cycles >= 3, "modifier_co_use_missing", "Physical Ctrl/Shift holds were not observed sufficiently", ctrlCycles=physical_ctrl_cycles, shiftCycles=physical_shift_cycles)
            require(canary_checks >= 10 and canary_focus_checks >= 10, "canary_comparisons_short", "Insufficient stable same-process canary comparisons", modifierComparisons=canary_checks, focusComparisons=canary_focus_checks)
            require(len({tuple(s["cursor"]) for s in samples}) >= 3, "physical_mouse_not_observed", "Physical mouse movement was not observed")
            require(stillness["verified"], "hands_still_inconclusive", "No stable cursor evidence in the fixed measured interval; this does not establish the source of motion", **stillness)
            for_pass["status"] = "passed"
            print(json.dumps({"pass": pass_index, "status": "passed", "actions": actions}), flush=True)

        instructions("Both timed passes completed. Finishing isolation and cleanup checks.\r\nThe disposable test pad will close. No typed contents were saved.",
                     "Background control test: final verification")
    except BaseException as exc:
        record_failure(report, exc, "interactive_gate")
        print(json.dumps({"status": "failed", "failure": report["failure"], "detail": report["failures"][-1]}), flush=True)
    finally:
        cleanup_native(host, fixture, report)
        if monitor.ready.is_set():
            try:
                monitor.stop()
                report["monitorStopped"] = True
            except BaseException as exc:
                record_failure(report, exc, "metadata_monitor_cleanup")
        for client in reversed(clients):
            if client is not host and client.process.poll() is None:
                try:
                    client.call("shutdown", timeout=1)
                except BaseException as exc:
                    record_failure(report, exc, "fixture_shutdown")
            try:
                client.close()
            except BaseException as exc:
                record_failure(report, exc, "owned_process_cleanup")
        report["allOwnedProcessesExited"] = all(c.process.poll() is not None for c in clients)
        fixture_pid = fixture.process.pid if fixture else -1
        violations = [e for e in monitor.events if e["pid"] == fixture_pid and e["event"] in (3, 8, 0x8005)]
        report["targetFocusOrCaptureEvents"] = violations
        report["metadataSampleCount"] = len(monitor.samples)
        report["targetForegroundSamples"] = sum(s["foregroundPid"] == fixture_pid for s in monitor.samples)
        report["targetFocusSamples"] = sum(s["focusPid"] == fixture_pid for s in monitor.samples)
        if violations or report["targetForegroundSamples"] or report["targetFocusSamples"]:
            record_failure(report, GateFailure("real_focus_violation", "Background target acquired real focus/capture"), "final_isolation")
        if not report["allOwnedProcessesExited"]:
            record_failure(report, GateFailure("process_survived", "Owned process survived cleanup"), "final_cleanup")
        finalize_report(report)
        args.result.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "result": str(args.result)}), flush=True)
    return 0 if report["status"] == "owned_fixture_co_use_passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
