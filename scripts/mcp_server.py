"""Local stdio MCP bridge for our own Windows controller; no network listener."""
from __future__ import annotations

import atexit
import concurrent.futures
import contextvars
from contextlib import contextmanager
import hashlib
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
sys.path.insert(0, str(ROOT / "scripts"))
from trace_capsule import TraceCapsule, disabled


def trace_source_hashes():
    hashes = {}
    for relative in ("scripts/mcp_server.py", "scripts/trace_capsule.py", "scripts/trace_report.py", "extensions/zen/native/core_adapter.py", "extensions/zen/native/bridge.py"):
        try:
            hashes[relative] = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
        except OSError:
            hashes[relative] = None
    return hashes

# Trace only this plugin's inputs/outputs. No global keystroke, desktop or Codex-log collection.
TRACE = disabled() if os.environ.get("BWC_TRACE_DISABLED") == "1" else TraceCapsule(
    Path(os.environ.get("BWC_TRACE_DIR", str(ROOT / "runtime" / "traces"))),
    metadata={"component": "BetterWinControl MCP", "pid": os.getpid(),
              "threadId": os.environ.get("CODEX_THREAD_ID"),
              "label": os.environ.get("BWC_TRACE_LABEL"), "sourceSha256": trace_source_hashes(),
              "contentMode": "full", "coverage": "plugin requests, responses and instrumented stages"})
REPORT_LOCK = threading.Lock()


def schema(properties=None, required=()):
    return {"type": "object", "properties": properties or {}, "required": list(required), "additionalProperties": False}


TOOLS = [
    {"name": "diagnostics", "description": "Read this MCP process's local full-content trace status. Set report:true to refresh the forensic HTML timeline and summary: every BetterWinControl call, request/response, repeat, error, stage duration and outside-plugin gaps. Does not inspect or control an app. Logs can contain page text, typed text, clipboard results and screenshots; nothing is uploaded.", "inputSchema": schema({"report": {"type": "boolean"}})},
    {"name": "list_windows", "description": "List visible desktop application windows for explicit selection. Does not capture their contents or change focus.", "inputSchema": schema()},
    {"name": "interaction_targets", "description": "Track verified same-process owner-chain dialogs/popups and the parent of the selected window. Returns exact native identities, relationship evidence and completeness limits. Same-PID siblings, cross-process dialogs and ownerless menus are not assumed owned. Select a verified target explicitly with attach_window; old native helper removal must be verified first. Does not itself provide native menu-loop or cross-app drag support.", "inputSchema": schema()},
    {"name": "attach_window", "description": "Select an authorized existing application window by its listed HWND and start a real floating live preview. Replaces any previous target; does not foreground the target.", "inputSchema": schema({"hwnd": {"type": "integer", "minimum": 1}}, ["hwnd"])},
    {"name": "observe", "description": "Observe the selected window and verified owned dialogs, with optional name/automation-ID search or subtree. Follow continuation until complete; an incomplete page does not prove a control is absent. Returns a fresh inputFrame token when capture geometry is verified.", "inputSchema": schema({"max_elements": {"type": "integer", "minimum": 1, "maximum": 2000}, "max_nodes": {"type": "integer", "minimum": 1, "maximum": 10000}, "continuation": {"type": "string"}, "search": {"type": "string"}, "subtree_id": {"type": "string"}, "include_screenshot": {"type": "boolean"}})},
    {"name": "capabilities", "description": "Describe semantic/native/browser backends and explicitly unverified or unsupported target operations. Does not attach a native helper.", "inputSchema": schema()},
    {"name": "browser", "description": "Always-ready Zen companion for the selected native window/session. After attach, connect authenticates the existing helper and automatically binds an unambiguous browser window; no toolbar click is required. If pending:true, poll pair within the returned deadline until bound:true. Observe/input/verify require verified binding. Stop revokes the session; a later explicit task may connect afresh. Page actions still require existing site permission. prepare is installation setup only. No focus commands or implicit installation.", "inputSchema": schema({"operation": {"type": "string", "enum": ["prepare", "connect", "pair", "capabilities", "observe", "input", "verify", "disconnect"]}, "arguments": {"type": "object", "additionalProperties": True}}, ["operation"])},
    {"name": "input", "description": "EXPERIMENTAL independent application-local pointer/key sequence, bound to a fresh observe inputFrame.frameId. Coordinates are physical screenshot pixels in the selected client area. No foreground/shared mouse fallback. Results distinguish delivery from verified application effects. Native raw input, IME, cross-application dragging, and nested menu loops are not yet supported. Observe again to verify effects.", "inputSchema": schema({"frame_id": {"type": "string"}, "deadline_ms": {"type": "integer", "minimum": 100, "maximum": 8000}, "steps": {"type": "array", "minItems": 1, "maxItems": 128, "items": schema({"type": {"type": "string", "enum": ["move", "hover", "click", "double_click", "button_down", "button_up", "wheel", "drag", "text", "key_down", "key_up", "shortcut", "release"]}, "x": {"type": "integer", "minimum": 0, "maximum": 100000}, "y": {"type": "integer", "minimum": 0, "maximum": 100000}, "button": {"type": "string", "enum": ["left", "right", "middle"]}, "delta": {"type": "integer", "minimum": -12000, "maximum": 12000}, "text": {"type": "string", "maxLength": 2000}, "vk": {"type": "integer", "minimum": 1, "maximum": 255}, "keysym": {"type": "string", "maxLength": 256}, "keys": {"type": "array", "minItems": 1, "maxItems": 8, "items": {"type": "integer", "minimum": 1, "maximum": 255}}, "path": {"type": "array", "minItems": 2, "maxItems": 64, "items": schema({"x": {"type": "integer", "minimum": 0, "maximum": 100000}, "y": {"type": "integer", "minimum": 0, "maximum": 100000}}, ["x", "y"])}}, ["type"])}}, ["frame_id", "steps"])},
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
    tool["annotations"] = {"readOnlyHint": tool["name"] in {"diagnostics", "list_windows", "interaction_targets", "observe", "state", "capabilities", "clipboard_state", "clipboard_read"}, "destructiveHint": tool["name"] in {"act", "input", "browser", "clipboard_write"}, "openWorldHint": False}


def send(message):
    TRACE.event("mcp.response", message=message)
    with WRITE_LOCK:
        sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()


@contextmanager
def traced_lock(lock, stage, **metadata):
    with TRACE.span(stage, **metadata):
        lock.acquire()
    try:
        yield
    finally:
        lock.release()


def trace_report():
    """Reporting failures never change an application-control outcome."""
    with REPORT_LOCK:
        try:
            if not TRACE.flush(timeout=2):
                return {"reportReady": False, "reason": "trace_flush_incomplete", "trace": TRACE.status()}
            status = TRACE.status()
            directory = status.get("directory") or status.get("runDir")
            if not directory:
                return {"reportReady": False, "trace": status}
            from trace_report import generate_report
            generated = generate_report(Path(directory))
            return {"reportReady": True, "report": generated, "trace": status}
        except Exception as exc:
            TRACE.event("report.error", error=str(exc), errorType=type(exc).__name__)
            return {"reportReady": False, "reason": "report_generation_failed", "trace": TRACE.status()}


def schedule_trace_report():
    threading.Thread(target=trace_report, name="BWC-TraceReport", daemon=True).start()


def validate(name, args):
    if name not in TOOL_MAP:
        raise ValueError("unknown_tool")

    def check(value, rule, field):
        kind = rule["type"]
        valid = ((kind == "integer" and type(value) is int) or (kind == "number" and type(value) in (int, float))
                 or (kind == "boolean" and type(value) is bool) or (kind == "string" and isinstance(value, str))
                 or (kind == "object" and isinstance(value, dict)) or (kind == "array" and isinstance(value, list)))
        if not valid:
            raise ValueError(f"invalid_argument_type: {field}")
        if kind == "object":
            properties = rule.get("properties", {})
            if not rule.get("additionalProperties", False) and set(value) - set(properties):
                raise ValueError(f"unknown_argument: {field}")
            for key in rule.get("required", []):
                if key not in value:
                    raise ValueError(f"missing_argument: {field}.{key}")
            for key, item in value.items():
                if key in properties:
                    check(item, properties[key], f"{field}.{key}")
        if kind == "array":
            if not rule.get("minItems", 0) <= len(value) <= rule.get("maxItems", 128):
                raise ValueError(f"array_length: {field}")
            for i, item in enumerate(value):
                check(item, rule["items"], f"{field}[{i}]")
        if "enum" in rule and value not in rule["enum"]:
            raise ValueError(f"invalid_choice: {field}")
        if "minimum" in rule and value < rule["minimum"] or "maximum" in rule and value > rule["maximum"]:
            raise ValueError(f"out_of_range: {field}")
        if isinstance(value, str) and len(value) > rule.get("maxLength", 200):
            raise ValueError(f"argument_too_long: {field}")
    check(args, TOOL_MAP[name]["inputSchema"], "arguments")


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
        self.operation = threading.RLock()
        self.writer = threading.Lock()
        self.process = None
        self.pending = {}
        self.pending_methods = {}
        self.ids = itertools.count(1)
        self.stopped = True
        self.generation = 0
        self.on_revoke = None

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
            TRACE.event("native.started", nativePid=proc.pid, executable=str(executable))
            threading.Thread(target=self._read, args=(proc,), daemon=True).start()
            threading.Thread(target=self._drain_error, args=(proc,), daemon=True).start()
            return proc

    def _drain_error(self, proc):
        for line in proc.stderr:
            TRACE.event("native.stderr", nativePid=proc.pid, text=line)

    def _read(self, proc):
        try:
            for line in proc.stdout:
                if len(line) > 40_000_000:
                    raise RuntimeError("controller_response_too_large")
                response = json.loads(line)
                TRACE.event("native.response", nativePid=getattr(proc, "pid", None), response=response)
                if response.get("notification") == "revoked":
                    with self.lock:
                        if self.process is proc and self.on_revoke:
                            self.on_revoke()
                    continue
                with self.lock:
                    item = self.pending.get(response.get("id"))
                    if item and item[0] is proc:
                        item[2].append(response)
                        item[1].set()
        except Exception as exc:
            TRACE.event("native.reader.error", error=str(exc), errorType=type(exc).__name__)
        finally:
            with self.lock:
                if self.process is proc and self.on_revoke:
                    self.on_revoke()
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
            TRACE.event("native.request", nativePid=getattr(proc, "pid", None), nativeRequestId=ident, method=method, arguments=params)
            with traced_lock(self.writer, "native.writer.queue", nativeRequestId=ident):
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
        with TRACE.span("native.rpc", method=method, arguments=params):
            result = self._call(method, params, expected_generation)
            TRACE.event("native.rpc.result", method=method, result=result)
            return result

    def _call(self, method, params, expected_generation=None):
        with self.lock:
            requested_generation = self.generation if expected_generation is None else expected_generation
        with traced_lock(self.operation, "native.operation.queue", method=method):
            with self.lock:
                if requested_generation != self.generation:
                    raise RuntimeError("request_cancelled_by_stop")
                if self.stopped and method not in {"attach_window", "list_windows", "state", "capabilities"}:
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
            context = contextvars.copy_context()
            threading.Thread(target=context.run, args=(self._write, proc, ident, method, params), daemon=True).start()
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
        return self.interrupt("stop")

    def interrupt(self, method):
        with TRACE.span("native.interrupt", method=method):
            result = self._interrupt(method)
            TRACE.event("native.interrupt.result", method=method, result=result)
            return result

    def _interrupt(self, method):
        if self.on_revoke:
            self.on_revoke()
        with self.lock:
            self.generation += 1
            if method == "stop":
                self.stopped = True
            proc = self.process
            clipboard_uncertain = "clipboard_write" in self.pending_methods.values()
            for ident, (owner, event, responses) in self.pending.items():
                if not responses:
                    responses.append({"error": {"message": interrupted_message(self.pending_methods.get(ident), "stopped: request_cancelled" if method == "stop" else "paused: request_cancelled")}})
                event.set()
            ident = next(self.ids)
            event, responses = threading.Event(), []
            if proc is not None and proc.poll() is None:
                self.pending[ident] = (proc, event, responses)
                self.pending_methods[ident] = method
        acknowledgement = None
        forced_exit = False
        if proc is not None:
            try:
                if proc.poll() is None:
                    can_deliver = self.writer.acquire(timeout=0.1)
                    if can_deliver:
                        self.writer.release()
                        context = contextvars.copy_context()
                        threading.Thread(target=context.run, args=(self._write, proc, ident, method, {}), daemon=True).start()
                        if event.wait(5.5) and responses:
                            acknowledgement = responses[0].get("result")
                    else:
                        # A blocked writer cannot carry cancellation. Terminate only our
                        # controller; the native host's owner-loss watchdog revokes separately.
                        # The absent teardown acknowledgement remains explicitly unverified.
                        forced_exit = True
                        proc.terminate()
                        proc.wait(timeout=2)
                if method == "stop":
                    # EOF also revokes the controller; the native host independently watches
                    # controller death. A lost reply is explicitly NOT hook-removal proof.
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.terminate()
                        proc.wait(timeout=2)
                    try:
                        proc.stdin.close()
                    except (OSError, ValueError):
                        pass
            except (OSError, subprocess.TimeoutExpired):
                pass
            finally:
                with self.lock:
                    self.pending.pop(ident, None)
                    self.pending_methods.pop(ident, None)
                    if (method == "stop" or forced_exit) and self.process is proc:
                        self.process = None
                        self.stopped = True
        result = {"stopped": method == "stop" or forced_exit, "paused": True, "targetRevoked": method == "stop" or forced_exit,
                  "controllerExited": proc is None or proc.poll() is not None,
                  "teardown": (acknowledgement or {}).get("teardown", {"teardownVerified": False, "status": "no_acknowledgement"} if proc else {"teardownRequired": False})}
        if clipboard_uncertain:
            result["clipboardWriteOutcome"] = "unknown: shared clipboard may have changed; do not automatically retry or restore"
        return result


NATIVE = Native()
atexit.register(NATIVE.stop)
BROWSER = None
BROWSER_OPERATION = threading.RLock()


def browser_call(operation, arguments, expected_generation=None):
    global BROWSER
    with traced_lock(BROWSER_OPERATION, "browser.operation.queue", operation=operation):
        with traced_lock(NATIVE.operation, "browser.native_gate.queue", operation=operation):
            if expected_generation is not None and expected_generation != NATIVE.generation:
                raise RuntimeError("request_cancelled_by_stop")
            generation = NATIVE.generation
            if BROWSER is None:
                folder = ROOT / "extensions" / "zen" / "native"
                if not (folder / "core_adapter.py").is_file():
                    raise RuntimeError("browser_adapter_not_built")
                # Only the bundled adapter code is loaded, never tool-supplied paths.
                sys.path.insert(0, str(folder))
                from core_adapter import CoreAdapter
                BROWSER = CoreAdapter(ROOT, lambda: NATIVE.call("state", {}).get("controller", {}), trace=TRACE)
                NATIVE.on_revoke = BROWSER.cancel
            adapter = BROWSER
            if operation == "input":
                NATIVE.call("invalidate_frame", {})
            if operation != "connect":
                with TRACE.span("browser.adapter", operation=operation):
                    return adapter.call(operation, arguments)
        # The accept thread must read native state while connect waits for it.
        # Holding NATIVE.operation here would block host identity verification.
        with TRACE.span("browser.adapter", operation=operation):
            result = adapter.call(operation, arguments)
        if generation != NATIVE.generation:
            adapter.cancel()
            raise RuntimeError("request_cancelled_by_stop")
        return result


def tool_call(name, args, expected_generation=None, trace_token=None):
    token = trace_token if trace_token is not None else TRACE.receive(name, args)
    with TRACE.begin(token):
        try:
            result = _tool_call(name, args, expected_generation)
        except BaseException as exc:
            TRACE.end(token, error=exc)
            raise
        TRACE.end(token, result=result)
    if name == "stop":
        schedule_trace_report()
    return result


def _tool_call(name, args, expected_generation=None):
    try:
        validate(name, args)
        if name == "diagnostics":
            result = trace_report() if args.get("report", False) else {"trace": TRACE.status()}
        elif name == "browser":
            if expected_generation is not None and expected_generation != NATIVE.generation:
                raise RuntimeError("request_cancelled_by_stop")
            result = browser_call(args["operation"], args.get("arguments", {}), expected_generation)
            if result.get("ok") is False:
                raise ControllerError({"code": "browser_adapter_error", "message": result.get("error", "browser_adapter_failed"), "evidence": result})
        else:
            result = NATIVE.interrupt(name) if name in {"stop", "pause"} else NATIVE.call(name, args, expected_generation)
        if name == "capabilities" and isinstance(result, dict):
            browser = result.setdefault("browser", {})
            browser["connectionMode"] = "on_request_without_toolbar_gesture"
            browser["status"] = "Browser-accepted v0.2 companion and registered native host required. connect automatically verifies and binds the selected window. Page operations require site permission."
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

    def finish(ident, token, future):
        try:
            if future.cancelled():
                TRACE.end(token, error=RuntimeError("cancelled_before_execution"))
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
                TRACE.event("mcp.request", message=request)
                if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                    raise ValueError("invalid_jsonrpc_request")
                ident = request.get("id")
                method, params = request.get("method"), request.get("params", {})
                if method == "notifications/cancelled":
                    TRACE.event("mcp.cancel_requested", requestId=params.get("requestId"))
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
                    token = TRACE.receive(params.get("name"), params.get("arguments", {}), request_id=ident)
                    if params.get("name") == "stop":
                        result = tool_call("stop", params.get("arguments", {}), trace_token=token)
                        send({"jsonrpc": "2.0", "id": ident, "result": result})
                        continue
                    with request_lock:
                        if ident in requests:
                            TRACE.end(token, error=ValueError("duplicate_request_id"))
                            raise ValueError("duplicate_request_id")
                        with NATIVE.lock:
                            received_generation = NATIVE.generation
                        future = pool.submit(tool_call, params.get("name"), params.get("arguments", {}), received_generation, token)
                        requests[ident] = future
                    future.add_done_callback(lambda f, request_id=ident, trace_token=token: finish(request_id, trace_token, f))
                    continue
                else:
                    send({"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "Method not found"}})
                    continue
                send({"jsonrpc": "2.0", "id": ident, "result": result})
            except Exception as exc:
                TRACE.event("mcp.protocol_error", requestId=ident, error=str(exc), rawRequest=line)
                send({"jsonrpc": "2.0", "id": ident, "error": {"code": -32602, "message": str(exc)}})
    finally:
        NATIVE.stop()
        pool.shutdown(wait=True, cancel_futures=True)
        TRACE.event("mcp.shutdown")
        TRACE.close()
        trace_report()


if __name__ == "__main__":
    main()
