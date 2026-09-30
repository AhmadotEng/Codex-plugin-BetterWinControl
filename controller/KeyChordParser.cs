using System.Globalization;
using Microsoft.Windows.SDK.BuildTools.WinApp.UIAutomation;

namespace BackgroundControl;

/// <summary>Adapts one keysym-style shortcut to the native backend's numeric VK contract.</summary>
public static class KeyChordParser
{
    private static readonly Dictionary<string, (string ParserName, int Vk)> ModifierAliases = new(StringComparer.OrdinalIgnoreCase)
    {
        ["Control"] = ("ctrl", 0x11), ["Ctrl"] = ("ctrl", 0x11),
        ["Control_L"] = ("ctrl", 0xA2), ["Ctrl_L"] = ("ctrl", 0xA2),
        ["Control_R"] = ("ctrl", 0xA3), ["Ctrl_R"] = ("ctrl", 0xA3),
        ["Shift"] = ("shift", 0x10), ["Shift_L"] = ("shift", 0xA0), ["Shift_R"] = ("shift", 0xA1),
        ["Alt"] = ("alt", 0x12), ["Alt_L"] = ("alt", 0xA4), ["Alt_R"] = ("alt", 0xA5),
    };
    private static readonly Dictionary<string, string> MainAliases = new(StringComparer.OrdinalIgnoreCase)
    {
        ["Page_Up"] = "pageup", ["Prior"] = "pageup", ["Page_Down"] = "pagedown", ["Next"] = "pagedown",
        ["Caps_Lock"] = "capslock", ["Num_Lock"] = "vk=0x90", ["Scroll_Lock"] = "vk=0x91",
        ["KP_Add"] = "vk=0x6B", ["Numpad_Add"] = "vk=0x6B",
        ["KP_Subtract"] = "vk=0x6D", ["Numpad_Subtract"] = "vk=0x6D",
        ["KP_Multiply"] = "vk=0x6A", ["Numpad_Multiply"] = "vk=0x6A",
        ["KP_Divide"] = "vk=0x6F", ["Numpad_Divide"] = "vk=0x6F",
        ["KP_Decimal"] = "vk=0x6E", ["Numpad_Decimal"] = "vk=0x6E",
        ["KP_Separator"] = "vk=0x6C", ["Numpad_Separator"] = "vk=0x6C",
    };

    public static int[] ParseChord(string keysym)
    {
        if (string.IsNullOrWhiteSpace(keysym) || keysym.Length > 256 || keysym.Contains('\0'))
            throw Invalid("Provide one shortcut of 1..256 characters.");
        var parts = keysym.Split('+', StringSplitOptions.TrimEntries);
        if (parts.Length is < 1 or > 8 || parts.Any(p => p.Length == 0 || p.Any(char.IsWhiteSpace)))
            throw Invalid("Use one '+'-separated chord; text and multiple actions are not accepted.");

        var names = new string[parts.Length];
        var specificModifiers = new int[parts.Length - 1];
        for (int i = 0; i < parts.Length - 1; i++)
        {
            if (!ModifierAliases.TryGetValue(parts[i], out var modifier))
                throw Invalid($"Unknown or unsupported modifier '{parts[i]}'.");
            names[i] = modifier.ParserName;
            specificModifiers[i] = modifier.Vk;
        }
        names[^1] = NormalizeMain(parts[^1]);

        IReadOnlyList<KeyAction> actions;
        try { actions = KeyStringParser.Parse(string.Join('+', names)); }
        catch (FormatException error) { throw Invalid(error.Message, error); }
        if (actions.Count != 1 || actions[0] is not KeyChord chord)
            throw Invalid("The key parser returned text or multiple actions; use a named key or explicit virtual key.");
        if (chord.Modifiers.Count != specificModifiers.Length)
            throw Invalid("The dependency parser returned an unexpected modifier structure.");
        for (int i = 0; i < specificModifiers.Length; i++)
            if (chord.Modifiers[i] != GenericModifier(specificModifiers[i]))
                throw Invalid("The dependency parser returned a different modifier.");

        // The native wire protocol carries VK only. It derives extended bits for
        // a fixed set of VKeys; an additional required bit cannot be discarded.
        if (chord.Extended && !NativeExtended(chord.Vk))
            throw Unsupported("This key requires an extended-key distinction absent from the current native protocol.");
        var keys = specificModifiers.Append((int)chord.Vk).ToArray();
        if (keys.Any(vk => vk is < 1 or > 255) || keys.Select(EffectiveKey).Distinct().Count() != keys.Length)
            throw Invalid("A shortcut must contain distinct physical virtual keys.");
        return keys;
    }

    private static string NormalizeMain(string key)
    {
        if (ModifierAliases.TryGetValue(key, out var modifier)) return Raw(modifier.Vk);
        if (MainAliases.TryGetValue(key, out var alias)) return alias;
        if (key.StartsWith("KP_", StringComparison.OrdinalIgnoreCase) || key.StartsWith("Numpad_", StringComparison.OrdinalIgnoreCase))
        {
            var suffix = key[(key.IndexOf('_') + 1)..];
            if (suffix.Length == 1 && suffix[0] is >= '0' and <= '9') return Raw(0x60 + suffix[0] - '0');
            throw Unsupported("Keypad Enter/navigation distinctions are not representable by the current VK-only protocol.");
        }
        if (key.Equals("Menu", StringComparison.OrdinalIgnoreCase)) return "apps";
        if (key.Length == 1)
        {
            char original = key[0];
            if (original is >= 'A' and <= 'Z' or >= 'a' and <= 'z' or >= '0' and <= '9') return Raw(char.ToUpperInvariant(original));
            throw Unsupported("Literal symbols require a target keyboard-layout mapping; use an explicit VK or named key.");
        }
        // Unknown names are intentionally left to the dependency parser. Its
        // TextInput fallback is rejected above, never converted into typing.
        return key;
    }

    private static string Raw(int vk) => "vk=0x" + vk.ToString("X2", CultureInfo.InvariantCulture);
    private static int GenericModifier(int vk) => vk is 0xA0 or 0xA1 ? 0x10 : vk is 0xA2 or 0xA3 ? 0x11 : vk is 0xA4 or 0xA5 ? 0x12 : vk;
    private static int EffectiveKey(int vk) => vk is 0x10 ? 0xA0 : vk is 0x11 ? 0xA2 : vk is 0x12 ? 0xA4 : vk;
    private static bool NativeExtended(int vk) => vk is 0xA3 or 0xA5 or 0x2D or 0x2E or 0x6F or 0x90 || vk is >= 0x21 and <= 0x28;
    private static InvalidOperationException Invalid(string message, Exception? cause = null) => new("invalid_keysym: " + message, cause);
    private static InvalidOperationException Unsupported(string message) => new("unsupported_keysym: " + message);
}
