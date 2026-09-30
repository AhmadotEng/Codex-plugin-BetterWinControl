using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json;
using BackgroundControl;

internal static class Program
{
    static readonly Native.WndProc Procedure = WindowProc;
    static readonly List<nint> Windows = new();
    static nint root;
    static uint fixtureThread;
    static readonly string ClassName = "InteractionOwnershipFixture_" + Environment.ProcessId;

    [MTAThread]
    static int Main(string[] args)
    {
        if (args.FirstOrDefault() == "--fixture") return Fixture(args);
        string resultPath = args.FirstOrDefault() ?? Path.Combine(AppContext.BaseDirectory, "results.json");
        var checks = new List<object>();
        var children = new List<Process>();
        var watch = Stopwatch.StartNew();
        var focusBefore = Native.GetForegroundWindow();
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(20));
        string? failure = null;
        object? initial = null, flood = null;
        try
        {
            using var controller = new WindowController();
            var primary = Launch(); children.Add(primary.Process);
            long selected = primary.Ready.GetProperty("root").GetInt64();
            long popup = primary.Ready.GetProperty("popup").GetInt64();
            long nested = primary.Ready.GetProperty("nested").GetInt64();
            long sibling = primary.Ready.GetProperty("sibling").GetInt64();
            long hidden = primary.Ready.GetProperty("hidden").GetInt64();
            long minimized = primary.Ready.GetProperty("minimized").GetInt64();
            var foreign = Launch(selected); children.Add(foreign.Process);
            long foreignPopup = foreign.Ready.GetProperty("root").GetInt64();
            controller.Attach(selected);
            var discovered = JsonSerializer.SerializeToElement(controller.InteractionTargets()); initial = discovered;
            var targets = discovered.GetProperty("targets").EnumerateArray().ToArray();
            JsonElement Row(long hwnd) => targets.Single(x => x.GetProperty("window").GetProperty("hwnd").GetInt64() == hwnd);
            void Verified(long hwnd, string relation)
            {
                var row = Row(hwnd); Require(row.GetProperty("ownershipVerified").GetBoolean(), relation + " ownership");
                Require(row.GetProperty("relation").GetString() == relation, relation + " classification");
                Require(row.GetProperty("eligibility").GetProperty("selectable").GetBoolean(), relation + " selectable");
                var identity = row.GetProperty("window");
                Require(identity.GetProperty("pid").GetInt32() == primary.Process.Id && identity.GetProperty("startTicks").GetInt64() > 0 &&
                    identity.GetProperty("threadId").GetUInt32() != 0 && identity.GetProperty("className").GetString() == primary.Ready.GetProperty("className").GetString(), "exact native identity fields");
            }
            Verified(selected, "selected"); Verified(popup, "owned_descendant"); Verified(nested, "owned_descendant");
            Require(Row(nested).GetProperty("ownerChain").GetArrayLength() == 3, "nested owner evidence retains all three identities");
            checks.Add(new { name = "selected_and_same_process_owner_chains", passed = true });
            var siblingRow = Row(sibling);
            Require(!siblingRow.GetProperty("ownershipVerified").GetBoolean() && !siblingRow.GetProperty("eligibility").GetProperty("selectable").GetBoolean(), "same PID and identical title must not prove ownership");
            checks.Add(new { name = "same_pid_same_title_sibling_is_unverified", passed = true });
            var foreignRow = Row(foreignPopup);
            Require(!foreignRow.GetProperty("ownershipVerified").GetBoolean() && !foreignRow.GetProperty("eligibility").GetProperty("selectable").GetBoolean() &&
                foreignRow.GetProperty("relation").GetString() == "unverified_cross_process_owned_descendant", "cross-process native owner is not eligible");
            checks.Add(new { name = "cross_process_owner_link_is_unverified", passed = true });
            Require(!Row(hidden).GetProperty("visible").GetBoolean() && !Row(hidden).GetProperty("eligibility").GetProperty("selectable").GetBoolean(), "hidden owner remains ineligible");
            Require(Row(minimized).GetProperty("minimized").GetBoolean() && !Row(minimized).GetProperty("eligibility").GetProperty("selectable").GetBoolean(), "minimized owner remains ineligible");
            Require(controller.TargetHwnd == selected, "discovery never switches target");
            checks.Add(new { name = "availability_flags_and_no_implicit_switch", passed = true });

            controller.Attach(nested);
            var fromPopup = JsonSerializer.SerializeToElement(controller.InteractionTargets());
            var ancestors = fromPopup.GetProperty("targets").EnumerateArray().Where(x => x.GetProperty("relation").GetString() == "owner_ancestor").ToArray();
            Require(ancestors.Any(x => x.GetProperty("window").GetProperty("hwnd").GetInt64() == popup) &&
                    ancestors.Any(x => x.GetProperty("window").GetProperty("hwnd").GetInt64() == selected), "owner parent and root available for explicit return");
            Require(ancestors.All(x => x.GetProperty("ownershipVerified").GetBoolean()), "owner ancestry verified");
            checks.Add(new { name = "selected_popup_exposes_verified_owner_return_path", passed = true });

            controller.Attach(selected);
            primary.Process.StandardInput.WriteLine("flood"); primary.Process.StandardInput.Flush();
            Require(primary.Process.StandardOutput.ReadLineAsync(timeout.Token).GetAwaiter().GetResult() == "flooded", "own hidden sibling fixture ready");
            var bounded = JsonSerializer.SerializeToElement(controller.InteractionTargets()); flood = bounded;
            var rows = bounded.GetProperty("targets").EnumerateArray().ToArray();
            Require(rows.Length <= 64 && bounded.GetProperty("scannedWindows").GetInt32() <= 512, "discovery response and enumeration bounded");
            Require(!bounded.GetProperty("complete").GetBoolean() && bounded.GetProperty("next").GetString()!.Contains("incomplete"), "truncation explicit with narrowing instruction");
            Require(rows.Any(x => x.GetProperty("window").GetProperty("hwnd").GetInt64() == nested && x.GetProperty("ownershipVerified").GetBoolean()), "unverified sibling flood does not displace verified descendant");
            Require(bounded.GetProperty("continuation").ValueKind == JsonValueKind.Null, "no invented continuation token");
            checks.Add(new { name = "bounded_prioritized_discovery_and_explicit_incomplete_result", passed = true,
                returned = rows.Length, scanned = bounded.GetProperty("scannedWindows").GetInt32(), elapsedMs = bounded.GetProperty("elapsedMs").GetInt64() });
            controller.Stop();
            bool stoppedRejected = false;
            try { controller.InteractionTargets(); } catch (InvalidOperationException) { stoppedRejected = true; }
            Require(stoppedRejected, "no discovery on a stopped session");
            checks.Add(new { name = "stopped_session_rejected", passed = true });
        }
        catch (Exception ex) { failure = ex.ToString(); }
        finally
        {
            foreach (var process in children)
            {
                if (!process.HasExited)
                {
                    try { process.StandardInput.WriteLine("stop"); process.StandardInput.Flush(); } catch { }
                    if (!process.WaitForExit(2000)) process.Kill(true);
                    process.WaitForExit(1000);
                }
            }
        }
        bool focusUnchanged = Native.GetForegroundWindow() == focusBefore;
        if (!focusUnchanged) failure ??= "Foreground changed during owned no-activation discovery test; cannot attribute concurrent user activity.";
        var result = new { status = failure is null ? "passed" : "failed", failure, checks, elapsedMs = watch.ElapsedMilliseconds,
            initial, flood, focusUnchanged, allOwnedProcessesExited = children.All(p => p.HasExited),
            limitation = "Owned fixture metadata only. No physical input or activation. Cross-process owner mapping remains unsupported. Native ownerless menus are classified by production metadata but no native popup menu loop is opened in this test. Focus check is before/after only, not a concurrent-user or transient-focus claim." };
        File.WriteAllText(resultPath, JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = true }));
        Console.WriteLine(JsonSerializer.Serialize(new { result.status, result.failure, checks = checks.Count, result.elapsedMs }));
        foreach (var process in children) process.Dispose();
        return failure is null ? 0 : 1;
    }

    static (Process Process, JsonElement Ready) Launch(long? owner = null)
    {
        var start = new ProcessStartInfo(Environment.ProcessPath!) { UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true };
        start.ArgumentList.Add("--fixture");
        if (owner is not null) start.ArgumentList.Add(owner.Value.ToString());
        var process = Process.Start(start)!;
        var line = process.StandardOutput.ReadLineAsync().WaitAsync(TimeSpan.FromSeconds(3)).GetAwaiter().GetResult();
        var ready = JsonDocument.Parse(line!).RootElement.Clone();
        Require(ready.GetProperty("pid").GetInt32() == process.Id, "fixture process identity");
        return (process, ready);
    }
    static int Fixture(string[] args)
    {
        fixtureThread = Native.GetCurrentThreadId();
        var cls = new Native.WndClass { Size = (uint)Marshal.SizeOf<Native.WndClass>(), Instance = Native.GetModuleHandle(null),
            Procedure = Marshal.GetFunctionPointerForDelegate(Procedure), ClassName = ClassName };
        Require(Native.RegisterClassEx(ref cls) != 0, "register own fixture class");
        long externalOwner = args.Length > 1 ? long.Parse(args[1]) : 0;
        root = Create(new nint(externalOwner), true);
        nint popup = 0, nested = 0, sibling = 0, hidden = 0, minimized = 0;
        if (externalOwner == 0)
        {
            popup = Create(root, true); nested = Create(popup, true); sibling = Create(0, true);
            hidden = Create(root, false); minimized = Create(root, true); Native.ShowWindow(minimized, 7);
        }
        Native.SetTimer(root, 1, 15000, 0);
        Console.WriteLine(JsonSerializer.Serialize(new { pid = Environment.ProcessId, root = root.ToInt64(), popup = popup.ToInt64(),
            nested = nested.ToInt64(), sibling = sibling.ToInt64(), hidden = hidden.ToInt64(), minimized = minimized.ToInt64(), className = ClassName }));
        new Thread(() =>
        {
            string? line;
            while ((line = Console.ReadLine()) is not null)
            {
                if (line == "flood") Native.PostMessage(root, 0x8001, 0, 0);
                else { Native.PostThreadMessage(fixtureThread, 0x12, 0, 0); return; }
            }
            Native.PostThreadMessage(fixtureThread, 0x12, 0, 0);
        }) { IsBackground = true }.Start();
        while (Native.GetMessage(out var message, 0, 0, 0) > 0) { Native.TranslateMessage(ref message); Native.DispatchMessage(ref message); }
        foreach (var hwnd in Windows) if (Native.IsWindow(hwnd)) Native.DestroyWindow(hwnd);
        return 0;
    }
    static nint Create(nint owner, bool visible)
    {
        var hwnd = Native.CreateWindowEx(0x08000080, ClassName, "AUTOMATED — DO NOT CLICK — Ownership fixture", 0x00CF0000,
            40, 40, 210, 110, owner, 0, Native.GetModuleHandle(null), 0);
        Require(hwnd != 0, "create own fixture"); Windows.Add(hwnd);
        if (visible) Native.ShowWindow(hwnd, 4);
        return hwnd;
    }
    static nint WindowProc(nint hwnd, uint message, nint w, nint l)
    {
        if (message == 0x8001)
        {
            for (int i = 0; i < 80; i++) Create(0, false);
            Console.WriteLine("flooded"); return 0;
        }
        if (message == 0x113) { Native.PostThreadMessage(fixtureThread, 0x12, 0, 0); return 0; }
        return Native.DefWindowProc(hwnd, message, w, l);
    }
    static void Require(bool condition, string message) { if (!condition) throw new InvalidOperationException(message); }

    static class Native
    {
        internal delegate nint WndProc(nint hwnd, uint message, nint w, nint l);
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] internal struct WndClass
        { public uint Size, Style; public nint Procedure; public int ClassExtra, WindowExtra; public nint Instance, Icon, Cursor, Background; public string? MenuName; public string ClassName; public nint SmallIcon; }
        [StructLayout(LayoutKind.Sequential)] internal struct Point { public int X, Y; }
        [StructLayout(LayoutKind.Sequential)] internal struct Message { public nint Hwnd; public uint Value; public nint WParam, LParam; public uint Time; public Point Point; public uint Private; }
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] internal static extern nint GetModuleHandle(string? name);
        [DllImport("kernel32.dll")] internal static extern uint GetCurrentThreadId();
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] internal static extern ushort RegisterClassEx(ref WndClass value);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] internal static extern nint CreateWindowEx(uint ex, string cls, string title, uint style, int x, int y, int width, int height, nint parent, nint menu, nint instance, nint parameter);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] internal static extern nint DefWindowProc(nint hwnd, uint message, nint w, nint l);
        [DllImport("user32.dll")] internal static extern bool ShowWindow(nint hwnd, int command);
        [DllImport("user32.dll")] internal static extern bool PostMessage(nint hwnd, uint message, nint w, nint l);
        [DllImport("user32.dll")] internal static extern bool PostThreadMessage(uint thread, uint message, nint w, nint l);
        [DllImport("user32.dll")] internal static extern int GetMessage(out Message message, nint hwnd, uint min, uint max);
        [DllImport("user32.dll")] internal static extern bool TranslateMessage(ref Message message);
        [DllImport("user32.dll")] internal static extern nint DispatchMessage(ref Message message);
        [DllImport("user32.dll")] internal static extern nint SetTimer(nint hwnd, nuint id, uint milliseconds, nint callback);
        [DllImport("user32.dll")] internal static extern bool IsWindow(nint hwnd);
        [DllImport("user32.dll")] internal static extern bool DestroyWindow(nint hwnd);
        [DllImport("user32.dll")] internal static extern nint GetForegroundWindow();
    }
}
