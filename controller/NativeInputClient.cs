using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;

namespace BackgroundControl;

/// <summary>Private JSON-lines transport to the architecture-matched helper. No shared input APIs.</summary>
internal sealed class NativeInputClient
{
    private readonly Process process;
    private readonly SemaphoreSlim serial = new(1);
    private int sequence;
    private int revoked;
    private Task<object>? cleanup;
    private string? revokeEvent;
    private readonly object lifecycle = new();
    private static readonly JsonSerializerOptions Json = new() { PropertyNamingPolicy = JsonNamingPolicy.CamelCase };

    public static string HelperPath(long hwnd)
    {
        GetWindowThreadProcessId(new nint(hwnd), out var pid);
        using var target = Process.GetProcessById((int)pid);
        if (!IsWow64Process2(target.Handle, out var machine, out var native))
            throw new InvalidOperationException("architecture_unavailable");
        var arch = machine == 0x14c ? "x86" : machine == 0 && native == 0x8664 ? "x64" : "";
        if (arch.Length == 0) throw new InvalidOperationException("unsupported_architecture");
        return Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "..", "native", "bin", arch, "BetterWinControl.NativeHost.exe"));
    }

    public NativeInputClient(long hwnd)
    {
        var helper = HelperPath(hwnd);
        if (!File.Exists(helper)) throw new InvalidOperationException("native_helper_not_built: run scripts/build-native.ps1");
        var start = new ProcessStartInfo(helper) {
            UseShellExecute = false, CreateNoWindow = true, RedirectStandardInput = true,
            RedirectStandardOutput = true, RedirectStandardError = true,
            StandardInputEncoding = new UTF8Encoding(false), StandardOutputEncoding = Encoding.UTF8,
            WorkingDirectory = Path.GetDirectoryName(helper)!
        };
        start.ArgumentList.Add("--hwnd"); start.ArgumentList.Add(hwnd.ToString());
        start.ArgumentList.Add("--owner-pid"); start.ArgumentList.Add(Environment.ProcessId.ToString());
        process = Process.Start(start) ?? throw new InvalidOperationException("native_helper_start_failed");
        // Once a child exists, construction must return its lifetime handle so a
        // concurrent Stop can revoke it. Ancillary diagnostic draining must not
        // turn a successful launch into an apparent "helper not started" failure.
        try { _ = Task.Run(async () =>
        { try { while (await process.StandardError.ReadLineAsync() is not null) { } } catch { } }); }
        catch { /* JSON stdout remains authoritative; parent/EOF watchdog still owns lifetime. */ }
    }

    public JsonElement Request(object request)
    {
        if (Volatile.Read(ref revoked) != 0) throw new InvalidOperationException("native_input_revoked");
        serial.Wait();
        try
        {
            if (Volatile.Read(ref revoked) != 0) throw new InvalidOperationException("native_input_revoked");
            return Exchange(request);
        }
        finally { serial.Release(); }
    }

    private JsonElement Exchange(object request)
    {
        var node = JsonSerializer.SerializeToNode(request, Json)!.AsObject();
        var id = Interlocked.Increment(ref sequence);
        node["id"] = id;
        process.StandardInput.WriteLine(node.ToJsonString());
        process.StandardInput.Flush();
        var line = process.StandardOutput.ReadLineAsync().WaitAsync(TimeSpan.FromSeconds(3)).GetAwaiter().GetResult();
        if (line is null || line.Length > 1000000) throw new InvalidOperationException("native_helper_disconnected");
        using var response = JsonDocument.Parse(line);
        var root = response.RootElement;
        if (!root.TryGetProperty("id", out var reply) || reply.GetInt32() != id)
            throw new InvalidOperationException("native_protocol_mismatch");
        if (root.TryGetProperty("error", out var error))
            throw new InvalidOperationException("native_error: " + error.GetRawText());
        var result = root.GetProperty("result").Clone();
        if (result.TryGetProperty("revocationEventName", out var name)) revokeEvent = name.GetString();
        return result;
    }

    public Task<object> Revoke()
    {
        Volatile.Write(ref revoked, 1);
        var eventName = revokeEvent;
        if (eventName is not null)
        {
            var handle = OpenEvent(0x0002, false, eventName);
            if (handle != nint.Zero) { try { SetEvent(handle); } finally { CloseHandle(handle); } }
        }
        lock (lifecycle) return cleanup ??= Task.Run<object>(() =>
        {
            // Never block the preview Stop button. A helper's independent EOF/owner-loss
            // monitor is the last resort; loss of acknowledgement is not teardown proof.
            bool acquired = serial.Wait(3500);
            object result;
            try
            {
                if (!acquired) throw new TimeoutException("in_flight_dispatch");
                var ack = Exchange(new { op = "detach" });
                result = ack;
            }
            catch (Exception ex)
            {
                result = new { detached = false, hooksRemoved = false, moduleUnloaded = false,
                    teardownVerified = false, status = "detach_pending_or_unverified", reason = ex.Message };
            }
            finally
            {
                try { process.StandardInput.Close(); } catch { }
                if (acquired) serial.Release();
            }
            // Do not terminate a helper still performing teardown or its target process.
            return result;
        });
    }

    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(nint hwnd, out uint pid);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool IsWow64Process2(nint process, out ushort machine, out ushort native);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern nint OpenEvent(uint access, bool inherit, string name);
    [DllImport("kernel32.dll")] private static extern bool SetEvent(nint handle);
    [DllImport("kernel32.dll")] private static extern bool CloseHandle(nint handle);
}
