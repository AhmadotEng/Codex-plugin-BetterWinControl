"""Lossless, local, bounded asynchronous diagnostics for BetterWinControl.

Only data passed to this recorder is collected. In particular it never inspects
the environment, browser storage, authentication handshakes or other tasks.
Full tool payloads may contain private page, text and clipboard data: each new
capsule is restricted to the current user (and SYSTEM on Windows).
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import threading
import time
import uuid

SCHEMA_VERSION = 1
_VOLATILE_IDS = {"observationid", "observation_id", "frameid", "frame_id", "actionid", "documentid", "continuation", "sessionid", "session_id"}


def _json(value):
    # MCP and controller payloads are JSON. Unknown Python objects are represented
    # explicitly rather than disappearing; no arbitrary object attributes read.
    def fallback(item):
        if isinstance(item, bytes):
            import base64
            return {"encoding": "base64", "pythonType": "bytes", "data": base64.b64encode(item).decode("ascii")}
        return {"pythonType": type(item).__name__, "representation": repr(item)}
    # Escaping also preserves lone UTF-16 surrogates permitted in JSON strings.
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True, default=fallback).encode("utf-8")


def _logical(value, key=""):
    if key.lower() in _VOLATILE_IDS:
        return {"volatileId": True}
    if isinstance(value, dict):
        return {k: _logical(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_logical(v) for v in value]
    return value


def _private_directory(path):
    """Protect a newly created directory before any diagnostic data is written."""
    path.mkdir(parents=True, exist_ok=False, mode=0o700)
    if os.name != "nt":
        path.chmod(0o700)
        return
    import ctypes
    from ctypes import wintypes
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    token, sid_text, descriptor = wintypes.HANDLE(), wintypes.LPWSTR(), ctypes.c_void_p()
    try:
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        sddl = "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;" + sid_text.value + ")"
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if not advapi.SetFileSecurityW(str(path), 0x80000004, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if descriptor.value:
            kernel.LocalFree(descriptor)
        if sid_text:
            kernel.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
        if token:
            kernel.CloseHandle(token)


@dataclass
class CallToken:
    call_id: str
    tool: str
    received_monotonic: float
    started_monotonic: float | None = None
    ended: bool = False

    @property
    def trace_id(self):
        return self.call_id


class TraceCapsule:
    def __init__(self, root_dir, metadata=None, *, queue_size=256, rotate_bytes=8 * 1024 * 1024,
                 flush_interval=0.25, queue_bytes=64 * 1024 * 1024, inline_bytes=32768):
        self.root_dir = Path(root_dir)
        self.metadata = metadata or {}
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + str(os.getpid()) + "-" + uuid.uuid4().hex[:8]
        self._origin = time.perf_counter()
        self._run_dir = None
        self._queue = queue.Queue(maxsize=max(1, queue_size))
        self._queue_bytes_limit = max(1, queue_bytes)
        self._inline_bytes = max(128, inline_bytes)
        self._rotate_bytes = max(256, rotate_bytes)
        self._flush_interval = max(0.01, flush_interval)
        self._emit_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._count_lock = threading.Lock()
        self._context = ContextVar("trace_capsule_" + self.run_id, default=(None, None))
        self._stop = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._worker = None
        self._stream = None
        self._stream_bytes = 0
        self._file_number = 0
        self._files = []
        self._seq = 0
        self._pending = 0
        self._pending_bytes = 0
        self._write_errors = 0
        self._lost = 0
        self._fallbacks = 0
        self._fallback_ms = 0.0
        self._recording_ms = 0.0
        self._write_ms = 0.0
        self._last_error = None
        self._closed = False
        self._failed_start = False

    def _failure(self, error, lost=0):
        with self._count_lock:
            self._write_errors += 1
            self._lost += lost
            self._last_error = {"class": type(error).__name__, "message": str(error)}

    def _start(self):
        if self._worker:
            return True
        if self._failed_start or self._closed:
            return False
        try:
            if not self.root_dir.exists():
                try:
                    _private_directory(self.root_dir)
                except FileExistsError:
                    # Independent MCP processes share this parent; another may
                    # create it between exists() and mkdir(). Per-run directory
                    # creation below remains strict and receives its own DACL.
                    if not self.root_dir.is_dir():
                        raise
            run_dir = self.root_dir / self.run_id
            _private_directory(run_dir)
            _private_directory(run_dir / "payloads")
            self._run_dir = run_dir
            self._worker = threading.Thread(target=self._writer, name="bwc-trace-writer", daemon=True)
            self._worker.start()
            self._submit("trace.start", {"metadata": self.metadata, "processId": os.getpid(), "captureMode": "full", "scope": "explicit-tool-and-controller-data-only"}, None, None, None)
            return True
        except Exception as exc:
            self._failed_start = True
            self._failure(exc)
            return False

    def _submit(self, name, data, trace_id, span_id, parent_span_id, payloads=None):
        # Called under the producer lock. Encode now so later mutations to tool
        # results cannot change the historical record. No truncation or sampling.
        frozen = _json(data)
        frozen_payloads = {name: _json(value) for name, value in (payloads or {}).items()}
        size = len(frozen) + sum(map(len, frozen_payloads.values()))
        record = {"schemaVersion": SCHEMA_VERSION, "runId": self.run_id,
                  "timestampUtc": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                  "monotonicMs": round((time.perf_counter() - self._origin) * 1000, 6), "event": name}
        if trace_id is not None:
            record["traceId"] = trace_id
        if span_id is not None:
            record["spanId"] = span_id
        if parent_span_id is not None:
            record["parentSpanId"] = parent_span_id
        pending = (record, frozen, frozen_payloads, size)
        with self._count_lock:
            overflow = self._pending_bytes + size > self._queue_bytes_limit
            self._pending += 1
            self._pending_bytes += size
            self._idle.clear()
        if not overflow:
            try:
                self._queue.put_nowait(pending)
                return
            except queue.Full:
                pass
        started = time.perf_counter()
        # Lossless overload: spool synchronously, never discard commands because
        # a queue filled. Only disk/serialization failure can create a gap.
        with self._write_lock:
            self._drain_locked()
            self._write_pending(pending)
        with self._count_lock:
            self._fallbacks += 1
            self._fallback_ms += (time.perf_counter() - started) * 1000

    def _emit(self, name, data, *, trace_id=None, span_id=None, parent_span_id=None, payloads=None):
        recording_started = time.perf_counter()
        try:
            with self._emit_lock:
                if self._closed:
                    return
                if not self._start():
                    with self._count_lock:
                        self._lost += 1
                    return
                current_trace, current_span = self._context.get()
                self._submit(name, data, trace_id if trace_id is not None else current_trace,
                             span_id, parent_span_id if parent_span_id is not None else current_span, payloads)
        except Exception as exc:
            self._failure(exc, lost=1)

        finally:
            with self._count_lock:
                self._recording_ms += (time.perf_counter() - recording_started) * 1000

    def receive(self, tool, args, request_id=None):
        display_tool = tool if isinstance(tool, str) and tool.strip() else "<invalid tool>"
        token = CallToken(uuid.uuid4().hex, display_tool, time.perf_counter())
        try:
            fingerprint_started = time.perf_counter()
            try:
                fingerprint = hashlib.sha256(_json({"tool": tool, "arguments": args})).hexdigest()
                logical = hashlib.sha256(_json({"tool": tool, "arguments": _logical(args)})).hexdigest()
            finally:
                with self._count_lock:
                    self._recording_ms += (time.perf_counter() - fingerprint_started) * 1000
            artifact = "payloads/" + token.call_id + "-arguments.json"
            data = {"tool": display_tool, "requestId": request_id, "argumentsArtifact": artifact,
                    "requestFingerprint": fingerprint, "logicalFingerprint": logical}
            if display_tool != tool:
                data["requestedTool"] = tool
            self._emit("tool.received", data,
                       trace_id=token.call_id, payloads={artifact: args})
        except Exception as exc:
            self._failure(exc, lost=1)
        return token

    @contextmanager
    def begin(self, token):
        if token is None:
            yield
            return
        token.started_monotonic = time.perf_counter()
        context = self._context.set((token.call_id, None))
        self._emit("tool.start", {"tool": token.tool, "queueDelayMs": round((token.started_monotonic - token.received_monotonic) * 1000, 6)}, trace_id=token.call_id)
        try:
            yield token
        finally:
            self._context.reset(context)

    def end(self, token, result=None, error=None):
        if token is None or token.ended:
            return
        token.ended = True
        now = time.perf_counter()
        artifact = "payloads/" + token.call_id + "-result.json"
        result_error = isinstance(result, dict) and (result.get("isError") is True or result.get("ok") is False)
        data = {"tool": token.tool, "durationMs": round((now - (token.started_monotonic or token.received_monotonic)) * 1000, 6),
                "totalMs": round((now - token.received_monotonic) * 1000, 6), "outcome": "error" if error is not None or result_error else "ok",
                "resultArtifact": artifact}
        if error is not None:
            data["error"] = self._error(error)
        self._emit("tool.end", data, trace_id=token.call_id, payloads={artifact: result})

    @staticmethod
    def _error(error):
        if isinstance(error, BaseException):
            return {"class": type(error).__name__, "message": str(error)}
        return error

    @contextmanager
    def span(self, name, **metadata):
        trace_id, parent = self._context.get()
        span_id, started = uuid.uuid4().hex, time.perf_counter()
        self._emit("stage.start", {"name": name, "metadata": metadata}, trace_id=trace_id, span_id=span_id, parent_span_id=parent)
        context = self._context.set((trace_id, span_id))
        error = None
        try:
            yield span_id
        except BaseException as exc:
            error = exc
            raise
        finally:
            self._context.reset(context)
            data = {"name": name, "durationMs": round((time.perf_counter() - started) * 1000, 6), "outcome": "error" if error is not None else "ok"}
            if error is not None:
                data["error"] = self._error(error)
            self._emit("stage.end", data, trace_id=trace_id, span_id=span_id, parent_span_id=parent)

    def event(self, name, **metadata):
        self._emit(name, metadata)

    def _artifact(self, relative, content):
        # All paths are generated by this module, never taken from tool payloads.
        path = self._run_dir / relative
        with path.open("xb") as stream:
            if os.name != "nt":
                os.chmod(path, 0o600)
            stream.write(content)
            stream.flush()

    def _write_pending(self, pending):
        writing_started = time.perf_counter()
        record, data_bytes, payloads, size = pending
        try:
            self._seq += 1
            record["seq"] = self._seq
            if record["event"] == "trace.close":
                final = self.status()
                final.update({"closed": True, "pendingEvents": max(0, final["pendingEvents"] - 1),
                              "pendingBytes": max(0, final["pendingBytes"] - size)})
                data_bytes = _json(final)
            for relative, content in payloads.items():
                self._artifact(relative, content)
            if len(data_bytes) > self._inline_bytes:
                relative = "payloads/event-" + str(self._seq).zfill(9) + ".json"
                self._artifact(relative, data_bytes)
                record["data"] = {"payloadArtifact": relative, "payloadBytes": len(data_bytes), "payloadSha256": hashlib.sha256(data_bytes).hexdigest()}
            else:
                record["data"] = json.loads(data_bytes)
            line = _json(record) + b"\n"
            if self._stream is None or self._stream_bytes + len(line) > self._rotate_bytes:
                if self._stream:
                    self._stream.flush()
                    self._stream.close()
                self._file_number += 1
                filename = "events-" + str(self._file_number).zfill(6) + ".jsonl"
                self._stream = (self._run_dir / filename).open("xb")
                if os.name != "nt":
                    os.chmod(self._run_dir / filename, 0o600)
                self._files.append(filename)
                self._stream_bytes = 0
            self._stream.write(line)
            self._stream_bytes += len(line)
        except Exception as exc:
            self._failure(exc, lost=1)
            # A failed rotation/write must not leave a closed or broken stream
            # permanently poisoning subsequent records after a transient error.
            if self._stream:
                try:
                    self._stream.close()
                except Exception:
                    pass
            self._stream = None
            self._stream_bytes = 0
        finally:
            with self._count_lock:
                self._write_ms += (time.perf_counter() - writing_started) * 1000
                self._pending -= 1
                self._pending_bytes -= size
                if self._pending == 0:
                    self._idle.set()

    def _drain_locked(self):
        while True:
            try:
                pending = self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                self._write_pending(pending)
            finally:
                self._queue.task_done()

    def _writer(self):
        while not self._stop.is_set() or not self._queue.empty():
            try:
                with self._write_lock:
                    self._drain_locked()
                    if self._stream:
                        self._stream.flush()
            except Exception as exc:
                self._failure(exc)
            self._stop.wait(self._flush_interval)
        with self._write_lock:
            if self._stream:
                try:
                    self._stream.flush()
                    self._stream.close()
                except Exception as exc:
                    self._failure(exc)
                self._stream = None

    def flush(self, timeout=2):
        deadline = time.perf_counter() + max(0, timeout)
        if not self._idle.wait(max(0, deadline - time.perf_counter())):
            return False
        if not self._write_lock.acquire(timeout=max(0, deadline - time.perf_counter())):
            return False
        try:
            if self._stream:
                self._stream.flush()
            return self._write_errors == 0
        except Exception as exc:
            self._failure(exc)
            return False
        finally:
            self._write_lock.release()

    def status(self):
        with self._count_lock:
            return {"schemaVersion": SCHEMA_VERSION, "enabled": not self._failed_start, "captureMode": "full", "runId": self.run_id,
                    "runDir": str(self._run_dir) if self._run_dir else None, "directory": str(self._run_dir) if self._run_dir else None,
                    "files": list(self._files), "closed": self._closed,
                    "pendingEvents": self._pending, "pendingBytes": self._pending_bytes, "droppedEvents": self._lost,
                    "writeErrors": self._write_errors, "lastError": self._last_error, "synchronousFallbacks": self._fallbacks,
                    "synchronousFallbackMs": round(self._fallback_ms, 6),
                    "recordingWorkMs": round(self._recording_ms, 6), "writeWorkMs": round(self._write_ms, 6)}

    def close(self):
        with self._emit_lock:
            if self._closed:
                return
            if self._worker:
                self._emit("trace.close", self.status())
            self._closed = True
            self._stop.set()
        if self._worker and self._worker is not threading.current_thread():
            self._worker.join(timeout=5)


class NullTraceCapsule:
    def receive(self, tool, args, request_id=None):
        return None

    @contextmanager
    def begin(self, token):
        yield token

    @contextmanager
    def span(self, name, **metadata):
        yield None

    def end(self, token, result=None, error=None):
        pass

    def event(self, name, **metadata):
        pass

    def status(self):
        return {"schemaVersion": SCHEMA_VERSION, "enabled": False, "runDir": None, "directory": None, "pendingEvents": 0, "droppedEvents": 0, "writeErrors": 0}

    def flush(self, timeout=2):
        return True

    def close(self):
        pass


def disabled():
    return NullTraceCapsule()
