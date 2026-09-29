"""Check honest cancellation results; no native process or clipboard access."""
import importlib.util
import json
from pathlib import Path
import threading

root = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("clipboard_mcp_audit", root / "scripts/mcp_server.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
native = module.Native()
write_event, read_event = threading.Event(), threading.Event()
write_result, read_result = [], []
native.pending[1] = (None, write_event, write_result)
native.pending[2] = (None, read_event, read_result)
native.pending_methods.update({1: "clipboard_write", 2: "observe"})
result = native.stop()
assert write_event.is_set() and read_event.is_set()
assert "unknown" in result["clipboardWriteOutcome"]
assert "clipboard_write_outcome_unknown" in write_result[0]["error"]["message"]
assert "clipboard_write" not in read_result[0]["error"]["message"]
assert "clipboard_write_outcome_unknown" in module.interrupted_message("clipboard_write", "timeout")
assert module.interrupted_message("observe", "timeout") == "timeout"
native.pending.clear()
native.pending_methods.clear()
assert "clipboardWriteOutcome" not in native.stop()
report = {"status": "passed", "scope": "Production MCP stop/error path with controlled pending records",
          "checks": ["Pending clipboard write has uncertain result on stop", "Ordinary reads not mislabeled as clipboard writes",
                     "Timeout annotation retains uncertain clipboard outcome", "No-pending stop makes no clipboard change claim"],
          "liveClipboardAccess": False, "nativeProcessesStarted": 0}
(root / "tests/results/clipboard-cancellation.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report))
