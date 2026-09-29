"""Exercise MCP against a process-owned WPF fixture; never target user apps.

This is a bounded protocol/UIA/capture/lifecycle test, not a complete feature-parity
claim. It injects no global keyboard or pointer input. Build products and reports
stay under this tests directory. Run with Python 3.11+ on Windows.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import struct
import subprocess
import sys
import threading
import time
import traceback
import zlib


TESTS = Path(__file__).resolve().parent
PLUGIN = TESTS.parent
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class JsonProcess:
    def __init__(self, command: list[str], *, env: dict[str, str] | None = None):
        self.proc = subprocess.Popen(
            command, cwd=PLUGIN, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", bufsize=1, creationflags=CREATE_NO_WINDOW,
        )
        self.lines: queue.Queue[dict] = queue.Queue()
        self.stderr: list[str] = []
        self.non_json: list[str] = []
        self.next_id = 0
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self):
        assert self.proc.stdout
        for line in self.proc.stdout:
            try:
                self.lines.put(json.loads(line))
            except json.JSONDecodeError:
                self.non_json.append(line.rstrip())

    def _read_stderr(self):
        assert self.proc.stderr
        for line in self.proc.stderr:
            self.stderr.append(line.rstrip())

    def send(self, message: dict):
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def receive(self, timeout: float = 20) -> dict:
        try:
            return self.lines.get(timeout=timeout)
        except queue.Empty as exc:
            raise AssertionError(
                f"Process {self.proc.pid} response timed out; exit={self.proc.poll()}; "
                f"stderr={self.stderr[-8:]}; non_json={self.non_json[-4:]}"
            ) from exc

    def rpc(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        request_id = self.next_id
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            response = self.receive(max(0.1, deadline - time.monotonic()))
            if response.get("id") == request_id:
                return response
            # A server request cannot be silently accepted by a test client.
            if "method" in response and "id" in response:
                self.send({"jsonrpc": "2.0", "id": response["id"],
                           "error": {"code": -32601, "message": "Test client does not approve server requests"}})
        raise AssertionError(f"No response for {method}")

    def tool(self, name: str, arguments: dict | None = None, *, expect_error: bool = False):
        envelope = self.rpc("tools/call", {"name": name, "arguments": arguments or {}})
        if "error" in envelope:
            assert expect_error, f"Unexpected RPC error for {name}: {envelope}"
            return envelope, envelope
        result = envelope.get("result", {})
        is_error = bool(result.get("isError"))
        assert is_error == expect_error, f"{name}: expected error={expect_error}; got {result}"
        data = result.get("structuredContent")
        if data is None:
            text_parts = [item.get("text", "") for item in result.get("content", []) if item.get("type") == "text"]
            try:
                data = json.loads("\n".join(text_parts))
            except json.JSONDecodeError:
                data = {"text": "\n".join(text_parts)}
        return data, result

    def rejected(self, name: str, arguments: dict | None, reason: str):
        data, result = self.tool(name, arguments, expect_error=True)
        assert reason in json.dumps(result).lower(), f"{name} failed for the wrong reason: {result}"
        return data

    def close(self):
        if self.proc.poll() is None:
            if self.proc.stdin:
                self.proc.stdin.close()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=5)


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def field(value: dict, *names):
    normalized = {key.replace("_", "").lower(): item for key, item in value.items()}
    for name in names:
        key = name.replace("_", "").lower()
        if key in normalized:
            return normalized[key]
    return None


def observation_id(data: dict) -> str:
    for item in walk(data):
        found = field(item, "observation_id", "observationId")
        if found is not None:
            return str(found)
    raise AssertionError(f"No observation ID: {data}")


def element(data: dict, automation_id: str) -> dict:
    for item in walk(data):
        if field(item, "automation_id", "automationId") == automation_id:
            assert field(item, "id", "element_id") is not None, item
            return item
    raise AssertionError(f"Fixture element {automation_id!r} missing: {data}")


def action_args(data: dict, automation_id: str, action: str, **extra):
    node = element(data, automation_id)
    return {"observation_id": observation_id(data),
            "element_id": str(field(node, "element_id", "id")), "action": action, **extra}


def png_info(mcp_result: dict, save_path: Path | None = None) -> dict:
    images = [item for item in mcp_result.get("content", []) if item.get("type") == "image"]
    assert images, "observe(include_screenshot=True) returned no MCP image"
    image = images[0]
    assert image.get("mimeType") == "image/png", image.get("mimeType")
    data = base64.b64decode(image["data"], validate=True)
    assert data.startswith(b"\x89PNG\r\n\x1a\n"), "Not a PNG"
    width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", data[16:29])
    assert width >= 100 and height >= 100, (width, height)
    assert depth == 8 and color in (2, 6) and interlace == 0, (depth, color, interlace)
    offset, compressed = 8, bytearray()
    while offset < len(data):
        size = struct.unpack(">I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + size]
        if kind == b"IDAT":
            compressed.extend(payload)
        offset += size + 12
    raw = zlib.decompress(compressed)
    channels, previous, colors = (3 if color == 2 else 4), bytearray(), set()
    pixels_hash = hashlib.sha256()
    visible_pixels = 0
    stride = width * channels
    assert len(raw) == height * (stride + 1), "Unexpected PNG scanline size"
    for row in range(height):
        start = row * (stride + 1)
        filter_type, scan = raw[start], bytearray(raw[start + 1:start + 1 + stride])
        for col in range(stride):
            left = scan[col - channels] if col >= channels else 0
            up = previous[col] if previous else 0
            upper_left = previous[col - channels] if previous and col >= channels else 0
            if filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = up
            elif filter_type == 3:
                predictor = (left + up) // 2
            elif filter_type == 4:
                estimate = left + up - upper_left
                distances = (abs(estimate - left), abs(estimate - up), abs(estimate - upper_left))
                predictor = (left, up, upper_left)[distances.index(min(distances))]
            else:
                assert filter_type == 0, filter_type
                predictor = 0
            scan[col] = (scan[col] + predictor) & 255
        colors.update(tuple(scan[col:col + 3]) for col in range(0, stride, channels))
        pixels_hash.update(scan)
        visible_pixels += width if channels == 3 else sum(scan[col + 3] > 0 for col in range(0, stride, channels))
        previous = scan
    assert len(colors) > 8, f"Capture appears blank or uniform ({len(colors)} colors)"
    assert visible_pixels > width * height // 2, "Capture is mostly transparent"
    if save_path:
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_bytes(data)
    return {"width": width, "height": height, "bytes": len(data), "distinctRGBColors": len(colors),
            "pixelSha256": pixels_hash.hexdigest()}


def build_fixture() -> Path:
    env = os.environ.copy()
    env.update({"DOTNET_CLI_HOME": str(TESTS / ".dotnet-home"),
                "NUGET_PACKAGES": str(TESTS / ".nuget" / "packages"),
                "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1"})
    result = subprocess.run(
        ["dotnet", "build", str(TESTS / "fixture" / "Fixture.csproj"), "-c", "Release",
         "--disable-build-servers", "-v", "quiet"],
        cwd=TESTS, env=env, text=True, encoding="utf-8", capture_output=True,
        timeout=120, creationflags=CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    path = TESTS / "fixture" / "bin" / "Release" / "net10.0-windows" / "WindowsBackgroundControlFixture.exe"
    assert path.is_file(), path
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", type=Path, default=PLUGIN / "scripts" / "mcp_server.py")
    parser.add_argument("--report", type=Path, default=TESTS / "results" / "integration.json")
    parser.add_argument("--build-only", action="store_true")
    args = parser.parse_args()
    report = {"scope": "Owned fixture MCP/UIA/capture/lifecycle subset", "checks": [], "passed": False,
              "startedAtUtc": datetime.now(timezone.utc).isoformat()}
    fixture = server = None

    def check(name: str, detail=None):
        report["checks"].append({"name": name, "passed": True, "detail": detail})
        print(f"PASS {name}", flush=True)

    try:
        assert os.name == "nt", "This integration fixture requires Windows"
        ctypes.windll.user32.GetForegroundWindow.restype = ctypes.c_void_p
        fixture_path = build_fixture()
        check("fixture builds")
        if args.build_only:
            report["passed"] = True
            return 0
        assert args.server.is_file(), f"MCP server not ready: {args.server}"
        report["testedArtifacts"] = {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (args.server, PLUGIN / "runtime" / "BackgroundControl.dll") if path.is_file()
        }
        fixture = JsonProcess([str(fixture_path)])
        ready = fixture.receive()
        assert ready.get("event") == "ready", ready
        assert ready["fixturePID"] == fixture.proc.pid, ready
        hwnd = int(ready["hwnd"])
        owner = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(owner))
        assert owner.value == fixture.proc.pid, "Refuse any window not owned by the fixture"
        report["fixture"] = {"pid": fixture.proc.pid, "hwnd": hwnd}
        check("fixture ownership verified")
        server = JsonProcess([sys.executable, "-u", str(args.server)])
        init = server.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                         "clientInfo": {"name": "owned-fixture-test", "version": "1.0"}})
        assert "result" in init and "protocolVersion" in init["result"], init
        server.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        check("MCP initialize", init["result"].get("serverInfo"))
        listing = server.rpc("tools/list")
        names = {tool["name"] for tool in listing["result"]["tools"]}
        expected = {"list_windows", "attach_window", "observe", "act", "pause", "resume", "stop", "state"}
        assert expected <= names, {"missing": sorted(expected - names), "actual": sorted(names)}
        check("MCP tools/list", sorted(names))
        missing = server.rpc("integration/unknown_method")
        assert missing.get("error", {}).get("code") == -32601, missing
        server.tool("integration_unknown_tool", expect_error=True)
        server.rejected("attach_window", {"hwnd": True}, "invalid_argument_type")
        server.rejected("observe", {"max_elements": 0}, "out_of_range")
        check("MCP invalid method and tool rejected")
        windows, _ = server.tool("list_windows")
        assert any(str(field(item, "hwnd")) == str(hwnd) for item in walk(windows)), windows
        server.tool("attach_window", {"hwnd": hwnd})
        check("owned window listed and attached")
        capture_deadline = time.monotonic() + 10
        screenshot = None
        while time.monotonic() < capture_deadline:
            observed, visual = server.tool("observe", {"max_elements": 100, "include_screenshot": True})
            element(observed, "fixture-input")
            element(observed, "fixture-apply")
            if any(item.get("type") == "image" for item in visual.get("content", [])):
                screenshot = png_info(visual, args.report.parent / "fixture-before.png")
                break
            time.sleep(0.2)
        assert screenshot, f"No captured frame after warmup: {observed}"
        check("UIA observation and actual nonblank screenshot", screenshot)
        stale = action_args(observed, "fixture-apply", "invoke")
        server.tool("observe", {"max_elements": 100, "include_screenshot": False})
        server.rejected("act", stale, "stale_observation")
        check("stale observation action rejected")
        observed, _ = server.tool("observe", {"max_elements": 100, "include_screenshot": False})
        foreground_before = ctypes.windll.user32.GetForegroundWindow()
        token = f"owned-fixture-{os.getpid()}"
        server.tool("act", action_args(observed, "fixture-input", "set_value", value=token))
        observed, _ = server.tool("observe", {"max_elements": 100, "include_screenshot": False})
        assert token in json.dumps(element(observed, "fixture-input")), observed
        server.tool("act", action_args(observed, "fixture-apply", "invoke"))
        applied = fixture.receive()
        assert applied.get("event") == "applied" and applied["count"] == 1 and applied["value"] == token, applied
        observed, _ = server.tool("observe", {"max_elements": 100, "include_screenshot": False})
        assert f"Applied 1: {token}" in json.dumps(element(observed, "fixture-status")), observed
        foreground_after = ctypes.windll.user32.GetForegroundWindow()
        assert foreground_before == foreground_after, "UIA action changed foreground window"
        check("set_value/invoke/readback without foreground change")
        capture_deadline = time.monotonic() + 10
        changed_screenshot = None
        while time.monotonic() < capture_deadline:
            observed, visual = server.tool("observe", {"max_elements": 100, "include_screenshot": True})
            changed_screenshot = png_info(visual, args.report.parent / "fixture-after.png")
            if changed_screenshot["pixelSha256"] != screenshot["pixelSha256"]:
                break
            time.sleep(0.2)
        assert changed_screenshot and changed_screenshot["pixelSha256"] != screenshot["pixelSha256"], "Capture did not update after UI changes"
        check("screenshot pixels update after fixture action", changed_screenshot)
        before_pause = action_args(observed, "fixture-apply", "invoke")
        server.tool("pause")
        server.rejected("act", before_pause, "paused")
        server.tool("resume")
        observed, _ = server.tool("observe", {"max_elements": 100, "include_screenshot": False})
        check("pause blocks actions and resume restores observation")
        before_stop = action_args(observed, "fixture-apply", "invoke")
        stop_result, _ = server.tool("stop")
        assert stop_result.get("targetRevoked") is True and stop_result.get("controllerExited") is True, stop_result
        server.rejected("act", before_stop, "stopped")
        server.rejected("observe", {"max_elements": 100, "include_screenshot": False}, "stopped")
        server.rejected("resume", {}, "stopped")
        check("stop revokes target and blocks implicit reattach")
        state, _ = server.tool("state")
        report["finalState"] = state
        assert state.get("controller", {}).get("attached") is False, state
        check("state remains readable after stop")
        assert fixture.lines.empty(), "Rejected actions unexpectedly changed fixture"
        report["passed"] = True
        return 0
    except Exception as exc:
        report["error"] = str(exc)
        report["traceback"] = traceback.format_exc()
        print(report["traceback"], file=sys.stderr, flush=True)
        return 1
    finally:
        if server:
            if server.proc.poll() is None:
                try:
                    server.tool("stop")
                except Exception:
                    pass
            server.close()
            report["serverStderr"] = server.stderr[-30:]
            report["serverNonJsonStdout"] = server.non_json[-10:]
        if fixture:
            if fixture.proc.poll() is None and fixture.proc.stdin:
                try:
                    fixture.proc.stdin.write("stop\n")
                    fixture.proc.stdin.flush()
                    fixture.proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            fixture.close()
            report["fixtureExited"] = fixture.proc.poll() is not None
        args.report.parent.mkdir(parents=True, exist_ok=True)
        report["completedAtUtc"] = datetime.now(timezone.utc).isoformat()
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"Report: {args.report}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
