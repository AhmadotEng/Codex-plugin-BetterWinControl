"""Local authenticated bridge for the optional Zen adapter. No installation side effects.

Root imports BrokerServer, calls accept() with a strict authorize_client(pid) callback,
then owns the resulting BrokerConnection and its target/session lifecycle.
"""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import struct
import threading
import time
import uuid

DOMAIN = "BetterWinControl/ZenNative/v1"
EXTENSION_ID = "zen-companion@betterwincontrol.local"
MAX_MESSAGE = 1_000_000
OPS = {"capabilities", "pair_candidates", "bind", "observe", "input", "verify", "pause", "resume", "stop"}


class AdapterResponseError(RuntimeError):
    """A bounded extension error; does not itself imply transport failure."""


def encode(value):
    body = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_MESSAGE:
        raise ValueError("message_too_large")
    return body


def read_exact(stream, length):
    chunks = bytearray()
    while len(chunks) < length:
        piece = stream.read(length - len(chunks))
        if not piece:
            if not chunks:
                raise EOFError("connection_closed")
            raise ValueError("truncated_message")
        chunks.extend(piece)
    return bytes(chunks)


def read_native(stream):
    size = struct.unpack("<I", read_exact(stream, 4))[0]
    if not 0 < size <= MAX_MESSAGE:
        raise ValueError("message_too_large")
    value = json.loads(read_exact(stream, size))
    if not isinstance(value, dict):
        raise ValueError("message_must_be_object")
    return value


def write_native(stream, value):
    body = encode(value)
    stream.write(struct.pack("<I", len(body)) + body)
    stream.flush()


class JsonStream:
    def __init__(self, stream):
        self.stream = stream
        self.write_lock = threading.Lock()
        self.read_buffer = bytearray()

    def send(self, value):
        body = encode(value)
        with self.write_lock:
            self.stream.write(body + b"\n")
            self.stream.flush()

    def read(self):
        while b"\n" not in self.read_buffer:
            chunk = self.stream.read(65536)
            if not chunk:
                raise EOFError("connection_closed")
            self.read_buffer.extend(chunk)
            if len(self.read_buffer) > MAX_MESSAGE + 1 and b"\n" not in self.read_buffer:
                raise ValueError("invalid_frame")
        end = self.read_buffer.index(b"\n")
        body = bytes(self.read_buffer[:end])
        del self.read_buffer[:end + 1]
        if len(body) > MAX_MESSAGE:
            raise ValueError("invalid_frame")
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError("message_must_be_object")
        return value

    def close(self):
        self.stream.close()


def proof(secret, role, pipe_name, challenge, nonce):
    if len(secret) != 32 or role not in {"client", "server"}:
        raise ValueError("invalid_auth_material")
    body = "\0".join([DOMAIN, role, pipe_name.lower(), challenge, nonce]).encode("ascii")
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def nonce_value(value):
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("invalid_auth_nonce")
    try:
        bytes.fromhex(value)
    except ValueError:
        raise ValueError("invalid_auth_nonce") from None
    return value


def authenticate_client(channel, secret, pipe_name):
    request = channel.read()
    if request.get("v") != 1 or request.get("type") != "auth_challenge" or request.get("domain") != DOMAIN:
        raise ValueError("broker_authentication_required")
    challenge = nonce_value(request.get("challenge"))
    nonce = secrets.token_hex(32)
    channel.send({"v": 1, "type": "auth_response", "nonce": nonce,
                  "proof": proof(secret, "client", pipe_name, challenge, nonce)})
    response = channel.read()
    expected = proof(secret, "server", pipe_name, challenge, nonce)
    if response.get("type") != "auth_ok" or not isinstance(response.get("proof"), str) or not hmac.compare_digest(response["proof"], expected):
        raise ValueError("broker_authentication_failed")


def authenticate_server(channel, secret, pipe_name):
    challenge = secrets.token_hex(32)
    channel.send({"v": 1, "type": "auth_challenge", "domain": DOMAIN, "challenge": challenge})
    response = channel.read()
    nonce = nonce_value(response.get("nonce"))
    expected = proof(secret, "client", pipe_name, challenge, nonce)
    if response.get("v") != 1 or response.get("type") != "auth_response" or not isinstance(response.get("proof"), str) or not hmac.compare_digest(response["proof"], expected):
        raise ValueError("client_authentication_failed")
    channel.send({"v": 1, "type": "auth_ok", "proof": proof(secret, "server", pipe_name, challenge, nonce)})


def windows_only():
    if os.name != "nt":
        raise RuntimeError("windows_required")


def current_user_sid():
    windows_only()
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    token = wintypes.HANDLE()
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buf = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, buf, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wintypes.LPWSTR()
        advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return text.value
        finally:
            kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        kernel.CloseHandle(token)


def security_descriptor():
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    descriptor = ctypes.c_void_p()
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW("D:P(A;;GA;;;" + current_user_sid() + ")", 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    return descriptor


def restrict_file(path):
    """Apply a protected DACL granting only the current user access; caller owns path."""
    windows_only()
    descriptor = security_descriptor()
    try:
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
        if not advapi.SetFileSecurityW(str(path), 0x80000004, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        ctypes.WinDLL("kernel32").LocalFree(descriptor)


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def dpapi(data, decrypt=False):
    windows_only()
    raw = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    salt = DOMAIN.encode("ascii")
    entropy = (ctypes.c_ubyte * len(salt)).from_buffer_copy(salt)
    source, extra, dest = Blob(len(data), raw), Blob(len(salt), entropy), Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    # UI forbidden; user protection (not CRYPTPROTECT_LOCAL_MACHINE).
    if not fn(ctypes.byref(source), None, ctypes.byref(extra), None, None, 1, ctypes.byref(dest)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(dest.data, dest.size)
    finally:
        ctypes.WinDLL("kernel32").LocalFree(dest.data)


def write_config(path, pipe_name, secret):
    """Explicit setup API; never called merely by importing or starting the extension."""
    validate_pipe(pipe_name)
    if len(secret) != 32:
        raise ValueError("invalid_secret")
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    restrict_file(path.parent)
    if path.exists():
        raise FileExistsError("configuration_exists")
    body = {"v": 1, "pipe": pipe_name, "secretDpapi": base64.b64encode(dpapi(secret)).decode("ascii"), "extensionId": EXTENSION_ID}
    with path.open("x", encoding="utf-8") as stream:
        json.dump(body, stream)
    restrict_file(path)


def read_config(path):
    body = json.loads(Path(path).read_text(encoding="utf-8"))
    if body.get("v") != 1 or body.get("extensionId") != EXTENSION_ID:
        raise ValueError("invalid_config")
    validate_pipe(body["pipe"])
    secret = dpapi(base64.b64decode(body["secretDpapi"], validate=True), decrypt=True)
    if len(secret) != 32:
        raise ValueError("invalid_secret")
    return body["pipe"], secret


def validate_pipe(pipe_name):
    if not isinstance(pipe_name, str) or not pipe_name.startswith("\\\\.\\pipe\\BetterWinControl-Zen-") or len(pipe_name) > 240 or any(c in pipe_name[9:] for c in "\\/\r\n\0"):
        raise ValueError("invalid_local_pipe")


class WinPipeStream:
    """Independent overlapped reads/writes: the CRT serializes duplex pipe FileIO.

    Closing cancels pending operations. No foreground/input APIs are involved.
    """
    class Overlapped(ctypes.Structure):
        _fields_ = [("internal", ctypes.c_size_t), ("internal_high", ctypes.c_size_t),
                    ("offset", wintypes.DWORD), ("offset_high", wintypes.DWORD), ("event", wintypes.HANDLE)]

    def __init__(self, handle):
        self.handle, self.closed = handle, False
        self.lock, self.active = threading.Lock(), 0
        self.drained = threading.Event()
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        k = self.kernel
        k.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        k.CreateEventW.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
        for name in ["ReadFile", "WriteFile"]:
            getattr(k, name).argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]

    def _operation(self, kind, data=None, timeout=None):
        with self.lock:
            if self.closed:
                raise EOFError("connection_closed")
            self.active += 1
        event = self.kernel.CreateEventW(None, True, False, None)
        overlap = self.Overlapped(event=event)
        size = data if kind == "read" else len(data) if kind == "write" else 0
        buf = ctypes.create_string_buffer(size) if kind == "read" else ctypes.create_string_buffer(data) if kind == "write" else None
        count = wintypes.DWORD()
        end = time.monotonic() + timeout if timeout else None
        pending = False
        try:
            if not event:
                raise ctypes.WinError(ctypes.get_last_error())
            if kind == "connect":
                self.kernel.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
                ok = self.kernel.ConnectNamedPipe(self.handle, ctypes.byref(overlap))
            else:
                fn = self.kernel.ReadFile if kind == "read" else self.kernel.WriteFile
                ok = fn(self.handle, buf, size, ctypes.byref(count), ctypes.byref(overlap))
            error = ctypes.get_last_error() if not ok else 0
            if kind == "connect" and error == 535:
                return None  # Client connected before ConnectNamedPipe.
            if not ok and error != 997:
                if error in [109, 232, 233, 995]:
                    raise EOFError("connection_closed")
                raise ctypes.WinError(error)
            pending = not ok
            while pending:
                if self.closed or (end is not None and time.monotonic() >= end):
                    self.kernel.CancelIoEx(self.handle, ctypes.byref(overlap))
                    self.kernel.WaitForSingleObject(event, 0xFFFFFFFF)
                    pending = False
                    if self.closed:
                        raise EOFError("connection_closed")
                    raise TimeoutError("pipe_operation_timeout")
                result = self.kernel.WaitForSingleObject(event, 50)
                if result == 0:
                    pending = False
                elif result != 258:
                    raise ctypes.WinError(ctypes.get_last_error())
            if not self.kernel.GetOverlappedResult(self.handle, ctypes.byref(overlap), ctypes.byref(count), False):
                error = ctypes.get_last_error()
                if error in [109, 232, 233, 995]:
                    raise EOFError("connection_closed")
                raise ctypes.WinError(error)
            return buf.raw[:count.value] if kind == "read" else count.value
        finally:
            if pending:
                self.kernel.CancelIoEx(self.handle, ctypes.byref(overlap))
                self.kernel.WaitForSingleObject(event, 0xFFFFFFFF)
            if event:
                self.kernel.CloseHandle(event)
            with self.lock:
                self.active -= 1
                if self.closed and not self.active and self.handle is not None:
                    self.kernel.CloseHandle(self.handle)
                    self.handle = None
                    self.drained.set()

    def read(self, count):
        return self._operation("read", count)

    def write(self, data):
        offset = 0
        deadline = time.monotonic() + 5
        while offset < len(data):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("pipe_write_timeout")
            count = self._operation("write", data[offset:offset+65536], timeout=remaining)
            if not count:
                raise EOFError("connection_closed")
            offset += count
        return offset

    def flush(self):
        pass  # FlushFileBuffers would wait for the other process to consume data.

    def close(self):
        with self.lock:
            if not self.closed:
                self.closed = True
                self.kernel.CancelIoEx(self.handle, None)
                if not self.active:
                    self.kernel.CloseHandle(self.handle)
                    self.handle = None
                    self.drained.set()
        # A caller immediately reopening the fixed pipe must not race an old
        # overlapped read that still owns its first-instance handle.
        self.drained.wait(.25)


def with_deadline(channel, callback, timeout=5):
    """Bound authentication; an unauthenticated peer cannot retain the connection."""
    timer = threading.Timer(timeout, channel.close)
    timer.daemon = True
    timer.start()
    try:
        return callback()
    finally:
        timer.cancel()


def trace_event(trace, name, **metadata):
    """Diagnostics are optional and must never change a transport outcome."""
    if trace is not None:
        try:
            trace.event(name, **metadata)
        except Exception:
            pass


class BrokerServer:
    """One local, current-user-only pipe. authorize_client(pid) is mandatory and fail-closed.

    The callback must inspect OS process ancestry/image/command line, and return peer
    metadata only for the expected host launched by the authorized browser. It must
    not trust PID/path assertions received over the connection.
    """
    def __init__(self, pipe_name, secret, authorize_client, *, trace=None):
        windows_only(); validate_pipe(pipe_name)
        if len(secret) != 32 or not callable(authorize_client):
            raise ValueError("authorization_callback_and_secret_required")
        self.pipe_name, self.secret, self.authorize_client = pipe_name, secret, authorize_client
        self.trace = trace
        self.handle = None
        self.stream = None
        self.closed = False
        self.lock = threading.Lock()

    def close(self):
        with self.lock:
            self.closed = True
            stream = self.stream
        if stream is not None:
            stream.close()

    def accept(self, on_event=None, timeout=30):
        with self.lock:
            if self.closed:raise EOFError("server_closed")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        class Attributes(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", wintypes.BOOL)]
        descriptor = security_descriptor()
        attrs = Attributes(ctypes.sizeof(Attributes), descriptor, False)
        kernel.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Attributes)]
        kernel.CreateNamedPipeW.restype = wintypes.HANDLE
        try:
            # Duplex, first-instance protection; byte mode and reject remote clients.
            handle = kernel.CreateNamedPipeW(self.pipe_name, 3 | 0x00080000 | 0x40000000, 0x8, 1, 65536, 65536, 5000, ctypes.byref(attrs))
        finally:
            kernel.LocalFree(descriptor)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        self.handle = handle
        stream = WinPipeStream(handle)
        with self.lock:
            if self.closed:
                stream.close();self.handle=None
                raise EOFError("server_closed")
            self.stream = stream
        try:
            stream._operation("connect", timeout=timeout)
            pid = wintypes.ULONG()
            kernel.GetNamedPipeClientProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
            if not kernel.GetNamedPipeClientProcessId(handle, ctypes.byref(pid)):
                raise ctypes.WinError(ctypes.get_last_error())
            peer = self.authorize_client(pid.value)
            if not peer:
                raise PermissionError("native_host_process_not_authorized")
            self.handle = None  # stream owns it now.
            channel = JsonStream(stream)
            try:
                with_deadline(channel, lambda: authenticate_server(channel, self.secret, self.pipe_name))
                connection = BrokerConnection(channel, {"clientPid": pid.value, **peer}, on_event, trace=self.trace)
                self.stream = None  # The accepted connection now owns the stream.
                return connection
            except BaseException:
                channel.close(); raise
        finally:
            if self.handle is not None:
                stream.close(); self.handle = None


class BrokerConnection:
    def __init__(self, channel, peer, on_event=None, *, trace=None):
        self.channel, self.peer, self.on_event = channel, peer, on_event
        self.trace, self.connection_id = trace, uuid.uuid4().hex
        self.pending, self.lock, self.closed = {}, threading.Lock(), False
        self.hello = threading.Event()
        self._trace("zen.transport.authenticated", peer=peer)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _trace(self, name, **metadata):
        trace_event(self.trace, name, connectionId=self.connection_id, **metadata)

    def _read(self):
        try:
            while True:
                message = self.channel.read()
                self._trace("zen.transport.received", message=message, peer=self.peer)
                if message.get("v") != 1:
                    raise ValueError("invalid_protocol_version")
                if message.get("type") == "hello":
                    if message.get("extensionId") != EXTENSION_ID:
                        raise ValueError("wrong_extension")
                    self.hello.set()
                elif message.get("type") == "reply":
                    with self.lock:
                        item = self.pending.get(message.get("id"))
                        if item:
                            item[1].append(message); item[0].set()
                elif message.get("type") == "event":
                    if not self.hello.is_set():
                        raise ValueError("hello_required")
                    if self.on_event:
                        self.on_event(message, self.peer)
                else:
                    raise ValueError("invalid_message_type")
        except BaseException as exc:
            self._trace("zen.transport.reader_error", errorType=type(exc).__name__, error=str(exc))
            self.close()

    def request(self, session_id, epoch, op, args=None, timeout=5):
        if op not in OPS or not 0 < timeout <= 30:
            raise ValueError("invalid_request")
        if not self.hello.wait(timeout) or self.closed:
            raise RuntimeError("extension_not_connected")
        ident = uuid.uuid4().hex
        event, replies = threading.Event(), []
        with self.lock:
            if self.closed:
                raise RuntimeError("connection_closed")
            self.pending[ident] = (event, replies)
        try:
            message={"v":1,"id":ident,"sessionId":session_id,"epoch":epoch,"deadlineUnixMs":int(time.time()*1000+timeout*1000),"op":op,"args":args or {}}
            self._trace("zen.transport.request", message=message)
            self.channel.send(message)
            self._trace("zen.transport.sent", requestId=ident, sessionId=session_id, epoch=epoch)
            if not event.wait(timeout):
                self.close()
                raise TimeoutError("adapter_timeout_outcome_unknown_connection_revoked")
            response = replies[0]
            if "error" in response:
                code = response["error"].get("code", "adapter_error")
                if not isinstance(code, str) or not 1 <= len(code) <= 100 or not code.replace("_", "").isalnum():
                    code = "adapter_error"
                raise AdapterResponseError(code)
            if response.get("sessionId") != session_id or response.get("epoch") != epoch:
                self.close(); raise RuntimeError("response_session_mismatch")
            return response["result"]
        except AdapterResponseError as exc:
            self._trace("zen.transport.request_error", requestId=ident, sessionId=session_id, epoch=epoch,
                        errorType=type(exc).__name__, error=str(exc))
            raise
        except BaseException as exc:
            self._trace("zen.transport.request_error", requestId=ident, sessionId=session_id, epoch=epoch,
                        errorType=type(exc).__name__, error=str(exc))
            self.close()
            raise
        finally:
            with self.lock:
                self.pending.pop(ident, None)

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            pending_ids = list(self.pending)
            for event, replies in self.pending.values():
                if not replies:
                    replies.append({"error":{"code":"connection_closed_outcome_unknown"}})
                event.set()
        self._trace("zen.transport.closed", pendingRequestIds=pending_ids)
        try:
            self.channel.close()
        except OSError:
            pass


def connect_pipe(pipe_name, timeout=5):
    windows_only(); validate_pipe(pipe_name)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    deadline = time.monotonic() + timeout
    while True:
        try:
            handle = kernel.CreateFileW(pipe_name, 0xC0000000, 0, None, 3, 0x40000000, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            return JsonStream(WinPipeStream(handle))
        except OSError:
            if time.monotonic() >= deadline:
                raise TimeoutError("broker_unavailable") from None
            time.sleep(0.05)
