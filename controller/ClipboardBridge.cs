using System.Runtime.InteropServices;

namespace BackgroundControl;

// Clipboard access is explicit. Background typing never uses or restores the
// shared clipboard. Writers must supply the current sequence from State/Read.
public sealed class ClipboardBridge : IDisposable
{
    public const int MaxCharacters = 50_000;
    private readonly IClipboardAccess access;
    public ClipboardBridge(Func<Func<nint>, nint> ownerDispatcher) : this(new Win32ClipboardAccess(ownerDispatcher)) { }
    public ClipboardBridge(IClipboardAccess access) => this.access = access;

    public object State()
    {
        using var held = access.Open();
        return new { sequence = access.Sequence, hasText = access.HasText, scope = "shared_system_clipboard" };
    }

    public object Read()
    {
        using var held = access.Open();
        var text = access.HasText ? access.ReadText(MaxCharacters) : null;
        return new { sequence = access.Sequence, text, hasText = text is not null, scope = "shared_system_clipboard" };
    }

    public object Write(string text, uint expectedSequence, Action validateSession)
    {
        if (text.Length > MaxCharacters || text.Contains('\0'))
            throw new InvalidOperationException("invalid_clipboard_text: maximum 50000 characters and no embedded NUL");
        using var held = access.Open();
        if (access.Sequence != expectedSequence)
            throw new InvalidOperationException("clipboard_conflict: clipboard changed; nothing written; read current state before retrying");
        validateSession();
        access.ReplaceText(text);
        return new { written = true, sequence = access.Sequence, characters = text.Length,
            scope = "shared_system_clipboard", automaticRestore = false };
    }

    public void Dispose() => access.Dispose();
}

public interface IClipboardAccess : IDisposable
{
    IDisposable Open();
    uint Sequence { get; }
    bool HasText { get; }
    string ReadText(int maximumCharacters);
    void ReplaceText(string text);
}

internal sealed class Win32ClipboardAccess : IClipboardAccess
{
    const uint UnicodeText = 13, Moveable = 0x0002;
    nint owner;
    bool opened;
    readonly Func<Func<nint>, nint> ownerDispatcher;
    public Win32ClipboardAccess(Func<Func<nint>, nint> ownerDispatcher) => this.ownerDispatcher = ownerDispatcher;

    public uint Sequence => GetClipboardSequenceNumber();
    public bool HasText => IsClipboardFormatAvailable(UnicodeText);

    public IDisposable Open()
    {
        if (opened) throw new InvalidOperationException("clipboard_already_open");
        if (owner == 0)
        {
            // Owner notifications must run on a continuously pumped thread.
            owner = ownerDispatcher(() => CreateWindowEx(0, "STATIC", "BackgroundControl clipboard owner", 0,
                0, 0, 0, 0, new nint(-3), 0, 0, 0));
            if (owner == 0) throw Failure("clipboard_owner_creation_failed");
        }
        if (!OpenClipboard(owner)) throw Failure("clipboard_busy: nothing changed; retry later");
        opened = true;
        return new Lease(this);
    }

    public string ReadText(int maximumCharacters)
    {
        RequireOpen();
        var handle = GetClipboardData(UnicodeText);
        if (handle == 0) throw Failure("clipboard_text_unavailable");
        var size = GlobalSize(handle).ToUInt64();
        var pointer = GlobalLock(handle);
        if (pointer == 0) throw Failure("clipboard_text_lock_failed");
        try
        {
            return DecodeBoundedText(pointer, size, maximumCharacters);
        }
        finally { GlobalUnlock(handle); }
    }

    internal static string DecodeBoundedText(nint pointer, ulong allocationBytes, int maximumCharacters)
    {
        if (allocationBytes < 2) throw new InvalidOperationException("clipboard_text_too_large_or_invalid");
        // GlobalSize reports allocation capacity, which can exceed text length.
        int scanCharacters = checked((int)Math.Min(allocationBytes / 2, (ulong)maximumCharacters + 1));
        var text = Marshal.PtrToStringUni(pointer, scanCharacters) ?? "";
        var terminator = text.IndexOf('\0');
        if (terminator < 0) throw new InvalidOperationException("clipboard_text_too_large_or_not_terminated");
        return text[..terminator];
    }

    public void ReplaceText(string text)
    {
        RequireOpen();
        // Allocate before clearing: allocation failures leave current data intact.
        var memory = GlobalAlloc(Moveable, (nuint)((text.Length + 1) * 2));
        if (memory == 0) throw Failure("clipboard_allocation_failed: nothing changed");
        bool transferred = false;
        try
        {
            var pointer = GlobalLock(memory);
            if (pointer == 0) throw Failure("clipboard_allocation_lock_failed: nothing changed");
            try { Marshal.Copy((text + '\0').ToCharArray(), 0, pointer, text.Length + 1); }
            finally { GlobalUnlock(memory); }
            if (!EmptyClipboard()) throw Failure("clipboard_clear_failed");
            if (SetClipboardData(UnicodeText, memory) == 0)
                throw Failure("clipboard_write_failed_after_clear: clipboard was cleared; no automatic retry or restoration");
            transferred = true;
        }
        finally { if (!transferred) GlobalFree(memory); }
    }

    void RequireOpen()
    {
        if (!opened) throw new InvalidOperationException("clipboard_not_open");
    }
    static InvalidOperationException Failure(string message) => new($"{message} (Win32 {Marshal.GetLastWin32Error()})");
    public void Dispose()
    {
        if (opened) { CloseClipboard(); opened = false; }
        if (owner != 0)
        {
            var closingOwner = owner; owner = 0;
            try { ownerDispatcher(() => { DestroyWindow(closingOwner); return 0; }); }
            catch (Exception ex) when (ex is OperationCanceledException or InvalidOperationException)
            { /* Dispatcher/process shutdown reclaims its windows; never mask an RPC error. */ }
        }
    }
    sealed class Lease(Win32ClipboardAccess clipboard) : IDisposable
    {
        bool disposed;
        public void Dispose()
        {
            if (disposed) return;
            disposed = true;
            CloseClipboard(); clipboard.opened = false;
        }
    }
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern nint CreateWindowEx(uint exStyle, string className, string title, uint style,
        int x, int y, int width, int height, nint parent, nint menu, nint instance, nint parameter);
    [DllImport("user32.dll")] static extern bool DestroyWindow(nint hwnd);
    [DllImport("user32.dll", SetLastError = true)] static extern bool OpenClipboard(nint hwnd);
    [DllImport("user32.dll")] static extern bool CloseClipboard();
    [DllImport("user32.dll")] static extern uint GetClipboardSequenceNumber();
    [DllImport("user32.dll")] static extern bool IsClipboardFormatAvailable(uint format);
    [DllImport("user32.dll", SetLastError = true)] static extern nint GetClipboardData(uint format);
    [DllImport("user32.dll", SetLastError = true)] static extern bool EmptyClipboard();
    [DllImport("user32.dll", SetLastError = true)] static extern nint SetClipboardData(uint format, nint memory);
    [DllImport("kernel32.dll", SetLastError = true)] static extern nint GlobalAlloc(uint flags, nuint bytes);
    [DllImport("kernel32.dll", SetLastError = true)] static extern nint GlobalLock(nint memory);
    [DllImport("kernel32.dll")] static extern bool GlobalUnlock(nint memory);
    [DllImport("kernel32.dll")] static extern nuint GlobalSize(nint memory);
    [DllImport("kernel32.dll")] static extern nint GlobalFree(nint memory);
}
