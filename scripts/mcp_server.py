"""Local stdio MCP bridge for our own Windows controller; no network listener."""
from __future__ import annotations

import atexit
import concurrent.futures
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
WRITE_LOCK = threading.Lock()
MAX_LINE = 1_000_000


def schema(properties=None, required=()):
    return {"type": "object", "properties": properties or {}, "required": list(required), "additionalProperties": False}


TOOLS = [
    {"name": "list_windows", "description": "List visible desktop application windows for explicit selection. Does not capture their contents or change focus.", "inputSchema": schema()},
    {"name": "attach_window", "description": "Select an authorized existing application window by its listed HWND and start a real floating live preview. Replaces any previous target; does not foreground the target.", "inputSchema": schema({"hwnd": {"type": "integer", "minimum": 1}}, ["hwnd"])},
    {"name": "observe", "description": "Read the selected window's accessibility tree and optionally its captured image. Use fresh observation and element IDs for actions. Password fields are redacted.", "inputSchema": schema({"max_elements": {"type": "integer", "minimum": 1, "maximum": 200}, "include_screenshot": {"type": "boolean"}})},
    {"name": "act", "description": "Perform a supported background action on an element from the latest observation. insert_text is available only on verified native Edit/RichEdit controls, not arbitrary browser fields. Returns unsupported when absent. Never injects physical mouse/keyboard input. Verify with a fresh observation.", "inputSchema": schema({"observation_id": {"type": "string"}, "element_id": {"type": "string"}, "action": {"type": "string", "enum": ["invoke", "set_value", "insert_text", "toggle", "select", "expand", "collapse", "scroll"]}, "value": {"type": "string", "maxLength": 50000}, "amount": {"type": "number"}}, ["observation_id", "element_id", "action"])},
    {"name": "pause", "description": "Pause automation for the selected window. The preview indicates paused state; observation remains available. A provider call already in progress may finish; use Stop to terminate the controller.", "inputSchema": schema()},
    {"name": "resume", "description": "Resume an attached paused target. Does not restore a stopped or closed target.", "inputSchema": schema()},
    {"name": "stop", "description": "Revoke the target and terminate this controller and preview immediately. Explicit attach is required to control again.", "inputSchema": schema()},
    {"name": "state", "description": "Read current selection, pause and capture state without an action or foreground change.", "inputSchema": schema()},
    {"name": "clipboard_state", "description": "Inspect sequence number and text availability on the SHARED system clipboard, without reading contents. Requires an active attached session.", "inputSchema": schema()},
    {"name": "clipboard_read", "description": "Explicitly read shared clipboard text when the user's task authorizes it. Returns a sequence token. Never used implicitly for background typing. Requires an active attached session.", "inputSchema": schema()},
    {"name": "clipboard_write", "description": "Explicitly replace shared clipboard with requested text only if it still has the expected sequence. Replaces all formats; never restores an older clipboard over later user content. Requires active attached session and user-authorized clipboard use.", "inputSchema": schema({"text": {"type": "string", "maxLength": 50000}, "expected_sequence": {"type": "integer", "minimum": 0, "maximum": 4294967295}}, ["text", "expected_sequence"])},
]
TOOL_MAP = {tool["name"]: tool for tool in TOOLS}
for tool in TOOLS:
    tool["annotations"] = {"readOnlyHint": tool["name"] in {"list_windows", "observe", "state", "clipboard_state", "clipboard_read"}, "destructiveHint": tool["name"] in {"act", "clipboard_write"}, "openWorldHint": False}


def send(message):
    with WRITE_LOCK:
        sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def validate(name, args):
    if name not in TOOL_MAP:
        raise ValueError("unknown_tool")
    if not isinstance(args, dict):
        raise ValueError("arguments_must_be_object")
    spec = TOOL_MAP[name]["inputSchema"]
    if set(args) - set(spec["properties"]):
        raise ValueError("unknown_argument")
    for field in spec["required"]:
        if field not in args:
            raise ValueError(f"missing_argument: {field}")
    for field, value in args.items():
        rule = spec["properties"][field]
        kind = rule["type"]
        valid = ((kind == "integer" and type(value) is int) or (kind == "number" and type(value) in (int, float))
                 or (kind == "boolean" and type(value) is bool) or (kind == "string" and isinstance(value, str)))
        if not valid:
            raise ValueError(f"invalid_argument_type: {field}")
        if "enum" in rule and value not in rule["enum"]:
            raise ValueError(f"invalid_choice: {field}")
        if "minimum" in rule and value < rule["minimum"] or "maximum" in rule and value > rule["maximum"]:
            raise ValueError(f"out_of_range: {field}")
        if isinstance(value, str) and len(value) > rule.get("maxLength", 200):
            raise ValueError(f"argument_too_long: {field}")


def interrupted_message(method, message):
    if method == "clipboard_write":
        return message + "; clipboard_write_outcome_unknown: shared clipboard may have changed; do not automatically retry or restore"
    return message


class ControllerError(RuntimeError):
    def __init__(self, details):
        super().__init__(details.get("message", "controller_error"))
        self.details = details


class Native:
    def __init__(self):
        self.lock = threading.RLock()
        self.operation = threading.Lock()
        self.process = None
        self.pending = {}
        self.pending_methods = {}
        self.ids = itertools.count(1)
        self.stopped = True
        self.generation = 0

    def _start(self):
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                return self.process
            executable = ROOT / "runtime" / "BackgroundControl.exe"
            if not executable.is_file():
                raise RuntimeError("controller_not_built: run scripts/build.ps1")
            proc = subprocess.Popen([str(executable), "--rpc", "--parent-pid", str(os.getpid())],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                encoding="utf-8", errors="replace", bufsize=1, cwd=str(ROOT),
                creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
            self.process = proc
            threading.Thread(target=self._read, args=(proc,), daemon=True).start()
            threading.Thread(target=self._drain_error, args=(proc,), daemon=True).start()
            return proc

    def _drain_error(self, proc):
        for _ in proc.stderr:
            pass  # Exceptions are returned over RPC; no user data is logged to disk.

    def _read(self, proc):
        try:
            for line in proc.stdout:
                if len(line) > 40_000_000:
                    raise RuntimeError("controller_response_too_large")
                response = json.loads(line)
                with self.lock:
                    item = self.pending.get(response.get("id"))
                    if item and item[0] is proc:
                        item[2].append(response)
                        item[1].set()
        except Exception:
            pass
        finally:
            with self.lock:
                for ident, (owner, event, responses) in self.pending.items():
                    if owner is proc:
                        if not responses:
                            responses.append({"error": {"message": interrupted_message(self.pending_methods.get(ident), "controller_exited_or_stopped")}})
                        event.set()
                if self.process is proc:
                    self.process = None
                    self.stopped = True
                    self.generation += 1
            # A broken protocol/pipe must not leave an untracked controller alive.
            try:
                if proc.poll() is None:
                    proc.terminate()
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
            except OSError:
                pass

    def _write(self, proc, ident, method, params):
        try:
            proc.stdin.write(json.dumps({"id": ident, "method": method, "params": params}, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except Exception:
            with self.lock:
                item = self.pending.get(ident)
                if item and item[0] is proc:
                    if not item[2]:
                        item[2].append({"error": {"message": interrupted_message(method, "controller_write_failed_or_stopped")}})
                    item[1].set()

    def call(self, method, params, expected_generation=None):
        with self.lock:
            requested_generation = self.generation if expected_generation is None else expected_generation
        with self.operation:
            with self.lock:
                if requested_generation != self.generation:
                    raise RuntimeError("request_cancelled_by_stop")
                if self.stopped and method not in {"attach_window", "list_windows", "state"}:
                    raise RuntimeError("stopped: explicitly attach a target first")
                if method == "state" and self.process is None:
                    return {"controller": {"attached": False, "stopped": True}, "capture": {"active": False}}
                proc = self._start()
                ident = next(self.ids)
                event, responses = threading.Event(), []
                self.pending[ident] = (proc, event, responses)
                self.pending_methods[ident] = method
            # Never hold the stop/state lock across a potentially full pipe.
            # A separate writer keeps the response deadline effective even if stdin blocks.
            threading.Thread(target=self._write, args=(proc, ident, method, params), daemon=True).start()
            try:
                if not event.wait(12):
                    self.stop()
                    raise RuntimeError(interrupted_message(method, "controller_timeout: controller terminated; explicitly reattach to continue"))
                response = responses[0]
                if "error" in response:
                    raise ControllerError(response["error"])
                with self.lock:
                    if requested_generation != self.generation:
                        raise RuntimeError(interrupted_message(method, "request_cancelled_by_stop"))
                    if method == "attach_window":
                        self.stopped = False
                return response.get("result", {})
            finally:
                with self.lock:
                    self.pending.pop(ident, None)
                    self.pending_methods.pop(ident, None)

    def stop(self):
        with self.lock:
            self.generation += 1
            self.stopped = True
            proc, self.process = self.process, None
            clipboard_uncertain = "clipboard_write" in self.pending_methods.values()
            for ident, (owner, event, responses) in self.pending.items():
                if not responses:
                    responses.append({"error": {"message": interrupted_message(self.pending_methods.get(ident), "stopped: controller terminated")}})
                event.set()
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
            finally:
                if proc.stdin:
                    proc.stdin.close()
        result = {"stopped": True, "targetRevoked": True, "controllerExited": proc is None or proc.poll() is not None}
        if clipboard_uncertain:
            result["clipboardWriteOutcome"] = "unknown: shared clipboard may have changed; do not automatically retry or restore"
        return result


NATIVE = Native()
atexit.register(NATIVE.stop)


def tool_call(name, args, expected_generation=None):
    try:
        validate(name, args)
        result = NATIVE.stop() if name == "stop" else NATIVE.call(name, args, expected_generation)
        image = None
        if name == "observe" and isinstance(result, dict):
            image = result.get("capture", {}).pop("pngBase64", None)
        content = [{"type": "text", "text": json.dumps(result, ensure_ascii=False, separators=(",", ":"))}]
        if image:
            content.append({"type": "image", "mimeType": "image/png", "data": image})
        return {"content": content, "structuredContent": result, "isError": False}
    except Exception as exc:
        error = exc.details if isinstance(exc, ControllerError) else {"message": str(exc)}
        return {"content": [{"type": "text", "text": json.dumps(error, ensure_ascii=False)}], "structuredContent": {"error": error}, "isError": True}


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    requests = {}
    request_lock = threading.Lock()
    initialized = False

    def finish(ident, future):
        try:
            send({"jsonrpc": "2.0", "id": ident, "result": future.result()})
        except Exception:
            send({"jsonrpc": "2.0", "id": ident, "error": {"code": -32603, "message": "Internal tool error"}})
        finally:
            with request_lock:
                requests.pop(ident, None)

    try:
        for line in sys.stdin:
            ident = None
            try:
                if len(line) > MAX_LINE:
                    raise ValueError("request_too_large")
                request = json.loads(line)
                if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                    raise ValueError("invalid_jsonrpc_request")
                ident = request.get("id")
                method, params = request.get("method"), request.get("params", {})
                if method == "notifications/cancelled":
                    with request_lock:
                        future = requests.get(params.get("requestId"))
                    if future and not future.done():
                        NATIVE.stop()
                        future.cancel()
                    continue
                if ident is None:
                    continue
                if method == "initialize":
                    requested = params.get("protocolVersion")
                    version = requested if requested in {"2024-11-05", "2025-03-26", "2025-06-18"} else "2025-06-18"
                    result = {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                              "serverInfo": {"name": "windows-background-control", "version": "0.1.0"}}
                    initialized = True
                elif method == "ping":
                    result = {}
                elif not initialized:
                    raise ValueError("initialize_required")
                elif method == "tools/list":
                    result = {"tools": TOOLS}
                elif method == "tools/call":
                    if params.get("name") == "stop":
                        result = tool_call("stop", params.get("arguments", {}))
                        send({"jsonrpc": "2.0", "id": ident, "result": result})
                        continue
                    with request_lock:
                        if ident in requests:
                            raise ValueError("duplicate_request_id")
                        with NATIVE.lock:
                            received_generation = NATIVE.generation
                        future = pool.submit(tool_call, params.get("name"), params.get("arguments", {}), received_generation)
                        requests[ident] = future
                    future.add_done_callback(lambda f, request_id=ident: finish(request_id, f))
                    continue
                else:
                    send({"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "Method not found"}})
                    continue
                send({"jsonrpc": "2.0", "id": ident, "result": result})
            except Exception as exc:
                send({"jsonrpc": "2.0", "id": ident, "error": {"code": -32602, "message": str(exc)}})
    finally:
        NATIVE.stop()
        pool.shutdown(wait=True, cancel_futures=True)


if __name__ == "__main__":
    main()
