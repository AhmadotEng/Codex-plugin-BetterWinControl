using System.Diagnostics;
using System.Text.Json;
using BackgroundControl;
using Microsoft.Windows.SDK.BuildTools.WinApp.UIAutomation;

var watch = Stopwatch.StartNew();
var checks = new List<object>();
void Equal(string input, params int[] expected)
{
    var actual = KeyChordParser.ParseChord(input);
    if (!actual.SequenceEqual(expected)) throw new Exception($"{input}: [{string.Join(',', actual)}] != [{string.Join(',', expected)}]");
    checks.Add(new { input, passed = true, keys = actual });
}
void Reject(string? input)
{
    try { KeyChordParser.ParseChord(input!); }
    catch (InvalidOperationException error) when (error.Message.StartsWith("invalid_keysym:") || error.Message.StartsWith("unsupported_keysym:"))
    { checks.Add(new { input, passed = true, rejected = error.Message.Split(':')[0] }); return; }
    throw new Exception("Unsafe/ambiguous chord was accepted: " + input);
}
string? failure = null;
try
{
    // These exercise real dependency types and named-key parsing, not a copied parser.
    var dependency = KeyStringParser.Parse("ctrl+shift+pageup");
    if (dependency.Count != 1 || dependency[0] is not KeyChord { Extended: true, Vk: 0x21 } baseChord ||
        !baseChord.Modifiers.SequenceEqual(new ushort[] { 0x11, 0x10 })) throw new Exception("Unexpected dependency API behavior.");
    if (KeyStringParser.Parse("hello world")[0] is not TextInput) throw new Exception("Dependency text fallback changed.");
    checks.Add(new { name = "actual_public_dependency_types", passed = true });
    Equal("Control_L+Shift_L+p", 162, 160, 80);
    Equal(" Control_L + Shift_R + P ", 162, 161, 80);
    Equal("ctrl+alt+Delete", 17, 18, 46);
    Equal("Control_R+Alt_R+Return", 163, 165, 13);
    Equal("Control_L+Control_R+k", 162, 163, 75);
    Equal("Shift_L+Shift_R+F7", 160, 161, 118);
    Equal("Control_R", 163);
    Equal("Ctrl", 17);
    Equal("Return", 13);
    Equal("space", 32);
    Equal("BackSpace", 8);
    Equal("Control+Page_Up", 17, 33);
    Equal("Alt+Next", 18, 34);
    Equal("KP_0", 96);
    Equal("Control_L+Numpad_9", 162, 105);
    Equal("KP_Divide", 111);
    Equal("KP_Add", 107);
    Equal("Num_Lock", 144);
    Equal("Scroll_Lock", 145);
    Equal("vk=0x41", 65);
    Equal("Ctrl+vk=66", 17, 66);
    Equal("7", 55);
    foreach (var input in new string?[] { null, "", " ", "ctrl++p", "ctrl+", "+Return", "Return Tab", "hello", "hello world",
        "text=enter", "Ctrl+text=p", "Control_L+period", "Control_L+!", "!", "你", "Ctrl+你", "ſ", "ı", "Ctrl+a+b", "Hyper_L+a",
        "KP_Enter", "Ctrl+Numpad_Enter", "KP_Left", "KP_Delete", "PrintScreen", "Menu", "Super_L+p", "Win+p",
        "vk=0x5B", "vk=0x00", "vk=256", "vk=oops", "Ctrl+Control_L+p", "Shift+Shift_L+p", "Ctrl+Ctrl", "p\0", new string('a', 257) }) Reject(input);
}
catch (Exception error) { failure = error.ToString(); }
var result = new { passed = failure is null, failure, count = checks.Count, checks, elapsedMs = watch.ElapsedMilliseconds,
    dependency = typeof(KeyStringParser).Assembly.GetName().FullName,
    scope = "Pure parsing only. No Windows windows, input, capture, helper launch, focus or clipboard operations." };
var json = JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = true });
File.WriteAllText(args.FirstOrDefault() ?? "results.json", json + Environment.NewLine);
Console.WriteLine(JsonSerializer.Serialize(new { result.passed, result.count, result.failure, result.dependency, result.elapsedMs, result.scope }));
return failure is null ? 0 : 1;
