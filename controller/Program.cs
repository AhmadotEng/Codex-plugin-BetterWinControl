using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Windows;
using System.Windows.Threading;
using System.Collections.Concurrent;

namespace BackgroundControl;

internal static class Program
{
    static readonly JsonSerializerOptions Json = new() { PropertyNamingPolicy = JsonNamingPolicy.CamelCase };
    static readonly object OutputLock = new();
    static void Reply(object value) { lock (OutputLock) { Console.WriteLine(JsonSerializer.Serialize(value, Json)); Console.Out.Flush(); } }
    [STAThread]
    static int Main(string[] args)
    {
        if (args.Length != 3 || args[0] != "--rpc" || args[1] != "--parent-pid" || !int.TryParse(args[2], out var parentPid)) return 2;
        Console.InputEncoding = new UTF8Encoding(false);
        Console.OutputEncoding = new UTF8Encoding(false);
        using var parent = Process.GetProcessById(parentPid);
        var parentStart = parent.StartTime.ToUniversalTime();
        var app = new Application { ShutdownMode = ShutdownMode.OnExplicitShutdown };
        WindowController? controller = null;
        CapturePreview? preview = null;
        preview = new CapturePreview(app.Dispatcher, command =>
        {
            if (controller is null) return;
            try
            {
                switch (command)
                {
                    case "pause": controller.Pause(); preview!.SetPaused(true); break;
                    case "resume": controller.Resume(); preview!.SetPaused(false); break;
                    case "stop": controller.Stop(); preview!.Stop(); app.Shutdown(); break;
                }
            }
            catch (Exception ex) { preview!.SetStatus(ex.Message); }
        });
        var parentWatch = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(500) };
        parentWatch.Tick += (_, _) =>
        {
            try
            {
                if (!parent.HasExited && parent.StartTime.ToUniversalTime() == parentStart) return;
            }
            catch { }
            controller?.Stop(); preview.Stop(); app.Shutdown();
        };
        parentWatch.Start();
        var worker = new Thread(() =>
        {
            try
            {
                using var local = new WindowController();
                using var input = new NativeInputSession(local);
                input.PointerAcknowledged += (x, y) => { if (!app.Dispatcher.HasShutdownStarted)
                    app.Dispatcher.BeginInvoke(() => preview.SetVirtualPointer(x, y)); };
                local.Revoked += () =>
                {
                    try
                    {
                        if (!app.Dispatcher.HasShutdownStarted) app.Dispatcher.BeginInvoke(() =>
                        { if (local.TargetHwnd == 0) preview.Stop(); else preview.SetPaused(local.Paused); });
                        Reply(new { notification = "revoked", sessionEpoch = local.SessionEpoch });
                    }
                    catch { }
                };
                using var clipboard = new ClipboardBridge(operation =>
                    app.Dispatcher.HasShutdownStarted || app.Dispatcher.HasShutdownFinished ? 0 : app.Dispatcher.Invoke(operation));
                controller = local;
                using var queue = new BlockingCollection<(string Line, long Generation)>();
                long generation = 0;
                var reader = new Thread(() =>
                {
                    try
                    {
                        string? incoming;
                        while ((incoming = Console.ReadLine()) is not null)
                        {
                            if (incoming.Length > 1_000_000) continue;
                            try
                            {
                                using var parsed = JsonDocument.Parse(incoming);
                                var root = parsed.RootElement;
                                var operation = root.GetProperty("method").GetString();
                                if (operation is "pause" or "stop")
                                {
                                    Interlocked.Increment(ref generation);
                                    object revoked = operation == "stop" ? local.Stop() : local.Pause();
                                    app.Dispatcher.Invoke(() => { if (operation == "stop") preview.Stop(); else preview.SetPaused(true); });
                                    Reply(new { id = root.GetProperty("id").Clone(), result = new { control = revoked, teardown = input.TeardownStatus(4500) } });
                                    if (operation == "stop") app.Dispatcher.BeginInvoke(() => app.Shutdown());
                                    continue;
                                }
                            }
                            catch (JsonException) { }
                            queue.Add((incoming, Volatile.Read(ref generation)));
                        }
                    }
                    finally { Interlocked.Increment(ref generation); local.Stop(); queue.CompleteAdding(); }
                }) { IsBackground = true, Name = "BackgroundControl-Cancellation" };
                reader.Start();
                foreach (var queued in queue.GetConsumingEnumerable())
                {
                    var line = queued.Line;
                    JsonElement? id = null;
                    try
                    {
                        if (line.Length > 1_000_000) throw new InvalidOperationException("request_too_large");
                        using var doc = JsonDocument.Parse(line);
                        var request = doc.RootElement;
                        id = request.GetProperty("id").Clone();
                        if (queued.Generation != Volatile.Read(ref generation)) throw new InvalidOperationException("request_cancelled_by_stop_or_pause");
                        var method = request.GetProperty("method").GetString() ?? "";
                        var p = request.TryGetProperty("params", out var provided) ? provided : default;
                        object OnUi(Func<object> operation) => app.Dispatcher.Invoke(operation);
                        object result;
                        switch (method)
                        {
                            case "list_windows": result = local.ListWindows(); break;
                            case "interaction_targets": result = local.InteractionTargets(); break;
                            case "attach_window":
                                input.Cancel();
                                input.RequireDetached();
                                result = local.Attach(p.GetProperty("hwnd").GetInt64());
                                if (queued.Generation != Volatile.Read(ref generation)) { local.Stop(); throw new InvalidOperationException("attachment_cancelled"); }
                                try { OnUi(() => { preview.Start(new nint(local.TargetHwnd), local.TargetTitle); preview.SetPaused(local.Paused); return true; }); }
                                catch { local.Stop(); throw; }
                                break;
                            case "observe":
                                var observation = local.ObservePage(p.TryGetProperty("max_elements", out var max) ? max.GetInt32() : 200,
                                    p.TryGetProperty("continuation", out var cont) ? cont.GetString() : null,
                                    p.TryGetProperty("search", out var search) ? search.GetString() : null,
                                    p.TryGetProperty("subtree_id", out var subtree) ? subtree.GetString() : null,
                                    p.TryGetProperty("max_nodes", out var nodes) ? nodes.GetInt32() : 2000);
                                var shot = preview.SnapshotForInputAsync(p.TryGetProperty("include_screenshot", out var include) && include.GetBoolean()).GetAwaiter().GetResult();
                                result = new { observation, capture = shot, inputFrame = input.CaptureToken(shot) };
                                break;
                            case "capabilities": result = input.Capabilities(); break;
                            case "invalidate_frame": input.InvalidateFrame(); result = new { invalidated = true }; break;
                            case "input":
                                result = input.Input(p.GetProperty("frame_id").GetString()!, p.GetProperty("steps"),
                                    p.TryGetProperty("deadline_ms", out var deadline) ? deadline.GetInt32() : 8000);
                                break;
                            case "act":
                                input.InvalidateFrame();
                                result = local.Act(p.GetProperty("observation_id").GetString()!, p.GetProperty("element_id").GetString()!,
                                    p.GetProperty("action").GetString()!, p.TryGetProperty("value", out var value) ? value.GetString() : null,
                                    p.TryGetProperty("amount", out var amount) ? amount.GetDouble() : 0);
                                OnUi(() => { preview.SetPaused(local.Paused); return true; });
                                break;
                            case "pause": local.Pause(); OnUi(() => { preview.SetPaused(true); return true; }); result = local.State(); break;
                            case "resume": local.Resume(); OnUi(() => { preview.SetPaused(false); return true; }); result = local.State(); break;
                            case "stop": local.Stop(); OnUi(() => { preview.Stop(); return true; }); result = local.State(); break;
                            case "state": result = new { controller = local.State(), capture = OnUi(() => preview.Snapshot(false)) }; break;
                            case "clipboard_state": local.ValidateActiveSession(); result = clipboard.State(); break;
                            case "clipboard_read": local.ValidateActiveSession(); result = clipboard.Read(); break;
                            case "clipboard_write":
                                var clipboardGuard = local.CaptureActiveSessionGuard();
                                result = clipboard.Write(p.GetProperty("text").GetString()!,
                                    p.GetProperty("expected_sequence").GetUInt32(), clipboardGuard);
                                break;
                            default: throw new InvalidOperationException("unknown_method");
                        }
                        Reply(new { id, result });
                    }
                    catch (Exception ex)
                    {
                        var evidence = new Dictionary<string, object?>();
                        foreach (var key in new[] { "providerMutationEntered", "focusBefore", "focusAfter", "paused", "deliveredCommands", "effectVerified" })
                            if (ex.Data.Contains(key)) evidence[key] = ex.Data[key];
                        try { app.Dispatcher.Invoke(() => preview.SetPaused(local.Paused)); } catch { }
                        Reply(new { id, error = new { code = "controller_error", message = ex.Message, evidence } });
                    }
                    Console.Out.Flush();
                }
            }
            catch (Exception ex) { Console.Error.WriteLine(ex.Message); }
            finally
            {
                controller?.Stop();
                app.Dispatcher.BeginInvoke(() => { preview.Stop(); app.Shutdown(); });
            }
        }) { IsBackground = true, Name = "BackgroundControl-UIA" };
        worker.SetApartmentState(ApartmentState.MTA);
        worker.Start();
        app.Run();
        controller?.Stop(); preview.Dispose();
        return 0;
    }
}
