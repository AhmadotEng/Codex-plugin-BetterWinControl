using System.Text.Json;
using System.Runtime.InteropServices;
using BackgroundControl;

// Exercise conflicts and failure paths without inspecting or replacing the
// user's real clipboard. The Win32 adapter is compiled, not live-write tested.
var checks = new List<string>();
void Require(bool condition, string message) { if (!condition) throw new Exception(message); }
void Expect(string part, Action operation)
{
    try { operation(); throw new Exception("Expected " + part); }
    catch (InvalidOperationException ex) { Require(ex.Message.Contains(part), ex.Message); }
}
var fake = new FakeClipboard();
using var clipboard = new ClipboardBridge(fake);
fake.Text = "user copied one";
fake.Sequence = 5;
var snapshot = JsonSerializer.SerializeToElement(clipboard.Read());
Require(snapshot.GetProperty("text").GetString() == fake.Text, "Read content");
Require(snapshot.GetProperty("sequence").GetUInt32() == 5, "Read token");
checks.Add("Read binds text to sequence while clipboard held");

fake.Text = "new user content";
fake.Sequence = 6;
Expect("clipboard_conflict", () => clipboard.Write("agent", 5, () => { }));
Require(fake.Text == "new user content" && fake.Writes == 0 && !fake.Held, "Stale write changed user data");
checks.Add("Concurrent user copy rejects old-token write and preserves new content");

fake.OnOpen = () => { fake.Sequence++; fake.Text = "copy at lock acquisition"; };
Expect("clipboard_conflict", () => clipboard.Write("agent", 6, () => { }));
Require(fake.Text == "copy at lock acquisition" && fake.Writes == 0, "Lock race overwrite");
fake.OnOpen = null;
checks.Add("Sequence checked after acquiring clipboard, including copy-at-acquisition race");

Expect("session_revoked", () => clipboard.Write("agent", fake.Sequence, () => throw new InvalidOperationException("session_revoked")));
Require(fake.Writes == 0 && !fake.Held, "Revoked session wrote data or leaked lock");
checks.Add("Revocation before mutation leaves clipboard intact and releases lock");

fake.Busy = true;
Expect("busy", () => clipboard.Write("agent", fake.Sequence, () => { }));
Require(fake.Writes == 0, "Busy clipboard mutated");
fake.Busy = false;
checks.Add("Busy clipboard fails without retrying or writing");

Expect("invalid_clipboard_text", () => clipboard.Write("a\0b", fake.Sequence, () => { }));
Expect("invalid_clipboard_text", () => clipboard.Write(new string('x', 50_001), fake.Sequence, () => { }));
Require(fake.Writes == 0, "Invalid text mutated");
checks.Add("Embedded NUL and overlong text rejected before mutation");

clipboard.Write("explicit new text", fake.Sequence, () => Require(fake.Held, "Session check outside lock"));
Require(fake.Text == "explicit new text" && fake.Writes == 1 && !fake.Held, "Explicit write");
fake.Text = "later user copy";
fake.Sequence++;
clipboard.Dispose();
Require(fake.Text == "later user copy", "Disposal restored stale data");
checks.Add("Explicit write changes text once; disposal never restores over later user content");

var failing = new FakeClipboard { ThrowOnWrite = true, Text = "before" };
using (var c = new ClipboardBridge(failing))
    Expect("write_failed_after_clear", () => c.Write("replacement", 0, () => { }));
Require(failing.Text == "" && failing.Writes == 1 && !failing.Held, "Write failure handling");
checks.Add("Post-clear native failure propagates honestly without automatic retry or restoration");

var buffer = Marshal.AllocHGlobal(128);
try
{
    Marshal.Copy(("ok\0" + new string('x', 61)).ToCharArray(), 0, buffer, 64);
    Require(Win32ClipboardAccess.DecodeBoundedText(buffer, 128, 3) == "ok", "Allocation padding treated as text length");
    Marshal.Copy("abcd".ToCharArray(), 0, buffer, 4);
    Expect("too_large_or_not_terminated", () => Win32ClipboardAccess.DecodeBoundedText(buffer, 128, 3));
    Expect("too_large_or_invalid", () => Win32ClipboardAccess.DecodeBoundedText(buffer, 1, 3));
    checks.Add("Production text decoder ignores allocation padding and bounds unterminated/invalid scans");
}
finally { Marshal.FreeHGlobal(buffer); }

Console.WriteLine(JsonSerializer.Serialize(new { status = "passed", checks,
    scope = "Clipboard concurrency/session checks against controlled fault-injecting adapter",
    liveSystemClipboardRead = false, liveSystemClipboardChanged = false,
    limitation = "Win32 read/write integration not established by this test" }));

sealed class FakeClipboard : IClipboardAccess
{
    public uint Sequence { get; set; }
    public string? Text;
    public bool Held, Busy, ThrowOnWrite;
    public int Writes;
    public Action? OnOpen;
    public bool HasText => Text is not null;
    public IDisposable Open()
    {
        if (Busy || Held) throw new InvalidOperationException("clipboard_busy");
        OnOpen?.Invoke(); Held = true; return new Release(() => Held = false);
    }
    public string ReadText(int maximumCharacters) => Held ? Text! : throw new Exception("Unlocked read");
    public void ReplaceText(string text)
    {
        if (!Held) throw new Exception("Unlocked write");
        Writes++; Sequence++; Text = "";
        if (ThrowOnWrite) throw new InvalidOperationException("clipboard_write_failed_after_clear");
        Text = text;
    }
    public void Dispose() { }
    sealed class Release(Action action) : IDisposable { public void Dispose() => action(); }
}
