using System;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;

namespace BackgroundControl;

/// <summary>Plain-text insertion into a validated native edit's current selection; never keyboard input.</summary>
public static class NativeEditActions
{
    public const int MaximumTextLength = 50_000;
    private const uint EmReplaceSel = 0x00C2, EmGetPasswordChar = 0x00D2, EmGetEventMask = 0x043B;
    private const long EsPassword = 0x20, EsReadOnly = 0x800, EsMultiline = 0x4;
    private const ulong EnmProtected = 0x00200000;

    public static bool Supports(nint elementHwnd, nint rootHwnd, int expectedProcessId,
        bool isPasswordOrProtected, bool isReadOnly)
    {
        try
        {
            Validate(elementHwnd, rootHwnd, expectedProcessId, isPasswordOrProtected, isReadOnly,
                Stopwatch.StartNew());
            return true;
        }
        catch (InvalidOperationException) { return false; }
    }

    public static void Insert(nint elementHwnd, nint rootHwnd, int expectedProcessId,
        bool isPasswordOrProtected, bool isReadOnly, string text, Action? beforeMutation = null)
    {
        ArgumentNullException.ThrowIfNull(text);
        if (text.Length > MaximumTextLength || text.IndexOf('\0') >= 0)
            throw new ArgumentException("native_edit_invalid_text: maximum 50000 UTF-16 code units and no NUL.", nameof(text));
        for (var i = 0; i < text.Length; i++)
        {
            if (char.IsHighSurrogate(text[i]))
            {
                if (++i >= text.Length || !char.IsLowSurrogate(text[i]))
                    throw new ArgumentException("native_edit_invalid_utf16", nameof(text));
            }
            else if (char.IsLowSurrogate(text[i]))
                throw new ArgumentException("native_edit_invalid_utf16", nameof(text));
        }
        var deadline = Stopwatch.StartNew();
        var before = Validate(elementHwnd, rootHwnd, expectedProcessId, isPasswordOrProtected, isReadOnly, deadline);
        if ((before.Style & EsMultiline) == 0 && (text.Contains('\r') || text.Contains('\n')))
            throw new ArgumentException("native_edit_single_line: newline insertion is unsupported.", nameof(text));
        // Selection is deliberately left untouched. The observed control owns its insertion point.
        // This system message marshals the Unicode string across processes. No WM_USER pointer messages.
        nint buffer = Marshal.StringToHGlobalUni(text);
        try
        {
            CheckIdentity(elementHwnd, rootHwnd, expectedProcessId, before.ThreadId);
            beforeMutation?.Invoke();
            Send(elementHwnd, EmReplaceSel, 1, buffer, deadline);
        }
        finally { Marshal.FreeHGlobal(buffer); }
        CheckIdentity(elementHwnd, rootHwnd, expectedProcessId, before.ThreadId);
        // EM_REPLACESEL has no success result. The caller must observe the resulting content.
    }

    private static (uint ThreadId, long Style) Validate(nint hwnd, nint root, int pid,
        bool passwordOrProtected, bool readOnly, Stopwatch deadline)
    {
        if (passwordOrProtected || readOnly)
            throw new InvalidOperationException("native_edit_protected_or_read_only");
        uint thread = CheckIdentity(hwnd, root, pid);
        var name = new StringBuilder(64);
        if (GetClassName(hwnd, name, name.Capacity) == 0)
            throw new InvalidOperationException("native_edit_class_unavailable");
        string className = name.ToString();
        bool rich = className.Equals("RICHEDIT50W", StringComparison.OrdinalIgnoreCase);
        if (!rich && !className.Equals("EDIT", StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("native_edit_unsupported_class");
        long style = GetWindowLongPtr(hwnd, -16).ToInt64();
        if (!IsWindowUnicode(hwnd) || !IsWindowEnabled(hwnd) || (style & (EsPassword | EsReadOnly)) != 0)
            throw new InvalidOperationException("native_edit_disabled_password_readonly_or_ansi");
        if (Send(hwnd, EmGetPasswordChar, 0, 0, deadline) != 0)
            throw new InvalidOperationException("native_edit_password_control");
        // A protected RichEdit range can delegate policy to its host. Decline such controls.
        // EM_GETEVENTMASK has scalar parameters/results, so no cross-process pointer marshalling.
        if (rich && (Send(hwnd, EmGetEventMask, 0, 0, deadline) & EnmProtected) != 0)
            throw new InvalidOperationException("native_edit_protected_richtext_policy");
        CheckIdentity(hwnd, root, pid, thread);
        return (thread, style);
    }

    private static uint CheckIdentity(nint hwnd, nint root, int pid, uint expectedThread = 0)
    {
        if (hwnd == 0 || root == 0 || pid <= 0 || !IsWindow(hwnd) || !IsWindow(root))
            throw new InvalidOperationException("native_edit_stale_target");
        uint thread = GetWindowThreadProcessId(hwnd, out uint targetPid);
        uint rootThread = GetWindowThreadProcessId(root, out uint rootPid);
        if (thread == 0 || rootThread == 0 || targetPid != (uint)pid || rootPid != (uint)pid ||
            (hwnd != root && !IsChild(root, hwnd)) || (expectedThread != 0 && thread != expectedThread))
            throw new InvalidOperationException("native_edit_target_identity_mismatch");
        return thread;
    }

    private static ulong Send(nint hwnd, uint message, nuint wp, nint lp, Stopwatch deadline)
    {
        long remaining = 200 - deadline.ElapsedMilliseconds;
        if (remaining <= 0) throw new InvalidOperationException("native_edit_timeout: outcome requires re-observation");
        Marshal.SetLastPInvokeError(0);
        nint ok = SendMessageTimeout(hwnd, message, wp, lp, 0x1 | 0x2 | 0x20,
            (uint)remaining, out nuint result);
        if (ok == 0)
            throw new InvalidOperationException($"native_edit_message_failed_or_timed_out: {Marshal.GetLastPInvokeError()}; outcome requires re-observation");
        return result;
    }

    [DllImport("user32.dll", EntryPoint = "SendMessageTimeoutW", SetLastError = true)]
    private static extern nint SendMessageTimeout(nint hwnd, uint msg, nuint wp, nint lp, uint flags, uint timeout, out nuint result);
    [DllImport("user32.dll")] [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool IsWindow(nint hwnd);
    [DllImport("user32.dll")] [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool IsChild(nint parent, nint child);
    [DllImport("user32.dll")] [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool IsWindowEnabled(nint hwnd);
    [DllImport("user32.dll")] [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool IsWindowUnicode(nint hwnd);
    [DllImport("user32.dll", EntryPoint = "GetClassNameW", CharSet = CharSet.Unicode)]
    private static extern int GetClassName(nint hwnd, StringBuilder name, int length);
    [DllImport("user32.dll", EntryPoint = "GetWindowLongPtrW")]
    private static extern nint GetWindowLongPtr(nint hwnd, int index);
    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(nint hwnd, out uint pid);
}
