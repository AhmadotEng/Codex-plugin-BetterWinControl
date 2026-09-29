using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Windows;
using System.Windows.Threading;

namespace BackgroundControl;

internal static class Program
{
    static readonly JsonSerializerOptions Json = new() { PropertyNamingPolicy = JsonNamingPolicy.CamelCase };
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
                using var clipboard = new ClipboardBridge(operation =>
                    app.Dispatcher.HasShutdownStarted || app.Dispatcher.HasShutdownFinished ? 0 : app.Dispatcher.Invoke(operation));
                controller = local;
                string? line;
                while ((line = Console.ReadLine()) is not null)
                {
                    JsonElement? id = null;
                    try
                    {
                        if (line.Length > 1_000_000) throw new InvalidOperationException("request_too_large");
                        using var doc = JsonDocument.Parse(line);
                        var request = doc.RootElement;
                        id = request.GetProperty("id").Clone();
                        var method = request.GetProperty("method").GetString() ?? "";
                        var p = request.TryGetProperty("params", out var provided) ? provided : default;
                        object OnUi(Func<object> operation) => app.Dispatcher.Invoke(operation);
                        object result;
                        switch (method)
                        {
                            case "list_windows": result = local.ListWindows(); break;
                            case "attach_window":
                                result = local.Attach(p.GetProperty("hwnd").GetInt64());
                                try { OnUi(() => { preview.Start(new nint(local.TargetHwnd), local.TargetTitle); preview.SetPaused(local.Paused); return true; }); }
                                catch { local.Stop(); throw; }
                                break;
                            case "observe":
                                var observation = local.Observe(p.TryGetProperty("max_elements", out var max) ? max.GetInt32() : 200);
                                var shot = (CaptureSnapshot)OnUi(() => preview.Snapshot(p.TryGetProperty("include_screenshot", out var include) && include.GetBoolean()));
                                result = new { observation, capture = shot };
                                break;
                            case "act":
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
                        Console.WriteLine(JsonSerializer.Serialize(new { id, result }, Json));
                    }
                    catch (Exception ex)
                    {
                        var evidence = new Dictionary<string, object?>();
                        foreach (var key in new[] { "providerMutationEntered", "focusBefore", "focusAfter", "paused" })
                            if (ex.Data.Contains(key)) evidence[key] = ex.Data[key];
                        try { app.Dispatcher.Invoke(() => preview.SetPaused(local.Paused)); } catch { }
                        Console.WriteLine(JsonSerializer.Serialize(new { id, error = new { code = "controller_error", message = ex.Message, evidence } }, Json));
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
