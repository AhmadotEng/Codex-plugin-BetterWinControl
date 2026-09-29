"""Reversible live Zen address-field test; original address stays in memory only."""
import ctypes
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from integration_test import JsonProcess

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "tests/results/zen-address-test.json"
TOKEN = "about:blank"


def main():
    report = {"startedAtUtc": datetime.now(timezone.utc).isoformat(),
              "scope": "Existing Zen address-field reversible ValuePattern test; no navigation command",
              "originalAddressPersisted": False, "navigationSubmitted": False, "changes": []}
    process = JsonProcess([sys.executable, "-u", str(ROOT / "scripts/mcp_server.py")])
    original = None
    changed = False
    restored = False

    def observe_field():
        data, _ = process.tool("observe", {"max_elements": 200, "include_screenshot": False})
        ob = data["observation"]
        matches = [e for e in ob["elements"] if e.get("automationId") == "urlbar-input" and not e.get("protectedElement")]
        if len(matches) != 1 or not isinstance(matches[0].get("value"), str):
            raise RuntimeError("Unique readable unprotected address field not observed")
        return ob, matches[0]

    def samples(action):
        before, after = action.get("focusBefore", {}), action.get("focusAfter", {})
        return {"foregroundUnchanged": before.get("foreground") == after.get("foreground"),
                "focusUnchanged": before.get("focus") == after.get("focus"),
                "cursorUnchanged": (before.get("cursorX"), before.get("cursorY")) == (after.get("cursorX"), after.get("cursorY")),
                "paused": action.get("paused"), "verified": action.get("verified"), "mechanism": action.get("mechanism")}

    try:
        process.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "zen-address-test", "version": "1"}})
        windows, _ = process.tool("list_windows")
        choices = [w for w in windows["windows"] if w["process"].lower() == "zen" and w["selectable"]]
        if len(choices) != 1:
            raise RuntimeError("Expected one selectable existing Zen window")
        target = choices[0]
        process.tool("attach_window", {"hwnd": target["hwnd"]})
        observation, element = observe_field()
        original = element["value"]
        if original == TOKEN:
            raise RuntimeError("Test token matches current address; refusing meaningless verification")
        report["target"] = {"hwnd": target["hwnd"], "pid": target["pid"], "process": "zen"}
        # The selected page and its title are retained in memory solely to verify no navigation.
        original_title = target["title"]
        changed = True
        action, _ = process.tool("act", {"observation_id": observation["observationId"], "element_id": element["id"], "action": "set_value", "value": TOKEN})
        report["changes"].append({"stage": "set_test_value", **samples(action)})
        observation, element = observe_field()
        if element["value"] != TOKEN:
            raise RuntimeError("Address readback differs; no blind restore will overwrite possible user input")
        report["testValueReadBack"] = True
        if observation.get("paused"):
            # Restore our own exact change after an automatic focus pause, never steal focus back.
            process.tool("resume")
            observation, element = observe_field()
            if element["value"] != TOKEN:
                raise RuntimeError("Address changed before restoration; preserving current user text")
        action, _ = process.tool("act", {"observation_id": observation["observationId"], "element_id": element["id"], "action": "set_value", "value": original})
        report["changes"].append({"stage": "restore_original_value", **samples(action)})
        observation, element = observe_field()
        restored = element["value"] == original
        if not restored:
            raise RuntimeError("Original address was not verified restored")
        current_windows, _ = process.tool("list_windows")
        actual = [w for w in current_windows["windows"] if w["hwnd"] == target["hwnd"] and w["pid"] == target["pid"]]
        report["selectedPageTitleUnchanged"] = len(actual) == 1 and actual[0]["title"] == original_title
        report["status"] = "passed" if all(c["foregroundUnchanged"] and c["focusUnchanged"] for c in report["changes"]) and report["selectedPageTitleUnchanged"] else "changed_and_restored_with_interference"
    except Exception as exc:
        report["status"] = "failed"
        # Do not persist provider messages that could include the original address.
        report["failureType"] = type(exc).__name__
        report["failure"] = str(exc) if "{" not in str(exc) else "Tool returned an error; original address was not logged"
        if changed and not restored and original is not None:
            try:
                observation, element = observe_field()
                if element["value"] == TOKEN:
                    if observation.get("paused"):
                        process.tool("resume")
                        observation, element = observe_field()
                    if element["value"] == TOKEN:
                        process.tool("act", {"observation_id": observation["observationId"], "element_id": element["id"], "action": "set_value", "value": original})
                        _, element = observe_field()
                        restored = element["value"] == original
            except Exception:
                report["restorationRequiresUserCheck"] = True
    finally:
        report["testValueSubmitted"] = changed
        report["originalValueRestored"] = restored if changed else "no successful change recorded"
        try:
            stopped, _ = process.tool("stop")
            report["cleanup"] = stopped
        except Exception:
            report["cleanup"] = "server close requested"
        process.close()
        report["serverExited"] = process.proc.poll() is not None
        report["artifacts"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in [ROOT / "scripts/mcp_server.py", ROOT / "runtime/BackgroundControl.dll"]}
        report["limitations"] = "One reversible field test, with before/after focus samples; not proof of generic keyboard actions or concurrent user typing."
        REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
