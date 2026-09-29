using BackgroundControl;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json;

using var document = JsonDocument.Parse(Console.ReadLine()!);
var root = document.RootElement;
nint host = (nint)root.GetProperty("host").GetInt64();
int pid = root.GetProperty("pid").GetInt32();
var results = new List<object>();
foreach (var control in root.GetProperty("controls").EnumerateObject())
{
    nint hwnd = (nint)control.Value.GetInt64();
    bool supported = NativeEditActions.Supports(hwnd, host, pid, false, false);
    bool expected = control.Name is "edit_single" or "edit_multi" or "richedit_multi";
    if (supported != expected) throw new Exception($"Wrong support: {control.Name} {supported}");
    var rejected = new Dictionary<string, bool>();
    nint unrelatedRoot = (nint)root.GetProperty("controls").GetProperty(control.Name == "edit_single" ? "edit_multi" : "edit_single").GetInt64();
    foreach (var (label, targetRoot, targetPid, protectedFlag, readonlyFlag) in new[] {
        ("wrong_pid", host, pid + 1, false, false),
        ("unrelated_root", unrelatedRoot, pid, false, false),
        ("protected_flag", host, pid, true, false),
        ("readonly_flag", host, pid, false, true) })
    {
        bool denied = !NativeEditActions.Supports(hwnd, targetRoot, targetPid, protectedFlag, readonlyFlag);
        if (!denied) throw new Exception($"Validation accepted: {label}");
        rejected[label] = denied;
    }
    if (supported)
    {
        NativeEditActions.Insert(hwnd, host, pid, false, false, "世界🙂");
        foreach (var invalid in new[] { "\0", "\uD800", new string('x', 50_001) })
        {
            try { NativeEditActions.Insert(hwnd, host, pid, false, false, invalid); throw new Exception("Invalid text accepted"); }
            catch (ArgumentException) { }
        }
    }
    else
    {
        try { NativeEditActions.Insert(hwnd, host, pid, false, false, "must not insert"); throw new Exception("Protected insertion accepted"); }
        catch (InvalidOperationException) { }
    }
    results.Add(new { control = control.Name, supported, rejected });
}
if (NativeEditActions.Supports(host, host, pid, false, false)) throw new Exception("Wrong class accepted");
var stallResult = SendMessageTimeout(host, 0x8002, 0, 0, 0x23, 1, out _);
if (stallResult != 0) throw new Exception("Fixture did not block its UI thread");
var deadline = Stopwatch.StartNew();
bool hungSupport = NativeEditActions.Supports((nint)root.GetProperty("controls").GetProperty("edit_single").GetInt64(), host, pid, false, false);
if (hungSupport || deadline.ElapsedMilliseconds > 300) throw new Exception("Native helper timeout failed");
results.Add(new { stage = "production_supports_timeout", elapsedMs = deadline.ElapsedMilliseconds, supported = hungSupport });
Thread.Sleep(300); // Allow the deliberately blocked own fixture callback to finish before readback.
Console.WriteLine(JsonSerializer.Serialize(new { status = "passed", results }));

[DllImport("user32.dll", EntryPoint = "SendMessageTimeoutW")]
static extern nint SendMessageTimeout(nint hwnd, uint message, nuint wp, nint lp, uint flags, uint timeout, out nuint result);
