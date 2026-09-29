using System;
using System.ComponentModel;
using System.Diagnostics;
using System.Collections.Generic;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using Microsoft.Win32.SafeHandles;

namespace BackgroundControl;

/// <summary>
/// A session-local kernel object used as an exclusive ownership token, not as a signaled event.
/// A fresh CreateEvent owns the lease; ERROR_ALREADY_EXISTS loses and immediately closes its handle.
/// Unlike a mutex, disposal is not thread-affine. Process exit closes the token automatically.
/// </summary>
internal sealed class WindowLease : IDisposable
{
    private readonly SafeWaitHandle handle;
    private readonly long hwnd, startTicks;
    private readonly int pid;
    private readonly uint threadId;
    private readonly WindowPart[] ownerChain;
    private readonly System.Threading.Timer watcher;
    private int released;
    internal bool IsReleased => Volatile.Read(ref released) != 0;

    private WindowLease(SafeWaitHandle handle, long hwnd, int pid, long startTicks, uint threadId, WindowPart[] ownerChain)
    {
        this.handle = handle;
        this.hwnd = hwnd;
        this.pid = pid;
        this.startTicks = startTicks;
        this.threadId = threadId;
        this.ownerChain = ownerChain;
        watcher = new System.Threading.Timer(_ => CheckIdentity(), null, 250, 250);
    }

    internal static WindowLease Acquire(long hwnd, int pid, long startTicks, uint threadId, WindowLease? previous = null)
    {
        var chain = ReadOwnerChain(hwnd, pid);
        long scopeHwnd = chain[^1].Hwnd;
        if (previous is not null && previous.pid == pid && previous.startTicks == startTicks &&
            previous.ownerChain[^1].Hwnd == scopeHwnd && previous.IsScopeCurrent())
        {
            // Reattachment within the same owner family gets its own reference and watchdog.
            // The old lease remains intact until the transaction commits.
            if (!DuplicateHandle(GetCurrentProcess(), previous.handle, GetCurrentProcess(), out var duplicate, 0, false, 2))
                throw new InvalidOperationException("lease_unavailable: Could not retain the existing window lease.");
            try { return new WindowLease(duplicate, hwnd, pid, startTicks, threadId, chain); }
            catch { duplicate.Dispose(); throw; }
        }
        string name = $@"Local\Codex.WindowsBackgroundControl.v1.{pid}.{startTicks:x}.{scopeHwnd:x}";
        var raw = CreateEvent(nint.Zero, false, false, name);
        int error = Marshal.GetLastWin32Error();
        if (raw == nint.Zero) throw new InvalidOperationException("lease_unavailable: " + new Win32Exception(error).Message);
        var handle = new SafeWaitHandle(raw, ownsHandle: true);
        if (error == 183)
        {
            handle.Dispose();
            throw new InvalidOperationException("target_busy: Another controller already owns this window. Stop that controller before attaching here.");
        }
        try { return new WindowLease(handle, hwnd, pid, startTicks, threadId, chain); }
        catch { handle.Dispose(); throw; }
    }

    private void CheckIdentity()
    {
        if (IsReleased) return;
        try
        {
            var currentThread = GetWindowThreadProcessId(new nint(hwnd), out var currentPid);
            if (!IsWindow(new nint(hwnd)) || currentPid != pid || currentThread != threadId || !IsScopeCurrent())
            { Dispose(); return; }
            using var process = Process.GetProcessById(pid);
            if (process.StartTime.ToUniversalTime().Ticks != startTicks) Dispose();
        }
        catch (Exception ex) when (ex is ArgumentException or InvalidOperationException or Win32Exception)
        { Dispose(); }
    }

    internal bool IsScopeCurrent()
    {
        if (IsReleased) return false;
        try
        {
            if (ReadOwnerChain(hwnd, pid).SequenceEqual(ownerChain)) return true;
        }
        catch (InvalidOperationException) { }
        Dispose();
        return false;
    }

    internal IDisposable Hold()
    {
        if (!IsScopeCurrent()) throw new InvalidOperationException("lease_lost: Window ownership changed.");
        bool added = false;
        try
        {
            handle.DangerousAddRef(ref added);
            if (IsReleased) throw new InvalidOperationException("lease_lost: Window ownership was revoked.");
            return new HoldReference(handle);
        }
        catch { if (added) handle.DangerousRelease(); throw; }
    }

    private static WindowPart[] ReadOwnerChain(long selected, int pid)
    {
        var parts = new List<WindowPart>();
        var hwnd = new nint(selected);
        while (hwnd != nint.Zero && parts.Count < 16)
        {
            uint thread = GetWindowThreadProcessId(hwnd, out var currentPid);
            if (!IsWindow(hwnd) || currentPid != pid || thread == 0)
                throw new InvalidOperationException("stale_target: Window owner-chain identity changed.");
            var className = new StringBuilder(256);
            GetClassName(hwnd, className, className.Capacity);
            parts.Add(new(hwnd.ToInt64(), thread, className.ToString()));
            var owner = GetWindow(hwnd, 4);
            if (owner == nint.Zero) return parts.ToArray();
            GetWindowThreadProcessId(owner, out var ownerPid);
            if (ownerPid != pid) return parts.ToArray();
            hwnd = owner;
        }
        throw new InvalidOperationException("invalid_target: Window owner chain is cyclic or exceeds the bounded depth.");
    }

    private sealed record WindowPart(long Hwnd, uint ThreadId, string ClassName);
    private sealed class HoldReference(SafeWaitHandle handle) : IDisposable
    {
        private SafeWaitHandle? held = handle;
        public void Dispose() => Interlocked.Exchange(ref held, null)?.DangerousRelease();
    }

    public void Dispose()
    {
        if (Interlocked.Exchange(ref released, 1) != 0) return;
        watcher.Dispose();
        handle.Dispose();
    }

    [DllImport("kernel32.dll", EntryPoint = "CreateEventW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern nint CreateEvent(nint attributes, bool manualReset, bool initialState, string name);
    [DllImport("user32.dll")] private static extern bool IsWindow(nint hwnd);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(nint hwnd, out uint processId);
    [DllImport("user32.dll")] private static extern nint GetWindow(nint hwnd, uint command);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetClassName(nint hwnd, StringBuilder text, int maxCount);
    [DllImport("kernel32.dll")] private static extern nint GetCurrentProcess();
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool DuplicateHandle(nint sourceProcess, SafeWaitHandle source,
        nint destinationProcess, out SafeWaitHandle duplicate, uint access, bool inherit, uint options);
}
