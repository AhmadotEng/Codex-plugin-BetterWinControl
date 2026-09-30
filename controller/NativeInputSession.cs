using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json;

namespace BackgroundControl;

public sealed class NativeInputSession : IDisposable
{
    private readonly WindowController controller;
    private NativeInputClient? client;
    private FrameTicket? ticket;
    private Task<object>? teardown;
    private readonly object nativeLifecycle = new();
    private TaskCompletionSource<object>? pendingAttach;
    private long generation;
    private int inputInFlight, heldState;
    private InputIsolationMonitor? isolation;
    private string? isolationFailure;
    public event Action<double?, double?>? PointerAcknowledged;
    private sealed record FrameTicket(string Id, long Hwnd, long Epoch, Geometry Geometry, Rect Source, long FrameVersion,
        DateTimeOffset Expires, int Width, int Height);
    [StructLayout(LayoutKind.Sequential)]
    private record struct Rect(int Left, int Top, int Right, int Bottom);
    [StructLayout(LayoutKind.Sequential)]
    private struct Point { public int X, Y; }
    private sealed record Geometry(Rect Window, Rect Frame, Rect Client, int ClientX, int ClientY, uint Pid, uint Thread, long Start, uint Dpi);

    public NativeInputSession(WindowController controller)
    {
        this.controller = controller;
        controller.Revoked += Cancel;
        isolation = new InputIsolationMonitor(() => controller.TargetHwnd, reason =>
        {
            if (reason == "selected_window_destroyed") { isolationFailure = reason; controller.Stop(); }
            else if (!controller.Paused && (Volatile.Read(ref inputInFlight) != 0 || Volatile.Read(ref heldState) != 0))
            { isolationFailure = reason; controller.Pause(); }
        });
    }

    public object Capabilities()
    {
        string? helper = null;
        try { if (controller.TargetHwnd != 0) helper = NativeInputClient.HelperPath(controller.TargetHwnd); } catch { }
        return new {
            semantic = new { available = true, backend = "UIA", effectVerification = "per-action readback where available" },
            input = new {
                backend = "application-local synchronous Win32 dispatch + MinHook virtual state",
                helperBuilt = helper is not null && System.IO.File.Exists(helper),
                attached = Volatile.Read(ref client) is not null, experimental = true,
                operations = new[] { "move", "hover", "click", "double_click", "button_down", "button_up", "wheel", "drag", "text", "key_down", "key_up", "shortcut", "release" },
                coordinates = "fresh capture physical pixels, selected client area only",
                verifiedApplicationCompatibility = false,
                unsupported = new[] { "cross-application drag/drop", "raw input", "IME composition", "nested native menu loops", "cross-thread input contexts", "protected processes", "secure desktop" },
                commandDeliveryIsNotEffectVerification = true,
                sharedPhysicalInputInjection = false
            },
            browser = new { adapterImplemented = true, verifiedInZen = false,
                status = "Signed companion, native-host registration and verified window pairing required; use browser tool for readiness." }
        };
    }

    public object CaptureToken(CaptureSnapshot shot)
    {
        ticket = null;
        if (!shot.Active || shot.Paused || shot.IsClosed || shot.AgeMs < 0 || shot.AgeMs > 1000 || shot.Width < 1 || shot.Height < 1)
            return new { available = false, reason = "fresh_capture_required" };
        var hwnd = controller.TargetHwnd;
        if (hwnd == 0 || IsIconic(new nint(hwnd))) return new { available = false, reason = "target_not_available" };
        var geometry = ReadGeometry(hwnd);
        // WGC frame extents must match verified native bounds exactly. Never guess a
        // nonclient inset from an image and risk clicking a different control.
        Rect source;
        if (Matches(geometry.Frame, shot)) source = geometry.Frame;
        else if (Matches(geometry.Window, shot)) source = geometry.Window;
        else return new { available = false, reason = "capture_extent_unverified", shot.Width, shot.Height };
        ticket = new(Guid.NewGuid().ToString("N"), hwnd, controller.SessionEpoch, geometry, source, shot.FrameVersion,
            DateTimeOffset.UtcNow.AddSeconds(10), shot.Width, shot.Height);
        return new { available = true, frameId = ticket.Id, ticket.FrameVersion, ticket.Expires, ticket.Width, ticket.Height,
            coordinates = "capture physical pixels", hwnd, pid = geometry.Pid,
            clientBounds = new { x = geometry.ClientX - source.Left, y = geometry.ClientY - source.Top,
                width = geometry.Client.Right, height = geometry.Client.Bottom } };
    }

    public object Input(string frameId, JsonElement steps, int deadlineMs = 8000)
    {
        if (steps.ValueKind != JsonValueKind.Array || steps.GetArrayLength() is < 1 or > 128)
            throw new InvalidOperationException("invalid_steps: 1..128 steps required");
        var plan = Expand(steps); // Validate the ENTIRE sequence before any input.
        var frame = ticket;
        if (frame is null || frame.Id != frameId) throw new InvalidOperationException("stale_frame: observe again");
        foreach (var command in plan)
        {
            if (!command.TryGetValue("x", out var xValue)) continue;
            int x = (int)xValue!, y = (int)command["y"]!;
            long cx = (long)x + frame.Source.Left - frame.Geometry.ClientX;
            long cy = (long)y + frame.Source.Top - frame.Geometry.ClientY;
            if (cx < 0 || cy < 0 || cx >= frame.Geometry.Client.Right || cy >= frame.Geometry.Client.Bottom)
                throw new InvalidOperationException("coordinates_outside_selected_client");
            command["x"] = (int)cx; command["y"] = (int)cy;
        }
        var guard = controller.CaptureActiveSessionGuard();
        var runGeneration = Volatile.Read(ref generation);
        var watch = Stopwatch.StartNew();
        var beforeForeground = GetForegroundWindow();
        if (beforeForeground == new nint(frame.Hwnd)) throw new InvalidOperationException("background_target_required");
        isolationFailure = null;
        void Check()
        {
            if (isolationFailure is not null) throw new InvalidOperationException("input_isolation_lost: " + isolationFailure);
            guard();
            if (runGeneration != Volatile.Read(ref generation)) throw new InvalidOperationException("input_cancelled");
            if (watch.ElapsedMilliseconds > Math.Clamp(deadlineMs, 100, 8000)) throw new InvalidOperationException("input_deadline");
            if (DateTimeOffset.UtcNow > frame.Expires || controller.SessionEpoch != frame.Epoch ||
                controller.TargetHwnd != frame.Hwnd) throw new InvalidOperationException("stale_frame");
            var now = ReadGeometry(frame.Hwnd);
            if (now != frame.Geometry || IsIconic(new nint(frame.Hwnd)))
                throw new InvalidOperationException("stale_geometry_or_identity: observe again");
            // Physical user switching between other apps is allowed; automatic activation
            // of the target is never treated as acceptable delivery.
            if (beforeForeground != new nint(frame.Hwnd) && GetForegroundWindow() == new nint(frame.Hwnd))
                throw new InvalidOperationException("foreground_isolation_lost");
        }
        Check();
        NativeInputClient? active = Volatile.Read(ref client);
        var results = new List<JsonElement>();
        Volatile.Write(ref inputInFlight, 1);
        try
        {
            if (active is null)
            {
                active = StartTrackedClient(frame.Hwnd, runGeneration);
                var capabilities = active.Request(new { op = "capabilities" });
                Check();
            }
            foreach (var command in plan)
            {
                Check();
                var ack = active.Request(command);
                results.Add(ack);
                Volatile.Write(ref heldState,
                    (ack.TryGetProperty("heldKeys", out var keys) && keys.GetArrayLength() > 0) ||
                    (ack.TryGetProperty("heldButtons", out var buttons) && buttons.GetArrayLength() > 0) ? 1 : 0);
                if (ack.TryGetProperty("pointer", out var pointer) && pointer.TryGetProperty("x", out var px) && pointer.TryGetProperty("y", out var py))
                    PointerAcknowledged?.Invoke(
                        (px.GetDouble() + frame.Geometry.ClientX - frame.Source.Left) / frame.Width,
                        (py.GetDouble() + frame.Geometry.ClientY - frame.Source.Top) / frame.Height);
                Check();
            }
            ticket = null; // Fresh capture for the next batch, including key-only sequences.
            return new { delivered = results.Count, effectVerified = false, backend = "native_virtual_input",
                results, foregroundBefore = beforeForeground.ToInt64(), foregroundAfter = GetForegroundWindow().ToInt64(),
                verification = "Observe the target effect; acknowledgement alone does not prove application behavior." };
        }
        catch (Exception ex)
        {
            ex.Data["deliveredCommands"] = results.Count;
            ex.Data["effectVerified"] = false;
            if (active is not null && Volatile.Read(ref client) is null) teardown = active.Revoke();
            Cancel();
            throw;
        }
        finally { Volatile.Write(ref inputInFlight, 0); }
    }

    private NativeInputClient StartTrackedClient(long hwnd, long expectedGeneration)
    {
        RequireDetached(0); // Resume/retry cannot replace evidence from a pending old helper.
        var pending = new TaskCompletionSource<object>(TaskCreationOptions.RunContinuationsAsynchronously);
        lock (nativeLifecycle)
        {
            if (expectedGeneration != Volatile.Read(ref generation)) throw new InvalidOperationException("input_cancelled");
            pendingAttach = pending; // Visible to Stop BEFORE a child process can be started.
        }
        NativeInputClient? created = null;
        try
        {
            created = new NativeInputClient(hwnd);
            bool cancelled;
            lock (nativeLifecycle)
            {
                cancelled = expectedGeneration != Volatile.Read(ref generation);
                if (!cancelled) client = created; // Publish before the capabilities handshake.
                if (!cancelled && ReferenceEquals(pendingAttach, pending)) pendingAttach = null;
            }
            if (cancelled)
            {
                var cleanup = created.Revoke();
                lock (nativeLifecycle)
                {
                    teardown = cleanup;
                    if (ReferenceEquals(pendingAttach, pending)) pendingAttach = null;
                }
                _ = cleanup.ContinueWith(t => pending.TrySetResult(t.IsCompletedSuccessfully ? t.Result :
                    new { teardownVerified = false, status = "attachment_cleanup_unverified" }));
                throw new InvalidOperationException("attachment_cancelled");
            }
            pending.TrySetResult(new { teardownVerified = false, status = "helper_attached" });
            return created;
        }
        catch
        {
            if (created is null) pending.TrySetResult(new { teardownRequired = false, status = "helper_not_started" });
            lock (nativeLifecycle) if (ReferenceEquals(pendingAttach, pending)) pendingAttach = null;
            throw;
        }
    }

    private static List<Dictionary<string, object?>> Expand(JsonElement steps)
    {
        var result = new List<Dictionary<string, object?>>();
        void Add(string op, params (string Key, object? Value)[] args)
        { var d = new Dictionary<string, object?> { ["op"] = op }; foreach (var p in args) d[p.Key] = p.Value; result.Add(d); }
        foreach (var s in steps.EnumerateArray())
        {
            var op = s.GetProperty("type").GetString();
            int Number(string key) => s.GetProperty(key).GetInt32();
            string Button() { var b = s.TryGetProperty("button", out var p) ? p.GetString()! : "left";
                return b is "left" or "right" or "middle" ? b : throw new InvalidOperationException("invalid_button"); }
            int Key(JsonElement e) { var k = e.GetInt32(); return k is > 0 and < 256 ? k : throw new InvalidOperationException("invalid_virtual_key"); }
            void Move() => Add("move", ("x", Number("x")), ("y", Number("y")));
            switch (op)
            {
                case "move": case "hover": Move(); break;
                case "double_click": Move(); Add("double_click", ("button", Button())); break;
                case "click":
                    Move();
                    Add("button", ("button", Button()), ("down", true)); Add("button", ("button", Button()), ("down", false));
                    break;
                case "button_down": case "button_up": Add("button", ("button", Button()), ("down", op == "button_down")); break;
                case "wheel":
                    int delta = Number("delta");
                    if (Math.Abs((long)delta) > 12000) throw new InvalidOperationException("wheel_out_of_range");
                    Add("wheel", ("delta", delta)); break;
                case "text":
                    var text = s.GetProperty("text").GetString()!;
                    if (text.Length > 2000 || text.Contains('\0')) throw new InvalidOperationException("text_out_of_range: maximum 2000 UTF-16 units per step");
                    Add("text", ("text", text)); break;
                case "key_down": case "key_up": Add("key", ("vk", Key(s.GetProperty("vk"))), ("down", op == "key_down")); break;
                case "shortcut":
                    bool named = s.TryGetProperty("keysym", out var keysym);
                    if (named && s.TryGetProperty("keys", out _)) throw new InvalidOperationException("ambiguous_shortcut: supply keysym OR keys");
                    var keys = named ? KeyChordParser.ParseChord(keysym.GetString()!) :
                        s.GetProperty("keys").EnumerateArray().Select(Key).ToArray();
                    if (keys.Length is < 1 or > 8 || keys.Distinct().Count() != keys.Length) throw new InvalidOperationException("invalid_shortcut");
                    foreach (var key in keys) Add("key", ("vk", key), ("down", true));
                    foreach (var key in keys.Reverse()) Add("key", ("vk", key), ("down", false));
                    break;
                case "drag":
                    var path = s.GetProperty("path").EnumerateArray().ToArray();
                    if (path.Length is < 2 or > 64) throw new InvalidOperationException("invalid_drag_path");
                    Add("move", ("x", path[0].GetProperty("x").GetInt32()), ("y", path[0].GetProperty("y").GetInt32()));
                    Add("button", ("button", Button()), ("down", true));
                    foreach (var point in path.Skip(1)) Add("move", ("x", point.GetProperty("x").GetInt32()), ("y", point.GetProperty("y").GetInt32()));
                    Add("button", ("button", Button()), ("down", false)); break;
                case "release": Add("release"); break;
                default: throw new InvalidOperationException("unsupported_input_step");
            }
        }
        if (result.Count > 512) throw new InvalidOperationException("expanded_sequence_too_large");
        return result;
    }

    public void Cancel()
    {
        Interlocked.Increment(ref generation);
        ticket = null;
        Volatile.Write(ref heldState, 0);
        lock (nativeLifecycle)
        {
            var old = Interlocked.Exchange(ref client, null);
            if (old is not null) teardown = old.Revoke();
            else if (pendingAttach is not null) teardown = pendingAttach.Task;
        }
        PointerAcknowledged?.Invoke(null, null);
    }
    public void InvalidateFrame() => ticket = null;
    public void RequireDetached(int waitMs = 4500)
    {
        var status = JsonSerializer.SerializeToElement(TeardownStatus(waitMs));
        if (status.TryGetProperty("teardownRequired", out var required) && !required.GetBoolean()) return;
        if (status.TryGetProperty("hooksRemoved", out var hooks) && hooks.GetBoolean() &&
            status.TryGetProperty("moduleUnloaded", out var module) && module.GetBoolean()) return;
        throw new InvalidOperationException("target_transition_teardown_pending: previous helper removal is not verified");
    }
    public object TeardownStatus(int waitMs = 0)
    {
        Task<object>? work;
        lock (nativeLifecycle) work = pendingAttach?.Task ?? teardown;
        if (work is null) return new { nativeHelperAttached = false, teardownRequired = false };
        if (waitMs > 0) work.Wait(Math.Min(waitMs, 5000));
        return work.IsCompletedSuccessfully ? work.Result : new { teardownVerified = false, status = "detach_pending" };
    }
    public void Dispose() { controller.Revoked -= Cancel; Cancel(); isolation?.Dispose(); }
    private static bool Matches(Rect r, CaptureSnapshot s) => r.Right - r.Left == s.Width && r.Bottom - r.Top == s.Height;
    private static Geometry ReadGeometry(long hwnd)
    {
        var h = new nint(hwnd);
        var thread = GetWindowThreadProcessId(h, out var pid);
        if (thread == 0 || !GetWindowRect(h, out var window) || !GetClientRect(h, out var client))
            throw new InvalidOperationException("target_geometry_unavailable");
        var p = new Point();
        if (!ClientToScreen(h, ref p)) throw new InvalidOperationException("target_geometry_unavailable");
        var frame = DwmGetWindowAttribute(h, 9, out Rect dwm, Marshal.SizeOf<Rect>()) == 0 ? dwm : window;
        using var process = Process.GetProcessById((int)pid);
        var dpi = GetDpiForWindow(h);
        if (dpi == 0) throw new InvalidOperationException("target_dpi_unavailable");
        return new(window, frame, client, p.X, p.Y, pid, thread, process.StartTime.ToUniversalTime().Ticks, dpi);
    }
    [DllImport("user32.dll")] private static extern nint GetForegroundWindow();
    [DllImport("user32.dll")] private static extern bool GetWindowRect(nint hwnd, out Rect rect);
    [DllImport("user32.dll")] private static extern bool GetClientRect(nint hwnd, out Rect rect);
    [DllImport("user32.dll")] private static extern bool ClientToScreen(nint hwnd, ref Point point);
    [DllImport("user32.dll")] private static extern bool IsIconic(nint hwnd);
    [DllImport("user32.dll")] private static extern uint GetDpiForWindow(nint hwnd);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(nint hwnd, out uint pid);
    [DllImport("dwmapi.dll")] private static extern int DwmGetWindowAttribute(nint hwnd, uint attr, out Rect value, int size);
}
