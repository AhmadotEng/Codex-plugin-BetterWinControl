using System.Runtime.InteropServices;

namespace BackgroundControl;

/// <summary>WinEvent metadata only. Does not hook, record or inspect physical keystrokes.</summary>
internal sealed class InputIsolationMonitor : IDisposable
{
    private readonly Thread thread;
    private readonly ManualResetEventSlim ready = new();
    private readonly Func<long> target;
    private readonly Action<string> violation;
    private readonly List<nint> hooks = new();
    private readonly WinEvent callback;
    private uint threadId;
    private int stopped;
    private Exception? startupFailure;
    public InputIsolationMonitor(Func<long> target, Action<string> violation)
    {
        this.target = target; this.violation = violation; callback = OnEvent;
        thread = new Thread(Run) { IsBackground = true, Name = "BackgroundControl-IsolationEvents" };
        thread.Start();
        if (!ready.Wait(2000) || startupFailure is not null)
        { Dispose(); throw new InvalidOperationException("input_isolation_monitor_unavailable", startupFailure); }
    }
    private void Run()
    {
        try
        {
            threadId = GetCurrentThreadId();
            PeekMessage(out _, 0, 0, 0, 0); // establish a message queue before readiness
            foreach (uint e in new uint[] { 3, 8, 0x8001, 0x8005 })
            {
                var hook = SetWinEventHook(e, e, 0, callback, 0, 0, 0);
                if (hook == 0) throw new InvalidOperationException("win_event_hook_failed");
                hooks.Add(hook);
            }
            ready.Set();
            while (Volatile.Read(ref stopped) == 0 && GetMessage(out var msg, 0, 0, 0) > 0)
            { TranslateMessage(ref msg); DispatchMessage(ref msg); }
        }
        catch (Exception ex) { startupFailure = ex; ready.Set(); }
        finally { foreach (var hook in hooks) UnhookWinEvent(hook); }
    }
    private void OnEvent(nint hook, uint e, nint hwnd, int obj, int child, uint eventThread, uint time)
    {
        var selected = new nint(target());
        if (selected == 0 || hwnd == 0) return;
        if (e == 0x8001)
        {
            if (hwnd == selected && obj == 0 && child == 0) violation("selected_window_destroyed");
            return;
        }
        var root = GetAncestor(hwnd, 2);
        bool owned = root == selected;
        for (int i = 0; !owned && root != 0 && i < 16; i++)
        { root = GetWindow(root, 4); owned = root == selected; }
        if (!owned) return;
        // Accessibility focus events alone can be logical-only; check real GUI state.
        if (e == 0x8005)
        {
            var gui = new Gui { Size = (uint)Marshal.SizeOf<Gui>() };
            // A background thread may retain its own last focused HWND. Only the
            // foreground input queue determines where physical typing is delivered.
            if (!GetGUIThreadInfo(0, ref gui) || gui.Focus == 0 ||
                (gui.Focus != hwnd && GetAncestor(gui.Focus, 2) != selected)) return;
        }
        violation(e == 8 ? "target_acquired_physical_mouse_capture" :
            e == 3 ? "target_acquired_foreground" : "target_acquired_physical_keyboard_focus");
    }
    public void Dispose()
    {
        if (Interlocked.Exchange(ref stopped, 1) != 0) return;
        if (threadId != 0) PostThreadMessage(threadId, 0x0012, 0, 0);
        if (Thread.CurrentThread != thread) thread.Join(1000);
    }
    [StructLayout(LayoutKind.Sequential)] private struct Rect { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)] private struct Gui
    { public uint Size, Flags; public nint Active, Focus, Capture, Menu, MoveSize, Caret; public Rect CaretRect; }
    [StructLayout(LayoutKind.Sequential)] private struct Message
    { public nint Hwnd; public uint Id; public nuint W; public nint L; public uint Time; public int X, Y; public uint Private; }
    private delegate void WinEvent(nint hook, uint e, nint hwnd, int obj, int child, uint thread, uint time);
    [DllImport("kernel32.dll")] private static extern uint GetCurrentThreadId();
    [DllImport("user32.dll")] private static extern nint SetWinEventHook(uint min, uint max, nint module, WinEvent callback, uint pid, uint tid, uint flags);
    [DllImport("user32.dll")] private static extern bool UnhookWinEvent(nint hook);
    [DllImport("user32.dll")] private static extern int GetMessage(out Message message, nint hwnd, uint min, uint max);
    [DllImport("user32.dll")] private static extern bool PeekMessage(out Message message, nint hwnd, uint min, uint max, uint remove);
    [DllImport("user32.dll")] private static extern bool TranslateMessage(ref Message message);
    [DllImport("user32.dll")] private static extern nint DispatchMessage(ref Message message);
    [DllImport("user32.dll")] private static extern bool PostThreadMessage(uint thread, uint message, nuint w, nint l);
    [DllImport("user32.dll")] private static extern nint GetAncestor(nint hwnd, uint flags);
    [DllImport("user32.dll")] private static extern nint GetWindow(nint hwnd, uint command);
    [DllImport("user32.dll")] private static extern bool GetGUIThreadInfo(uint thread, ref Gui info);
}
