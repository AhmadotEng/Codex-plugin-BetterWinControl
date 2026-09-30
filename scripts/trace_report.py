"""Build a local, inert HTML timeline and JSON summary from a BWC trace capsule.

Usage: python scripts/trace_report.py [CAPSULE_DIRECTORY]
With no directory, selects the latest capsule under runtime/traces. This reads
only the selected capsule; it never reads Codex conversations or live apps.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import html
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1
DISPLAY_LIMIT = 40000
REPORT_LOCK = threading.RLock()


def number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def utc_valid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.utcoffset() is not None and stamp.utcoffset().total_seconds() == 0
    except (ValueError, TypeError, OverflowError):
        return False


def read_events(capsule: Path) -> tuple[list[dict], list[dict], list[str]]:
    """Validate framing, ordering and required event fields, preserving good rows."""
    events, issues, filenames = [], [], []
    previous_seq = None
    run_id = None
    previous_time = None
    for path in sorted(capsule.glob("events*.jsonl")):
        filenames.append(path.name)
        # Never follow an attacker-supplied artifact or trace symlink outside the capsule.
        if not path.resolve().is_relative_to(capsule.resolve()):
            issues.append({"kind": "unsafe_trace_path", "file": path.name})
            continue
        try:
            raw = path.read_bytes()
        except OSError as exc:
            issues.append({"kind": "unreadable_trace", "file": path.name, "error": type(exc).__name__})
            continue
        lines = raw.splitlines()
        for line_no, raw_line in enumerate(lines, 1):
            location = {"file": path.name, "line": line_no}
            if not raw_line.strip():
                issues.append({"kind": "empty_record", **location})
                continue
            try:
                event = json.loads(raw_line)
            except (ValueError, UnicodeError):
                issues.append({"kind": "malformed_record", **location})
                continue
            if (not isinstance(event, dict) or event.get("schemaVersion") != SCHEMA_VERSION
                    or not isinstance(event.get("runId"), str) or not event["runId"]
                    or type(event.get("seq")) is not int or event["seq"] < 1
                    or not utc_valid(event.get("timestampUtc"))
                    or not number(event.get("monotonicMs")) or event["monotonicMs"] < 0
                    or not isinstance(event.get("event"), str) or not event["event"]
                    or not isinstance(event.get("data"), dict)):
                issues.append({"kind": "invalid_schema", **location})
                continue
            if event["event"].startswith("tool.") and not isinstance(event.get("traceId"), str):
                issues.append({"kind": "missing_call_id", **location})
                continue
            if event["event"].startswith("stage.") and not isinstance(event.get("spanId"), str):
                issues.append({"kind": "missing_span_id", **location})
                continue
            reference = event["data"].get("payloadArtifact")
            if reference is not None:
                artifact = _artifact_path(capsule, reference)
                try:
                    expanded = json.loads(artifact.read_text(encoding="utf-8")) if artifact else None
                except (OSError, ValueError, UnicodeError):
                    expanded = None
                if not isinstance(expanded, dict):
                    issues.append({"kind": "missing_or_invalid_event_payload", **location})
                    continue
                event["data"] = expanded
                event["payloadArtifact"] = reference
            if event["event"] in ("tool.received", "tool.start", "tool.end") and not isinstance(event["data"].get("tool"), str):
                issues.append({"kind": "invalid_tool_event", **location})
                continue
            if event["event"].startswith("stage.") and not isinstance(event["data"].get("name"), str):
                issues.append({"kind": "invalid_stage_event", **location})
                continue
            if previous_seq is not None and event["seq"] != previous_seq + 1:
                issues.append({"kind": "sequence_gap_or_reorder", "previous": previous_seq, "current": event["seq"], **location})
            elif previous_seq is None and event["seq"] != 1:
                issues.append({"kind": "missing_initial_records", "firstSequence": event["seq"], **location})
            if previous_time is not None and event["monotonicMs"] < previous_time:
                issues.append({"kind": "time_reordered", **location})
            if run_id is not None and event["runId"] != run_id:
                issues.append({"kind": "mixed_runs", **location})
                continue
            run_id = event["runId"]
            previous_seq = event["seq"]
            previous_time = event["monotonicMs"]
            events.append(event)
        if raw and not raw.endswith(b"\n"):
            issues.append({"kind": "unterminated_tail", "file": path.name})
    if not filenames:
        issues.append({"kind": "no_event_files"})
    return events, issues, filenames


def union_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged = []
    for start, end in sorted(intervals):
        if end < start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def distribution(values: list[float]) -> dict:
    values = sorted(value for value in values if number(value))
    if not values:
        return {"count": 0, "sumMs": 0, "meanMs": None, "maxMs": None, "p95Ms": None}
    return {"count": len(values), "sumMs": round(sum(values), 3),
            "meanMs": round(sum(values) / len(values), 3), "maxMs": round(values[-1], 3),
            "p95Ms": round(values[max(0, math.ceil(len(values) * .95) - 1)], 3)}


def fingerprint_groups(calls: list[dict], field: str) -> list[dict]:
    groups = defaultdict(list)
    for call in calls:
        fingerprint = call.get(field)
        # Do not infer equality from redacted/truncated payloads or missing fingerprints.
        if isinstance(fingerprint, str) and fingerprint:
            groups[(call.get("tool"), call.get("operation"), fingerprint)].append(call)
    return sorted(({"tool": key[0], "operation": key[1], "fingerprint": key[2],
                    "count": len(items), "callIds": [item["callId"] for item in items]}
                   for key, items in groups.items() if len(items) > 1), key=lambda group: -group["count"])


def _nested_flag(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return value.get(key) is True or any(_nested_flag(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_nested_flag(item, key) for item in value)
    return False


def _cancelled(value: Any) -> bool:
    if isinstance(value, dict):
        for key in ("code", "error"):
            message = value.get(key)
            if isinstance(message, str) and (message in ("cancelled", "canceled", "request_cancelled_by_stop", "request_cancelled") or message.startswith("request_cancelled_by_stop:")):
                return True
        return any(_cancelled(child) for child in value.values())
    return isinstance(value, list) and any(_cancelled(child) for child in value)


def _verification(value: Any) -> list[bool]:
    results = []
    if isinstance(value, dict):
        if type(value.get("effectVerified")) is bool:
            results.append(value["effectVerified"])
        for item in value.values():
            results.extend(_verification(item))
    elif isinstance(value, list):
        for item in value:
            results.extend(_verification(item))
    return results


def _artifact_path(capsule: Path, reference: Any) -> Path | None:
    if isinstance(reference, dict):
        reference = reference.get("path") or reference.get("artifact") or reference.get("relativePath")
    if not isinstance(reference, str) or not reference:
        return None
    candidate = capsule / reference
    # Includes symlink and Windows absolute-path traversal checks.
    if candidate.is_absolute() and Path(reference).is_absolute():
        return None
    try:
        resolved = candidate.resolve()
        if not resolved.is_relative_to(capsule.resolve()) or not resolved.is_file():
            return None
    except (OSError, ValueError):
        return None
    return resolved


def payload(capsule: Path, data: dict, name: str, issues: list[dict], call_id: str) -> Any:
    """Inline v1 payloads and lossless external JSON payloads share one interface."""
    reference = data.get(name + "Artifact") or data.get(name + "Ref")
    value = data.get(name)
    if reference is None and isinstance(value, dict) and ("artifact" in value or "relativePath" in value):
        reference = value
    if reference is None:
        return value
    path = _artifact_path(capsule, reference)
    if path is None:
        issues.append({"kind": "missing_or_unsafe_payload", "callId": call_id, "field": name})
        return {"unavailableArtifact": reference}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        issues.append({"kind": "invalid_payload", "callId": call_id, "field": name})
        return {"unavailableArtifact": reference}


def analyze(capsule: Path) -> dict:
    capsule = capsule.resolve()
    events, issues, files = read_events(capsule)
    grouped = defaultdict(list)
    for event in events:
        if isinstance(event.get("traceId"), str):
            grouped[event["traceId"]].append(event)
    calls = []
    closes = [event for event in events if event["event"] == "trace.close"]
    closed = bool(closes)
    trace_end = max((event["monotonicMs"] for event in events), default=0)
    trace_begin = min((event["monotonicMs"] for event in events), default=0)
    for call_id, call_events in grouped.items():
        by_type = defaultdict(list)
        for event in call_events:
            by_type[event["event"]].append(event)
        received = by_type["tool.received"][0] if by_type["tool.received"] else None
        start = by_type["tool.start"][0] if by_type["tool.start"] else None
        end = by_type["tool.end"][0] if by_type["tool.end"] else None
        if not any((received, start, end)):
            issues.append({"kind": "orphan_stages", "callId": call_id})
            continue
        for kind in ("tool.received", "tool.start", "tool.end"):
            if len(by_type[kind]) > 1:
                issues.append({"kind": "duplicate_event", "event": kind, "callId": call_id})
        for missing, record in (("received", received), ("start", start), ("end", end)):
            expected_live = not closed and end is None and missing in ("start", "end")
            if record is None and not expected_live:
                issues.append({"kind": "missing_tool_" + missing, "callId": call_id})
        first = received or start or end
        arguments = payload(capsule, received["data"], "arguments", issues, call_id) if received else None
        result = payload(capsule, end["data"], "result", issues, call_id) if end else None
        outcome = end["data"].get("outcome", "unknown") if end else ("incomplete" if closed else ("in_progress" if start else "queued"))
        if outcome == "ok" and _nested_flag(result, "isError"):
            outcome = "error"
        if _nested_flag(result, "cancelled") or _nested_flag(result, "canceled") or _cancelled(result) or (end and _cancelled(end["data"].get("error"))):
            outcome = "cancelled"
        if outcome not in ("ok", "error", "cancelled", "canceled", "incomplete", "pending", "in_progress", "queued"):
            issues.append({"kind": "unknown_outcome", "callId": call_id})
        pending = bool(end and _nested_flag(result, "pending"))
        stages, spans = [], defaultdict(list)
        for event in call_events:
            if event["event"].startswith("stage."):
                spans[event["spanId"]].append(event)
        for span_id, span_events in spans.items():
            starts = [event for event in span_events if event["event"] == "stage.start"]
            ends = [event for event in span_events if event["event"] == "stage.end"]
            stage_start = starts[0] if starts else None
            stage_end = ends[0] if ends else None
            expected_live = not closed and end is None and len(starts) == 1 and not ends
            if (len(starts) != 1 or len(ends) != 1) and not expected_live:
                issues.append({"kind": "incomplete_or_duplicate_stage", "callId": call_id, "spanId": span_id})
            basis = stage_start or stage_end
            duration = (stage_end["monotonicMs"] - stage_start["monotonicMs"]) if stage_start and stage_end else None
            if duration is not None and duration < 0:
                issues.append({"kind": "negative_stage_duration", "callId": call_id, "spanId": span_id})
                duration = None
            reported_duration = stage_end["data"].get("durationMs") if stage_end else None
            stages.append({"spanId": span_id, "parentSpanId": basis.get("parentSpanId"),
                           "name": basis["data"].get("name", "unknown"),
                           "startMs": stage_start["monotonicMs"] if stage_start else None,
                           "endMs": stage_end["monotonicMs"] if stage_end else None,
                           "startUtc": stage_start["timestampUtc"] if stage_start else None,
                           "endUtc": stage_end["timestampUtc"] if stage_end else None,
                           "durationMs": reported_duration if duration is not None and number(reported_duration) and reported_duration >= 0 else duration,
                           "recordedIntervalMs": duration,
                           "outcome": stage_end["data"].get("outcome", "unknown") if stage_end else ("in_progress" if expected_live else "incomplete"),
                           "metadata": stage_start["data"].get("metadata") if stage_start else None,
                           "details": stage_end["data"] if stage_end else None})
        received_time = received["monotonicMs"] if received else None
        start_time = start["monotonicMs"] if start else None
        end_time = end["monotonicMs"] if end else None
        def elapsed(a, b):
            if a is None or b is None:
                return None
            if b < a:
                issues.append({"kind": "negative_tool_duration", "callId": call_id})
                return None
            return round(b - a, 3)
        calls.append({"callId": call_id, "requestId": received["data"].get("requestId") if received else None,
                      "tool": first["data"].get("tool", "unknown"),
                      "operation": arguments.get("operation") if isinstance(arguments, dict) and isinstance(arguments.get("operation"), str) else None,
                      "receivedUtc": received["timestampUtc"] if received else None,
                      "startUtc": start["timestampUtc"] if start else None,
                      "endUtc": end["timestampUtc"] if end else None,
                      "receivedMs": received_time, "startMs": start_time, "endMs": end_time,
                      "queueMs": elapsed(received_time, start_time), "executionMs": elapsed(start_time, end_time),
                      "totalMs": elapsed(received_time, end_time), "outcome": outcome, "pending": pending,
                      "reportedTimings": {"queueDelayMs": start["data"].get("queueDelayMs") if start else None,
                                          "durationMs": end["data"].get("durationMs") if end else None,
                                          "totalMs": end["data"].get("totalMs") if end else None},
                      "effectVerifiedFlags": _verification(result),
                      "requestFingerprint": received["data"].get("requestFingerprint") if received else None,
                      "logicalFingerprint": received["data"].get("logicalFingerprint") if received else None,
                      "arguments": arguments, "result": result,
                      "argumentsArtifact": received["data"].get("argumentsArtifact") if received else None,
                      "resultArtifact": end["data"].get("resultArtifact") if end else None,
                      "error": end["data"].get("error") if end else None,
                      "stages": sorted(stages, key=lambda item: item["startMs"] if item["startMs"] is not None else float("inf")),
                      "eventSequences": [event["seq"] for event in call_events]})
        call = calls[-1]
        call["recordedEventIntervals"] = {"queueMs": call["queueMs"], "executionMs": call["executionMs"], "totalMs": call["totalMs"]}
        for field, reported_field in (("queueMs", "queueDelayMs"), ("executionMs", "durationMs"), ("totalMs", "totalMs")):
            recorded = call["reportedTimings"][reported_field]
            if number(recorded) and recorded >= 0:
                call[field] = round(recorded, 3)
    calls.sort(key=lambda call: call["receivedMs"] if call["receivedMs"] is not None else (call["startMs"] or 0))
    if not any(event["event"] == "trace.start" for event in events):
        issues.append({"kind": "missing_trace_start"})
    unassociated_spans = defaultdict(list)
    for event in events:
        if event["event"].startswith("stage.") and not event.get("traceId"):
            unassociated_spans[event["spanId"]].append(event)
    for span_id, span_events in unassociated_spans.items():
        starts = sum(event["event"] == "stage.start" for event in span_events)
        ends = sum(event["event"] == "stage.end" for event in span_events)
        if (starts != 1 or ends != 1) and not (not closed and starts == 1 and ends == 0):
            issues.append({"kind": "incomplete_or_duplicate_lifecycle_stage", "spanId": span_id})
    dropped = 0
    for event in events:
        data = event["data"]
        if event["event"] == "trace.dropped":
            count = data.get("droppedCount", 0)
            if number(count):
                dropped += max(0, count)
            issues.append({"kind": "dropped_events", "count": count, "sequence": event["seq"]})
        if event["event"] == "trace.close":
            for key in ("writeErrors", "droppedEvents", "pendingEvents"):
                count = data.get(key, 0)
                if number(count) and count > 0:
                    issues.append({"kind": key, "count": count})
            dropped = max(dropped, data.get("droppedEvents", 0) if number(data.get("droppedEvents")) else 0)
    intervals = []
    for call in calls:
        begin = call["receivedMs"] if call["receivedMs"] is not None else call["startMs"]
        if begin is not None:
            intervals.append((begin, call["endMs"] if call["endMs"] is not None else trace_end))
    union = union_intervals(intervals)
    gaps, cursor = [], trace_begin
    for begin, end in union:
        if begin > cursor:
            gaps.append({"startMs": cursor, "endMs": begin, "durationMs": round(begin - cursor, 3)})
        cursor = max(cursor, end)
    if cursor < trace_end:
        gaps.append({"startMs": cursor, "endMs": trace_end, "durationMs": round(trace_end - cursor, 3)})
    if events:
        epoch = datetime.fromisoformat(events[0]["timestampUtc"].replace("Z", "+00:00"))
        for gap in gaps:
            gap["startUtc"] = (epoch + timedelta(milliseconds=gap["startMs"] - trace_begin)).isoformat()
            gap["endUtc"] = (epoch + timedelta(milliseconds=gap["endMs"] - trace_begin)).isoformat()
    groups = defaultdict(list)
    stage_groups = defaultdict(list)
    for call in calls:
        groups[(call["tool"], json.dumps(call["operation"], sort_keys=True))].append(call)
        for stage in call["stages"]:
            if number(stage["durationMs"]):
                stage_groups[stage["name"]].append(stage["durationMs"])
    return {"schemaVersion": 1, "capsuleDirectory": str(capsule), "runId": events[0]["runId"] if events else None,
            "generatedUtc": datetime.now(timezone.utc).isoformat(), "eventFiles": files,
            "eventPayloadArtifacts": [event["payloadArtifact"] for event in events if "payloadArtifact" in event],
            "startUtc": events[0]["timestampUtc"] if events else None,
            "endUtc": events[-1]["timestampUtc"] if events else None,
            "complete": closed and not issues, "closed": closed, "completeThroughLastEvent": not issues,
            "liveSnapshot": not closed, "integrityIssues": issues, "droppedEvents": dropped,
            "timing": {"observedWindowMs": round(trace_end - trace_begin, 3),
                       "toolBusyUnionMs": round(sum(end - begin for begin, end in union), 3),
                       "outsidePluginMs": round(sum(gap["durationMs"] for gap in gaps), 3),
                       "queue": distribution([call["queueMs"] for call in calls]),
                       "execution": distribution([call["executionMs"] for call in calls]),
                       "endToEnd": distribution([call["totalMs"] for call in calls])},
            "counts": {"events": len(events), "calls": len(calls),
                       "outcomes": dict(Counter(call["outcome"] for call in calls)),
                       "unfinishedCalls": sum(call["endMs"] is None for call in calls),
                       "pendingResponses": sum(call["pending"] for call in calls)},
            "operationFrequency": [{"tool": key[0], "operation": json.loads(key[1]), "count": len(items),
                                    "queue": distribution([item["queueMs"] for item in items]),
                                    "execution": distribution([item["executionMs"] for item in items]),
                                    "endToEnd": distribution([item["totalMs"] for item in items])}
                                   for key, items in sorted(groups.items(), key=lambda pair: -len(pair[1]))],
            "stageTiming": [{"name": name, **distribution(values)} for name, values in sorted(stage_groups.items())],
            "repetitions": {"exact": fingerprint_groups(calls, "requestFingerprint"),
                            "logical": fingerprint_groups(calls, "logicalFingerprint")},
            "slowestCalls": [call["callId"] for call in sorted(calls, key=lambda call: call["totalMs"] or -1, reverse=True)[:20]],
            "outsidePluginGaps": sorted(gaps, key=lambda gap: -gap["durationMs"]), "calls": calls,
            "unassociatedStageEvents": [event for event in events if event["event"].startswith("stage.") and not event.get("traceId")],
            "otherEvents": [event for event in events if not event["event"].startswith(("tool.", "stage."))]}


def _text(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _display_json(value: Any) -> str:
    """Keep binaries in capsule artifacts, not duplicated in an enormous HTML DOM."""
    def display(item):
        if isinstance(item, dict):
            if item.get("type") in ("image", "audio") and isinstance(item.get("data"), str):
                return {**item, "data": "[binary content retained in the original payload artifact]"}
            return {key: display(child) for key, child in item.items()}
        if isinstance(item, list):
            return [display(child) for child in item]
        if isinstance(item, str) and item.startswith(("data:image/", "data:audio/")):
            return "[data URL retained in the original payload artifact]"
        return item
    rendered = json.dumps(display(value), ensure_ascii=False, indent=2)
    if len(rendered) > DISPLAY_LIMIT:
        rendered = rendered[:DISPLAY_LIMIT] + "\n[HTML preview truncated; complete payload is retained in the capsule and summary.json.]"
    return "<pre>" + _text(rendered) + "</pre>"


def render_html(summary: dict) -> str:
    capsule = Path(summary["capsuleDirectory"])
    def artifact_link(reference, label):
        path = _artifact_path(capsule, reference)
        if path is None:
            return ""
        href = quote(path.relative_to(capsule).as_posix(), safe="/")
        return "<p><a download href='" + _text(href) + "'>" + _text(label) + "</a></p>"
    def table(headers, rows):
        return "<table><thead><tr>" + "".join("<th>" + _text(item) + "</th>" for item in headers) + "</tr></thead><tbody>" + "".join("<tr>" + "".join("<td>" + _text(item) + "</td>" for item in row) + "</tr>" for row in rows) + "</tbody></table>"
    def ms(value):
        return "unknown" if value is None else f"{value:,.1f} ms"
    timing, counts = summary["timing"], summary["counts"]
    status = ("INCOMPLETE TRACE — missing, dropped or invalid records. Do not infer successful completion." if not summary["completeThroughLastEvent"]
              else ("Closed trace; all recorded calls have matching endings. This does not establish successful application effects." if summary["closed"]
                    else "OPEN SNAPSHOT — records are valid through the last recorded event. Calls still in progress are listed below; the stream has not closed."))
    sections = ["<h1>BetterWinControl trace capsule</h1>", "<p class='status'>" + status + "</p>",
                "<p>Run <code>" + _text(summary["runId"]) + "</code><br>" + _text(summary["startUtc"]) + " → " + _text(summary["endUtc"]) + "</p>",
                "<p>Local diagnostic content may contain private request and response data. This report runs no scripts and loads no remote resources.</p>",
                table(["Metric", "Value"], [["Tool calls", counts["calls"]], ["Outcomes", counts["outcomes"]],
                                            ["Pending responses", counts["pendingResponses"]], ["Observed window", ms(timing["observedWindowMs"])],
                                            ["Calls still open", counts["unfinishedCalls"]],
                                            ["Tool busy (union of overlapping calls)", ms(timing["toolBusyUnionMs"])],
                                            ["Outside plugin calls", ms(timing["outsidePluginMs"])]]),
                "<p>Outside-plugin gaps are time with no recorded call in flight. They cannot identify model reasoning, user waiting, other tools or scheduling. Missing endings are counted busy through the last recorded event; final durations remain unknown. Stage durations are inclusive and may overlap. Per-call timings use producer measurements when available; event timestamp intervals are also retained for comparison, including logging overhead.</p>",
                "<h2>Operation frequency and timing</h2>",
                table(["Tool", "Operation", "Calls", "Queue mean", "Execution mean", "End-to-end max"],
                      [[row["tool"], row["operation"], row["count"], ms(row["queue"]["meanMs"]), ms(row["execution"]["meanMs"]), ms(row["endToEnd"]["maxMs"])] for row in summary["operationFrequency"]]),
                "<h2>Slowest calls</h2>",
                "<p>" + ", ".join("<a href='#call-" + str(index) + "'>" + _text(call["callId"]) + "</a> (" + ms(call["totalMs"]) + ")" for call_id in summary["slowestCalls"] for index, call in enumerate(summary["calls"]) if call["callId"] == call_id) + "</p>",
                "<h2>Repeated requests</h2><p>Exact groups use recorded request fingerprints. Logical groups use the producer’s logical fingerprint, which may ignore volatile IDs. Repeated requests are evidence of repetition, not proof of an error or a loop.</p>",
                _display_json(summary["repetitions"]), "<h2>Stage timing</h2>",
                table(["Stage", "Completed spans", "Mean", "Maximum", "Inclusive sum"], [[row["name"], row["count"], ms(row["meanMs"]), ms(row["maxMs"]), ms(row["sumMs"])] for row in summary["stageTiming"]]),
                "<h2>Largest outside-plugin gaps</h2>",
                table(["Start from trace origin", "End", "Duration"], [[ms(row["startMs"]), ms(row["endMs"]), ms(row["durationMs"])] for row in summary["outsidePluginGaps"][:40]]),
                "<h2>Integrity</h2>", _display_json(summary["integrityIssues"]), "<h2>Chronological calls</h2>"]
    for index, call in enumerate(summary["calls"]):
        label = f'{index + 1}. {call["tool"]}' + (f' / {call["operation"]}' if call["operation"] else "")
        sections += ["<article id='call-" + str(index) + "'><h3>" + _text(label) + "</h3>",
                     "<p>Call <code>" + _text(call["callId"]) + "</code> · Request <code>" + _text(call["requestId"]) + "</code> · " + _text(call["outcome"]) + (" (pending response)" if call["pending"] else "") + "</p>",
                     "<p>" + _text(call["receivedUtc"]) + " → " + _text(call["endUtc"]) + "; queue " + ms(call["queueMs"]) + "; execution " + ms(call["executionMs"]) + "; total " + ms(call["totalMs"]) + "</p>",
                     "<p>Effect verification reported by backend: " + _text(call["effectVerifiedFlags"] or "not reported") + ". Tool outcome alone is not proof of the intended UI effect.</p>",
                     "<details><summary>Request arguments</summary>" + artifact_link(call.get("argumentsArtifact"), "Complete original request JSON") + _display_json(call["arguments"]) + "</details>",
                     "<details><summary>Result and error</summary>" + artifact_link(call.get("resultArtifact"), "Complete original response JSON, including binary payloads") + _display_json({"result": call["result"], "error": call["error"]}) + "</details>",
                     "<details><summary>Stage timeline</summary>" + _display_json(call["stages"]) + "</details></article>"]
        sections.append("<details><summary>Timing measurements for call " + _text(call["callId"]) + "</summary>" + _display_json({"producer": call["reportedTimings"], "eventIntervals": call["recordedEventIntervals"]}) + "</details>")
        for screenshot in call.get("screenshots", []):
            path = _artifact_path(capsule, screenshot["path"])
            if path:
                sections.append("<details><summary>Recorded screenshot for call " + _text(call["callId"]) + "</summary><img loading='lazy' style='max-width:100%;height:auto' alt='Recorded tool response screenshot' src='" + _text(quote(path.relative_to(capsule).as_posix(), safe="/")) + "'></details>")
    sections += ["<h2>Lifecycle and other recorded events</h2>", _display_json(summary["otherEvents"]),
                 "<h2>Unassociated stage events</h2><p>These have no recorded call ID; their time is not attributed to a specific Codex request.</p>", _display_json(summary["unassociatedStageEvents"])]
    sections += ["<h2>Complete capsule files</h2><p>HTML previews are bounded for responsiveness. These local files retain the complete recorded data, including content not displayed above.</p>",
                 artifact_link("summary.json", "Complete machine-readable summary")]
    for reference in summary["eventFiles"] + summary.get("eventPayloadArtifacts", []):
        sections.append(artifact_link(reference, reference))
    return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; base-uri 'none'; form-action 'none'">
<title>BetterWinControl trace capsule</title><style>
:root{color-scheme:light dark;font-family:system-ui,sans-serif}body{max-width:1180px;margin:36px auto;padding:0 24px;line-height:1.55}
h1,h2,h3{line-height:1.2}h2{margin-top:2.2em}table{border-collapse:collapse;width:100%;margin:20px 0}td,th{text-align:left;border-bottom:1px solid #8886;padding:9px;overflow-wrap:anywhere}
article{border:1px solid #8887;border-radius:10px;margin:18px 0;padding:18px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#8881;padding:12px;border-radius:6px;font-size:12px}
summary{cursor:pointer;padding:6px 0}.status{font-weight:700;border-left:4px solid #c97b22;padding:12px}a{color:#448bdf}code{overflow-wrap:anywhere}
</style></head><body>""" + "".join(sections) + "</body></html>"


def build_report(capsule: Path) -> dict:
    capsule = capsule.resolve()
    if not capsule.is_dir():
        raise ValueError("Capsule directory does not exist")
    summary = analyze(capsule)
    if not summary["eventFiles"]:
        raise ValueError("Capsule contains no events*.jsonl files")
    _extract_screenshots(capsule, summary)
    for event in summary["otherEvents"] + summary["unassociatedStageEvents"]:
        _replace_duplicate_binary(event.get("data"), event.get("payloadArtifact") or summary["eventFiles"])
    _atomic_text(capsule / "summary.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    _atomic_text(capsule / "report.html", render_html(summary))
    return summary


def _replace_duplicate_binary(value: Any, source: Any) -> None:
    if isinstance(value, dict):
        if value.get("type") in ("image", "audio") and isinstance(value.get("data"), str):
            value["data"] = {"retainedInOriginalArtifact": source, "originalEncodedLength": len(value["data"])}
        for child in value.values():
            _replace_duplicate_binary(child, source)
    elif isinstance(value, list):
        for child in value:
            _replace_duplicate_binary(child, source)


def _extract_screenshots(capsule: Path, summary: dict) -> None:
    """Extract only explicit MCP image blocks; never load remote page resources."""
    for index, call in enumerate(summary["calls"]):
        result = call.get("result")
        content = result.get("content", []) if isinstance(result, dict) else []
        call["screenshots"] = []
        if not isinstance(content, list):
            continue
        for image_index, item in enumerate(content):
            if not isinstance(item, dict) or item.get("type") != "image" or not isinstance(item.get("data"), str):
                continue
            mime = item.get("mimeType")
            suffix = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(mime)
            if suffix is None:
                continue
            try:
                raw = base64.b64decode(item["data"], validate=True)
            except (ValueError, base64.binascii.Error):
                summary["integrityIssues"].append({"kind": "invalid_image_payload", "callId": call["callId"]})
                summary["complete"] = False
                summary["completeThroughLastEvent"] = False
                continue
            valid = (suffix == "png" and raw.startswith(b"\x89PNG\r\n\x1a\n")
                     or suffix == "jpg" and raw.startswith(b"\xff\xd8\xff")
                     or suffix == "webp" and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP")
            if not valid:
                summary["integrityIssues"].append({"kind": "invalid_image_signature", "callId": call["callId"]})
                summary["complete"] = False
                summary["completeThroughLastEvent"] = False
                continue
            target = capsule / "report-assets" / f"call-{index + 1}-image-{image_index + 1}.{suffix}"
            if not target.resolve().is_relative_to(capsule):
                raise ValueError("Unsafe report asset directory")
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(raw)
            call["screenshots"].append({"path": target.relative_to(capsule).as_posix(), "mimeType": mime, "bytes": len(raw)})
            item["data"] = {"retainedInOriginalPayload": call.get("resultArtifact") or summary["eventFiles"],
                            "previewArtifact": target.relative_to(capsule).as_posix(), "originalEncodedLength": len(item["data"])}


def _atomic_text(target: Path, value: str) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, prefix=".trace-report-", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            output.write(value)
        os.replace(temporary, target)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def generate_report(capsule: Path) -> dict:
    """Small JSON-serializable API used by the MCP diagnostics operation."""
    capsule = Path(capsule).resolve()
    with REPORT_LOCK:
        summary = build_report(capsule)
    return {"report": str(capsule / "report.html"), "summary": str(capsule / "summary.json"),
            "complete": summary["complete"], "calls": summary["counts"]["calls"],
            "closed": summary["closed"], "completeThroughLastEvent": summary["completeThroughLastEvent"],
            "unfinishedCalls": summary["counts"]["unfinishedCalls"],
            "integrityIssueCount": len(summary["integrityIssues"]), "timing": summary["timing"]}


def latest_capsule(root: Path) -> Path:
    if not root.is_dir():
        raise ValueError("No trace directory exists yet")
    choices = [directory for directory in root.iterdir() if directory.is_dir() and any(directory.glob("events*.jsonl"))]
    if not choices:
        raise ValueError("No trace capsules found")
    return max(choices, key=lambda directory: max(path.stat().st_mtime_ns for path in directory.glob("events*.jsonl")))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capsule", nargs="?", type=Path, help="Capsule directory; defaults to the latest run")
    parser.add_argument("--root", type=Path, default=ROOT / "runtime" / "traces", help="Trace root used when no capsule is supplied")
    args = parser.parse_args(argv)
    try:
        capsule = args.capsule or latest_capsule(args.root)
        report = generate_report(capsule)
    except (OSError, ValueError) as exc:
        print("Trace report failed: " + str(exc), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
