"""Read-only Zen provider inspection through this plugin's local MCP server.

Persists only aggregate capabilities and recognized browser-chrome labels.
Never calls act, saves screenshots/page text/values, or navigates the browser.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

sys.dont_write_bytecode = True
from integration_test import JsonProcess, PLUGIN, TESTS

REPORT = TESTS / "results" / "realapp-inspection.json"
PROTECTED = re.compile(r"password|passcode|credential|sign[ -]?in|log[ -]?in|authentication|security|privacy|verify your identity", re.I)
CHROME_LABEL = re.compile(
    r"^(?:go back|go forward|back|forward|reload(?: current page)?|refresh|stop|home|"
    r"new tab|new window|open (?:application |app )?menu|application menu|bookmarks|downloads|"
    r"history|tab actions|close tab|search tabs|extensions|show bookmarks|"
    r"open a new tab|open a new window|toggle sidebar|sidebar|library|"
    r"back a page|forward a page|reload this page|stop loading this page)"
    r"(?:\s*\([^)]{0,60}\))?$", re.I)
SAFE_ID = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.:\-]{0,100}$")


class InspectionError(Exception):
    pass


def tool(server: JsonProcess, name: str, arguments: dict | None = None):
    response = server.rpc("tools/call", {"name": name, "arguments": arguments or {}})
    if "error" in response:
        raise InspectionError("rpc_error")
    result = response.get("result", {})
    if result.get("isError"):
        # Do not copy arbitrary exception content into the persisted diagnostic.
        text = " ".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        if "protected" in text.lower():
            raise InspectionError("protected_target_blocked")
        if "stopped" in text.lower():
            raise InspectionError("controller_stopped")
        if "timeout" in text.lower():
            raise InspectionError("provider_timeout")
        raise InspectionError("tool_error")
    data = result.get("structuredContent")
    if data is None:
        data = json.loads("\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"))
    return data


def focus_summary(before: dict, after: dict):
    return {
        "foregroundUnchanged": before.get("foreground") == after.get("foreground"),
        "focusUnchanged": before.get("focus") == after.get("focus"),
        "cursorUnchanged": (before.get("cursorX"), before.get("cursorY")) == (after.get("cursorX"), after.get("cursorY")),
        "focusQueriesSucceeded": bool(before.get("focusQuerySucceeded") and after.get("focusQuerySucceeded")),
        "limitation": "Before/after samples cannot exclude transient changes or attribute concurrent user movement.",
    }


def main():
    report = {
        "scope": "Existing Zen window, read-only local MCP attachment and bounded UIA observation",
        "startedAtUtc": datetime.now(timezone.utc).isoformat(),
        "actionsSubmitted": 0,
        "screenshotsRequested": False,
        "pageContentsAndValuesPersisted": False,
        "status": "not_started",
        "artifacts": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (
            PLUGIN / "scripts" / "mcp_server.py", PLUGIN / "runtime" / "BackgroundControl.dll")},
    }
    server = None
    try:
        server = JsonProcess([sys.executable, "-B", "-u", str(PLUGIN / "scripts" / "mcp_server.py")])
        init = server.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "zen-readonly-provider-inspection", "version": "1.0"}})
        if "result" not in init:
            raise InspectionError("initialize_failed")
        server.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listing = tool(server, "list_windows")
        zen = [w for w in listing.get("windows", []) if str(w.get("process", "")).lower() == "zen"]
        eligible = [w for w in zen if w.get("selectable") is True and w.get("title") and not PROTECTED.search(w["title"])]
        report["zenWindowCount"] = len(zen)
        report["eligibleZenWindowCount"] = len(eligible)
        if len(eligible) != 1:
            report["status"] = "ambiguous_multiple_zen_windows" if len(eligible) > 1 else (
                "protected_or_unselectable_zen_window" if zen else "no_zen_window")
            return 0
        selected = eligible[0]
        report["selection"] = {"hwnd": selected["hwnd"], "pid": selected["pid"], "process": "zen",
                               "selectedBy": "Unique selectable normal-titled Zen window from ListWindows metadata"}
        attached = tool(server, "attach_window", {"hwnd": selected["hwnd"]})
        report["attachFocusSamples"] = focus_summary(attached.get("focusBefore", {}), attached.get("focusAfter", {}))
        observed = tool(server, "observe", {"max_elements": 200, "include_screenshot": False})
        observation = observed.get("observation", observed)
        nodes = observation.get("elements", [])
        patterns = Counter(p for node in nodes for p in node.get("patterns", []))
        control_types = Counter(str(node.get("controlTypeId")) for node in nodes)
        report["elementCount"] = len(nodes)
        report["truncated"] = observation.get("truncated")
        report["patternCounts"] = dict(sorted(patterns.items()))
        report["controlTypeIdCounts"] = dict(sorted(control_types.items()))
        report["protectedElementCount"] = sum(bool(n.get("protectedElement")) for n in nodes)
        report["recognizedToolbarControls"] = [
            {"name": n["name"], "automationId": n.get("automationId") if SAFE_ID.fullmatch(n.get("automationId", "")) else "",
             "controlTypeId": n.get("controlTypeId"), "patterns": n.get("patterns", [])}
            for n in nodes if not n.get("protectedElement") and CHROME_LABEL.fullmatch(n.get("name", ""))
            and n.get("controlTypeId") in (50000, 50009, 50011, 50021)
        ]
        report["observationFocusSamples"] = focus_summary(observation.get("focusBefore", {}), observation.get("focusAfter", {}))
        report["pausedAfterObservation"] = observation.get("paused")
        report["status"] = "completed_read_only"
        report["limitation"] = "Provider patterns were observed, not invoked. This does not verify successful Zen input, full browser coverage, or concurrent user work."
        return 0
    except InspectionError as exc:
        report["status"] = str(exc)
        return 1
    except Exception as exc:
        report["status"] = "inspection_failed"
        report["errorType"] = type(exc).__name__
        return 1
    finally:
        if server:
            try:
                stopped = tool(server, "stop")
                report["cleanup"] = {"targetRevoked": stopped.get("targetRevoked"), "controllerExited": stopped.get("controllerExited")}
            except Exception:
                report["cleanup"] = {"stopRequestConfirmed": False}
            server.close()
            report["serverExited"] = server.proc.poll() is not None
        report["completedAtUtc"] = datetime.now(timezone.utc).isoformat()
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
