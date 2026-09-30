# Public dependency key parser tests

Run from the repository root:

```powershell
dotnet run --project tests/key-parser/KeyParserTests.csproj -c Release -- tests/key-parser/results.json
```

The project links the actual `controller/KeyChordParser.cs` and references
`Microsoft.Windows.SDK.BuildTools.WinApp.UIAutomation` 0.7.0. It exercises the
public `KeyStringParser.Parse(string)` and `KeyChord(Modifiers, Vk, Extended)`
types; it does not copy that parser or invoke keyboard injection services.

`ParseChord(string keysym)` returns one ordered `int[]` for the existing
numeric-VK native protocol. Leading modifiers are pressed in order and should
be released in reverse order by the caller. Explicit Control_L/Control_R,
Shift_L/Shift_R and Alt_L/Alt_R retain their side-specific VKs; generic
modifiers retain generic VKs. Duplicate physical aliases are rejected.

ASCII letter names are case-insensitive physical VK names, not text with an
implicit Shift requirement. Explicit Shift remains necessary for a shifted
shortcut. ASCII letters/digits are converted to explicit VK tokens before
calling the dependency, avoiding its single-character active-layout lookup.
Locale-dependent symbols and punctuation aliases require a future target
keyboard-layout adapter; they are rejected rather than guessed. Explicit
`vk=0xNN` is available for callers specifying native VK semantics.

Named navigation keys, Return/Tab/Escape, F1..F16, keypad digits and the mapped
keypad arithmetic keys are supported. Keypad Enter and keypad navigation
cannot preserve their distinct scan/extended-bit identity in the current
native command. PrintScreen/Apps/Windows keys that require an extended flag
the native implementation does not set are also rejected. This adapter does
not imply IME, global shortcut, menu-loop or application-effect support.

The tests check actual package types, representative chords and side-specific
modifiers, numpad distinctions, malformed/multiple/text inputs, duplicate
aliases, unsupported flags, bounds and non-ASCII case-folding ambiguity. They
perform no window, focus, clipboard, capture or input operation.
