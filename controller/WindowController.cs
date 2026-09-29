using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using Interop.UIAutomationClient;

namespace BackgroundControl;

/// <summary>All UIA methods run on the host's serialized MTA worker. Pause/Stop are nonblocking.</summary>
public sealed class WindowController : IDisposable
{
    private readonly IUIAutomation2 automation;
    private readonly IUIAutomationCacheRequest cache;
    private readonly IUIAutomationTreeWalker walker;
    private Target? target;
    private int paused = 1, disposed;
    private long epoch;
    private string? observationId;
    private long observationEpoch;
    private DateTimeOffset observationExpiry;
    private readonly Dictionary<string, ElementBinding> elements = new(StringComparer.Ordinal);
    private static readonly TimeSpan ObservationLifetime = TimeSpan.FromSeconds(45);
    private const int MaxDepth = 32;
    private static readonly (int Property, string[] Actions)[] PatternProperties =
    [
        (30031, ["invoke"]), (30043, ["set_value"]), (30041, ["toggle"]),
        (30036, ["select"]), (30028, ["expand", "collapse"]), (30034, ["scroll"]),
        (30090, ["legacy"])
    ];
    private static readonly HashSet<string> DeniedProcesses = new(StringComparer.OrdinalIgnoreCase)
    {
        "codex", "chatgpt", "windowsterminal", "wt", "cmd", "powershell", "pwsh", "conhost", "openconsole",
        "bash", "wsl", "mintty", "putty", "terminal", "wezterm", "alacritty", "credentialuibroker", "consent",
        "logonui", "lockapp", "winlogon", "sechealthui", "securityhealthsystray", "msmpeng", "mrt",
        "systemsettings", "mmc", "regedit", "1password", "keepass", "keepassxc", "bitwarden", "lastpass",
        "dashlane", "norton", "avastui", "avgui", "mbam", "mcafee", "kaspersky"
    };
    private static readonly Regex ProtectedText = new(
        @"\b(password|passcode|credential|sign[ -]?in|log[ -]?in|two[ -]?factor|authentication|security settings|privacy settings|windows security|user account control|verify your identity|age verification)\b",
        RegexOptions.IgnoreCase | RegexOptions.CultureInvariant, TimeSpan.FromMilliseconds(50));
    private static readonly Regex ProtectedControlName = new(
        @"^(?:codex|chatgpt|terminal|command prompt|powershell|developer console|javascript console|console input)(?:\s*[-:—].*)?$",
        RegexOptions.IgnoreCase | RegexOptions.CultureInvariant, TimeSpan.FromMilliseconds(50));

    public WindowController()
    {
        if (Thread.CurrentThread.GetApartmentState() != ApartmentState.MTA)
            throw Error("wrong_apartment", "Create and use WindowController on a serialized MTA worker.");
        automation = (IUIAutomation2)new CUIAutomation8();
        automation.AutoSetFocus = 0;
        automation.ConnectionTimeout = 500;
        automation.TransactionTimeout = 750;
        if (automation.AutoSetFocus != 0) throw Error("focus_guard_unavailable", "AutoSetFocus could not be disabled.");
        walker = automation.ControlViewWalker;
        cache = automation.CreateCacheRequest();
        cache.TreeScope = TreeScope.TreeScope_Element;
        foreach (var property in new[] { 30001, 30002, 30003, 30004, 30005, 30010, 30011, 30012, 30019, 30020, 30022 }
            .Concat(PatternProperties.Select(p => p.Property))) cache.AddProperty(property);
    }

    public long TargetHwnd => Volatile.Read(ref target)?.Hwnd ?? 0;
    public string TargetTitle => Volatile.Read(ref target)?.Title ?? "";
    public bool Paused => Volatile.Read(ref paused) != 0;

    public object ListWindows()
    {
        CheckDisposed();
        var windows = new List<object>();
        Native.EnumWindows((hwnd, _) =>
        {
            if (!Native.IsWindowVisible(hwnd) || Native.GetWindowTextLength(hwnd) == 0) return true;
            try
            {
                var identity = ReadIdentity(hwnd.ToInt64());
                string? denied = Denial(identity);
                windows.Add(new { hwnd = identity.Hwnd, pid = identity.Pid, title = identity.Title,
                    process = identity.ProcessName, selectable = denied is null, blockedReason = denied,
                    ownedBy = Native.GetWindow(hwnd, 4).ToInt64(), minimized = Native.IsIconic(hwnd) });
            }
            catch (Exception ex) when (ex is InvalidOperationException or System.ComponentModel.Win32Exception or ArgumentException) { }
            return windows.Count < 500;
        }, nint.Zero);
        return new { windows, scope = "visible_top_level_windows", desktopAutomationTreeScanned = false };
    }

    public object Attach(long hwnd)
    {
        CheckDisposed();
        var before = Native.Snapshot();
        var attachEpoch = Volatile.Read(ref epoch);
        var previous = Volatile.Read(ref target);
        var identity = ReadIdentity(hwnd);
        if (!Native.IsWindowVisible(new nint(hwnd)) || Native.GetAncestor(new nint(hwnd), 2).ToInt64() != hwnd)
            throw Error("invalid_target", "Select a visible top-level application window returned by ListWindows.");
        RejectDenied(identity);
        var lease = WindowLease.Acquire(identity.Hwnd, identity.Pid, identity.StartTicks, identity.ThreadId, previous?.Lease);
        bool committed = false;
        try
        {
            var root = automation.ElementFromHandle(new nint(hwnd));
            if (root.CurrentProcessId != identity.Pid) throw Error("scope_mismatch", "UIA root PID does not match the selected window.");
            RejectSensitive(root);
            var runtime = RuntimeId(root);
            if (runtime.Length == 0) throw Error("identity_unavailable", "Selected root has no UIA runtime identity.");
            identity = identity with { RuntimeId = runtime, Lease = lease };
            if (lease.IsReleased || Interlocked.CompareExchange(ref epoch, attachEpoch + 1, attachEpoch) != attachEpoch)
                throw Error("revoked", "Attachment was cancelled.");
            if (!ReferenceEquals(Interlocked.CompareExchange(ref target, identity, previous), previous))
                throw Error("revoked", "Target changed while attaching.");
            committed = true;
            previous?.Lease?.Dispose();
            Volatile.Write(ref paused, 0);
            if (Volatile.Read(ref epoch) != attachEpoch + 1 || !ReferenceEquals(Volatile.Read(ref target), identity) || lease.IsReleased)
            {
                Volatile.Write(ref paused, 1);
                if (ReferenceEquals(Interlocked.CompareExchange(ref target, null, identity), identity)) lease.Dispose();
                throw Error("revoked", "Attachment was cancelled.");
            }
            elements.Clear();
            observationId = null;
            var after = Native.Snapshot();
            CheckFocus(before, after, identity);
            return new { attached = true, window = WindowInfo(identity), autoSetFocus = automation.AutoSetFocus,
                paused = Paused, focusBefore = before, focusAfter = after,
                exclusiveWindowLease = true, providerTimeoutsMs = new { connection = 500, transaction = 750 },
                limitation = "Provider semantic actions may themselves change focus; a detected change pauses control without restoring focus." };
        }
        finally
        {
            // Failed replacement must not release the original target's lease.
            if (!committed) lease.Dispose();
        }
    }

    public object Observe(int maxElements = 200)
    {
        CheckDisposed();
        maxElements = Math.Clamp(maxElements, 1, 200);
        var current = ValidateTarget();
        var operationEpoch = Volatile.Read(ref epoch);
        var before = Native.Snapshot();
        elements.Clear();
        observationId = null;
        var result = new List<object>();
        var warnings = new List<string>();
        var roots = ScopeWindows(current);
        var watch = Stopwatch.StartNew();
        bool truncated = false;
        var visited = new HashSet<string>(StringComparer.Ordinal);
        foreach (var rootIdentity in roots)
        {
            if (result.Count >= maxElements || watch.ElapsedMilliseconds > 4000) { truncated = true; break; }
            RequireEpoch(operationEpoch, current, allowPaused: true);
            var root = automation.ElementFromHandleBuildCache(new nint(rootIdentity.Hwnd), cache);
            var queue = new Queue<(IUIAutomationElement Element, int Depth, string? Parent)>();
            queue.Enqueue((root, 0, null));
            while (queue.Count > 0)
            {
                RequireEpoch(operationEpoch, current, allowPaused: true);
                if (result.Count >= maxElements || watch.ElapsedMilliseconds > 4000) { truncated = true; break; }
                var (element, depth, parent) = queue.Dequeue();
                try
                {
                    var runtime = RuntimeId(element);
                    var runtimeKey = rootIdentity.Hwnd + ":" + string.Join(",", runtime);
                    if (!visited.Add(runtimeKey)) continue;
                    var password = element.CachedIsPassword != 0 || LegacyProtected(element);
                    var name = Limit(element.CachedName);
                    var sensitive = password || ProtectedText.IsMatch(name) || ProtectedControlName.IsMatch(name);
                    var id = "e" + (result.Count + 1);
                    var bounds = element.CachedBoundingRectangle;
                    var typeId = element.CachedControlType;
                    var actions = sensitive ? Array.Empty<string>() : CachedActions(element);
                    var observedValue = sensitive ? (Text: (string?)null, ReadOnly: (bool?)null) : ObserveValue(element, actions);
                    var nativeHwnd = element.CachedNativeWindowHandle;
                    if (!sensitive && NativeEditActions.Supports(nativeHwnd, new nint(rootIdentity.Hwnd), rootIdentity.Pid,
                        isPasswordOrProtected: false, isReadOnly: observedValue.ReadOnly ?? false))
                        actions = actions.Append("insert_text").ToArray();
                    result.Add(new { id, parent, name = password ? "[protected]" : name,
                        automationId = password ? "" : Limit(element.CachedAutomationId),
                        controlType = element.CachedLocalizedControlType, controlTypeId = typeId,
                        bounds = new { left = bounds.left, top = bounds.top, right = bounds.right, bottom = bounds.bottom },
                        enabled = element.CachedIsEnabled != 0, offscreen = element.CachedIsOffscreen != 0,
                        isPassword = password, protectedElement = sensitive, patterns = actions, rootHwnd = rootIdentity.Hwnd,
                        value = observedValue.Text, readOnly = observedValue.ReadOnly });
                    if (!sensitive && runtime.Length > 0)
                        elements[id] = new(element, rootIdentity, runtime, name, element.CachedAutomationId,
                            typeId, bounds.left, bounds.top, bounds.right, bounds.bottom, SemanticFingerprint(element, actions), nativeHwnd);
                    // Never descend into protected fields or authentication/security subtrees.
                    if (sensitive) continue;
                    if (depth >= MaxDepth) { truncated = true; continue; }
                    var children = new List<IUIAutomationElement>();
                    var child = walker.GetFirstChildElementBuildCache(element, cache);
                    var siblingIds = new HashSet<string>();
                    while (child is not null && children.Count < maxElements && watch.ElapsedMilliseconds <= 4000)
                    {
                        RequireEpoch(operationEpoch, current, allowPaused: true);
                        if (!siblingIds.Add(string.Join(",", RuntimeId(child)))) { warnings.Add("Provider returned a repeating sibling identity."); break; }
                        children.Add(child);
                        child = walker.GetNextSiblingElementBuildCache(child, cache);
                    }
                    if (child is not null) truncated = true;
                    foreach (var childElement in children) queue.Enqueue((childElement, depth + 1, id));
                }
                catch (COMException ex) { warnings.Add("Element unavailable: HRESULT 0x" + ex.HResult.ToString("X8")); }
            }
        }
        ValidateTarget();
        RequireEpoch(operationEpoch, current, allowPaused: true);
        var after = Native.Snapshot();
        CheckFocus(before, after, current);
        observationId = Guid.NewGuid().ToString("N");
        observationEpoch = Volatile.Read(ref epoch);
        observationExpiry = DateTimeOffset.UtcNow + ObservationLifetime;
        return new { observationId, window = WindowInfo(current), elements = result, truncated, traversal = "breadth_first",
            warnings = warnings.Distinct().Take(10).ToArray(), expiresAt = observationExpiry,
            paused = Paused, focusBefore = before, focusAfter = after,
            scope = roots.Select(r => new { hwnd = r.Hwnd, pid = r.Pid }).ToArray() };
    }

    public object Act(string observationId, string elementId, string action, string? value = null, double amount = 0)
    {
        CheckDisposed();
        var current = ValidateTarget();
        if (Paused) throw Error("paused", "Resume and obtain a new observation before acting.");
        using var leaseHold = current.Lease!.Hold();
        var operationEpoch = Volatile.Read(ref epoch);
        if (this.observationId != observationId || observationEpoch != operationEpoch || DateTimeOffset.UtcNow >= observationExpiry)
            throw Error("stale_observation", "Obtain a fresh observation; observations expire and are invalidated after actions or pause/stop.");
        if (!elements.TryGetValue(elementId, out var binding)) throw Error("unknown_element", "Element is absent or protected in this observation.");
        action = action.Trim().ToLowerInvariant();
        var before = Native.Snapshot();
        bool enteredProvider = false;
        bool verified = false;
        string mechanism = "";
        object? evidence = null;
        try
        {
            ValidateBinding(binding, current);
            var element = binding.Element;
            if (element.CurrentIsEnabled == 0) throw Error("disabled_element", "Selected element is disabled.");
            var actions = CurrentActions(element);
            if (NativeEditActions.Supports(element.CurrentNativeWindowHandle, new nint(binding.Root.Hwnd), binding.Root.Pid,
                isPasswordOrProtected: false, isReadOnly: ReadOnlyState(element))) actions = actions.Append("insert_text").ToArray();
            void GuardMutation()
            {
                // This check is immediately adjacent to COM entry. An in-flight provider call cannot be undone.
                ValidateTarget();
                RequireEpoch(operationEpoch, current, allowPaused: false);
                RejectSensitive(element);
                enteredProvider = true;
            }
            void Mutate(Action mutation)
            {
                GuardMutation();
                mutation();
            }
            switch (action)
            {
                case "insert_text":
                    if (value is null) throw Error("invalid_value", "insert_text requires a value.");
                    if (!actions.Contains("insert_text")) throw Unsupported(action, actions);
                    mechanism = "NativeEdit.EM_REPLACESEL";
                    NativeEditActions.Insert(element.CurrentNativeWindowHandle, new nint(binding.Root.Hwnd), binding.Root.Pid,
                        isPasswordOrProtected: false, isReadOnly: ReadOnlyState(element), text: value, beforeMutation: GuardMutation);
                    evidence = new { requestedLength = value.Length, selection = "control_current_selection",
                        note = "Native message returned without a success flag. Verify resulting text with a fresh observation." };
                    break;
                case "invoke":
                    if (Pattern<IUIAutomationInvokePattern>(element, 10000) is { } invoke)
                    { mechanism = "InvokePattern.Invoke"; Mutate(invoke.Invoke); }
                    else if (Pattern<IUIAutomationLegacyIAccessiblePattern>(element, 10018) is { } legacyInvoke &&
                        !string.IsNullOrWhiteSpace(legacyInvoke.CurrentDefaultAction))
                    { CheckLegacy(legacyInvoke); mechanism = "LegacyIAccessible.DoDefaultAction"; Mutate(legacyInvoke.DoDefaultAction); }
                    else throw Unsupported(action, actions);
                    evidence = new { note = "Provider returned. Verify resulting application state with Observe; this does not prove the requested task succeeded." };
                    break;
                case "set_value":
                    if (value is null) throw Error("invalid_value", "set_value requires a value.");
                    if (value.Length > 100000) throw Error("invalid_value", "Value exceeds the 100000-character limit.");
                    if (Pattern<IUIAutomationValuePattern>(element, 10002) is { } val)
                    {
                        if (val.CurrentIsReadOnly != 0) throw Error("read_only", "ValuePattern is read-only.");
                        mechanism = "ValuePattern.SetValue"; Mutate(() => val.SetValue(value));
                        RejectSensitive(element); verified = val.CurrentValue == value;
                    }
                    else if (Pattern<IUIAutomationLegacyIAccessiblePattern>(element, 10018) is { } legacyValue && legacyValue.CurrentRole == 42)
                    {
                        CheckLegacy(legacyValue);
                        if ((legacyValue.CurrentState & 0x40) != 0) throw Error("read_only", "Legacy editable control is read-only.");
                        mechanism = "LegacyIAccessible.SetValue"; Mutate(() => legacyValue.SetValue(value));
                        RejectSensitive(element); CheckLegacy(legacyValue); verified = legacyValue.CurrentValue == value;
                    }
                    else throw Unsupported(action, actions);
                    evidence = new { valueMatches = verified, requestedLength = value.Length };
                    break;
                case "toggle":
                    if (Pattern<IUIAutomationTogglePattern>(element, 10015) is not { } toggle) throw Unsupported(action, actions);
                    var oldToggle = toggle.CurrentToggleState;
                    mechanism = "TogglePattern.Toggle"; Mutate(toggle.Toggle);
                    var newToggle = toggle.CurrentToggleState;
                    verified = oldToggle != newToggle; evidence = new { before = oldToggle.ToString(), after = newToggle.ToString() };
                    break;
                case "select":
                    if (Pattern<IUIAutomationSelectionItemPattern>(element, 10010) is { } select)
                    { mechanism = "SelectionItemPattern.Select"; Mutate(select.Select); verified = select.CurrentIsSelected != 0; }
                    else if (Pattern<IUIAutomationLegacyIAccessiblePattern>(element, 10018) is { } legacySelect && (legacySelect.CurrentState & 0x200000) != 0)
                    { CheckLegacy(legacySelect); mechanism = "LegacyIAccessible.Select(TAKESELECTION)"; Mutate(() => legacySelect.Select(2)); verified = (legacySelect.CurrentState & 2) != 0; }
                    else throw Unsupported(action, actions);
                    evidence = new { selected = verified }; break;
                case "expand":
                case "collapse":
                    if (Pattern<IUIAutomationExpandCollapsePattern>(element, 10005) is not { } expand) throw Unsupported(action, actions);
                    if (expand.CurrentExpandCollapseState == ExpandCollapseState.ExpandCollapseState_LeafNode) throw Unsupported(action, actions);
                    mechanism = "ExpandCollapsePattern." + action;
                    if (action == "expand") Mutate(expand.Expand); else Mutate(expand.Collapse);
                    verified = expand.CurrentExpandCollapseState == (action == "expand" ? ExpandCollapseState.ExpandCollapseState_Expanded : ExpandCollapseState.ExpandCollapseState_Collapsed);
                    evidence = new { state = expand.CurrentExpandCollapseState.ToString() }; break;
                case "scroll":
                    if (Pattern<IUIAutomationScrollPattern>(element, 10004) is not { } scroll) throw Unsupported(action, actions);
                    var direction = (value ?? (amount < 0 ? "up" : "down")).ToLowerInvariant();
                    if (direction is not ("up" or "down" or "left" or "right") || !double.IsFinite(amount))
                        throw Error("invalid_scroll", "Direction must be up/down/left/right and amount must be finite.");
                    var horizontal = direction is "left" or "right";
                    if ((horizontal ? scroll.CurrentHorizontallyScrollable : scroll.CurrentVerticallyScrollable) == 0)
                        throw Unsupported(action + "_" + direction, actions);
                    var oldPercent = horizontal ? scroll.CurrentHorizontalScrollPercent : scroll.CurrentVerticalScrollPercent;
                    bool increase = direction is "down" or "right", large = Math.Abs(amount) >= 2;
                    var delta = increase ? (large ? ScrollAmount.ScrollAmount_LargeIncrement : ScrollAmount.ScrollAmount_SmallIncrement)
                        : (large ? ScrollAmount.ScrollAmount_LargeDecrement : ScrollAmount.ScrollAmount_SmallDecrement);
                    mechanism = "ScrollPattern.Scroll";
                    Mutate(() => scroll.Scroll(horizontal ? delta : ScrollAmount.ScrollAmount_NoAmount, horizontal ? ScrollAmount.ScrollAmount_NoAmount : delta));
                    var newPercent = horizontal ? scroll.CurrentHorizontalScrollPercent : scroll.CurrentVerticalScrollPercent;
                    verified = newPercent != oldPercent; evidence = new { direction, beforePercent = oldPercent, afterPercent = newPercent }; break;
                default: throw Unsupported(action, actions);
            }
            var after = Native.Snapshot();
            CheckFocus(before, after, current);
            bool revokedDuringAction = Volatile.Read(ref epoch) != operationEpoch || !ReferenceEquals(Volatile.Read(ref target), current);
            if (enteredProvider) InvalidateObservation();
            return new { outcome = "provider_completed", action, mechanism, verified, evidence,
                paused = Paused, revokedDuringAction, focusBefore = before, focusAfter = after,
                next = "Observe again before any further action.",
                focusEvidence = "Before/after samples cannot exclude brief transitions or attribute a concurrent user's focus change." };
        }
        catch (Exception ex)
        {
            var after = Native.Snapshot();
            CheckFocus(before, after, current);
            if (enteredProvider) InvalidateObservation();
            if (enteredProvider && action == "insert_text") Pause();
            ex.Data["providerMutationEntered"] = enteredProvider;
            ex.Data["focusBefore"] = before;
            ex.Data["focusAfter"] = after;
            ex.Data["paused"] = Paused;
            if (ex is COMException)
            {
                Pause();
                var wrapped = Error("provider_error", $"HRESULT 0x{ex.HResult:X8}; action outcome {(enteredProvider ? "unknown; observe before retrying" : "not submitted")}. Control paused.");
                foreach (System.Collections.DictionaryEntry item in ex.Data) wrapped.Data[item.Key] = item.Value;
                wrapped.Data["paused"] = true;
                throw wrapped;
            }
            throw;
        }
    }

    public object Pause()
    {
        Volatile.Write(ref paused, 1);
        Interlocked.Increment(ref epoch);
        return new { paused = true, inFlightProviderCallMayFinish = true };
    }

    public object Resume()
    {
        CheckDisposed();
        // The preview invokes this on its UI thread. Full UIA validation runs only on the next worker operation.
        var current = Volatile.Read(ref target) ?? throw Error("stopped", "Select a window again.");
        if (current.Lease is null || !current.Lease.IsScopeCurrent()) { Stop(); throw Error("lease_lost", "Window lease is no longer held."); }
        var nativeIdentity = ReadIdentity(current.Hwnd);
        if (nativeIdentity.Pid != current.Pid || nativeIdentity.StartTicks != current.StartTicks ||
            nativeIdentity.ThreadId != current.ThreadId || nativeIdentity.ClassName != current.ClassName)
        { Stop(); throw Error("stale_target", "Window identity changed; select it again."); }
        RejectDenied(nativeIdentity);
        var resumedEpoch = Interlocked.Increment(ref epoch);
        Volatile.Write(ref paused, 0);
        // A concurrently delivered Stop must not be resurrected.
        if (!ReferenceEquals(Volatile.Read(ref target), current) || Volatile.Read(ref epoch) != resumedEpoch)
        { Volatile.Write(ref paused, 1); throw Error("revoked", "Resume was cancelled by pause or stop."); }
        return new { paused = false, observationRequired = true };
    }

    public object Stop()
    {
        Volatile.Write(ref paused, 1);
        var previous = Interlocked.Exchange(ref target, null);
        Interlocked.Increment(ref epoch);
        previous?.Lease?.Dispose();
        return new { stopped = true, targetRevoked = true, inFlightProviderCallMayFinish = true };
    }

    public object State()
    {
        var current = Volatile.Read(ref target);
        if (current is not null)
        {
            try { current = ValidateTarget(); }
            catch (InvalidOperationException) { current = null; }
        }
        return new { attached = current is not null, paused = Paused, window = current is null ? null : WindowInfo(current),
            observationId = observationEpoch == Volatile.Read(ref epoch) && DateTimeOffset.UtcNow < observationExpiry ? observationId : null,
            actionTransport = "UIA semantic providers only", physicalInputInjection = false };
    }

    private Target ValidateTarget()
    {
        CheckDisposed();
        var current = Volatile.Read(ref target) ?? throw Error("stopped", "No attached window. Select a window explicitly.");
        try
        {
            if (current.Lease is null || !current.Lease.IsScopeCurrent()) throw Error("lease_lost", "Window lease is no longer held; attach again.");
            var now = ReadIdentity(current.Hwnd);
            if (now.Pid != current.Pid || now.StartTicks != current.StartTicks || now.ThreadId != current.ThreadId || now.ClassName != current.ClassName)
                throw Error("stale_target", "HWND/process identity changed.");
            RejectDenied(now);
            if (!RuntimeId(automation.ElementFromHandle(new nint(current.Hwnd))).SequenceEqual(current.RuntimeId))
                throw Error("stale_target", "Selected UIA root identity changed.");
            return current;
        }
        catch (Exception ex) when (ex is InvalidOperationException or COMException or ArgumentException or System.ComponentModel.Win32Exception)
        {
            Stop();
            if (ex is InvalidOperationException) throw;
            throw Error("stale_target", "Selected process/window is no longer available; select it again.");
        }
    }

    private void ValidateBinding(ElementBinding binding, Target current)
    {
        var rootIdentity = ReadIdentity(binding.Root.Hwnd);
        if (rootIdentity.Pid != binding.Root.Pid || rootIdentity.StartTicks != binding.Root.StartTicks ||
            !IsInScope(binding.Root.Hwnd, current)) throw Error("stale_element", "Element window identity is no longer in the selected scope.");
        RejectDenied(rootIdentity);
        var element = binding.Element;
        if (!RuntimeId(element).SequenceEqual(binding.RuntimeId) || Limit(element.CurrentName) != binding.Name ||
            element.CurrentAutomationId != binding.AutomationId || element.CurrentControlType != binding.Type ||
            element.CurrentNativeWindowHandle != binding.NativeHwnd)
            throw Error("stale_element", "Element identity or label changed. Observe again.");
        var rectangle = element.CurrentBoundingRectangle;
        if (rectangle.left != binding.Left || rectangle.top != binding.Top || rectangle.right != binding.Right || rectangle.bottom != binding.Bottom)
            throw Error("stale_element", "Element geometry changed. Observe again.");
        if (SemanticFingerprint(element, CurrentActions(element)) != binding.SemanticState)
            throw Error("stale_element", "Element value or semantic state changed. Observe again.");
        var root = automation.ElementFromHandle(new nint(binding.Root.Hwnd));
        var ancestor = element;
        for (int i = 0; i <= MaxDepth + 2 && ancestor is not null; i++)
        {
            RejectSensitive(ancestor);
            if (automation.CompareElements(ancestor, root) != 0) return;
            ancestor = walker.GetParentElement(ancestor);
        }
        throw Error("scope_mismatch", "Element no longer descends from its observed selected window.");
    }

    private List<Target> ScopeWindows(Target current)
    {
        var roots = new List<Target> { current };
        Native.EnumWindows((hwnd, _) =>
        {
            if (hwnd.ToInt64() == current.Hwnd || !Native.IsWindowVisible(hwnd) || !IsInScope(hwnd.ToInt64(), current)) return true;
            try { var identity = ReadIdentity(hwnd.ToInt64()); if (Denial(identity) is null) roots.Add(identity); }
            catch (Exception ex) when (ex is InvalidOperationException or ArgumentException or System.ComponentModel.Win32Exception) { }
            return roots.Count < 16;
        }, nint.Zero);
        return roots;
    }

    private static bool IsInScope(long hwnd, Target current)
    {
        var handle = new nint(hwnd);
        Native.GetWindowThreadProcessId(handle, out var pid);
        if (pid != current.Pid) return false;
        for (int i = 0; handle != nint.Zero && i < 16; i++, handle = Native.GetWindow(handle, 4))
            if (handle.ToInt64() == current.Hwnd) return true;
        return false;
    }

    private static Target ReadIdentity(long hwnd)
    {
        var handle = new nint(hwnd);
        if (hwnd == 0 || !Native.IsWindow(handle)) throw Error("invalid_target", "Window no longer exists.");
        var thread = Native.GetWindowThreadProcessId(handle, out var pid);
        if (thread == 0 || pid == 0) throw Error("invalid_target", "Window process identity unavailable.");
        using var process = Process.GetProcessById(checked((int)pid));
        var title = new StringBuilder(Math.Min(Native.GetWindowTextLength(handle) + 1, 4096));
        Native.GetWindowText(handle, title, title.Capacity);
        var className = new StringBuilder(256);
        Native.GetClassName(handle, className, className.Capacity);
        return new(hwnd, (int)pid, process.StartTime.ToUniversalTime().Ticks, thread, process.ProcessName,
            title.ToString(), className.ToString(), []);
    }

    private static string? Denial(Target item)
    {
        if (item.Pid == Environment.ProcessId) return "controller_process";
        if (DeniedProcesses.Contains(item.ProcessName) || item.ProcessName.Contains("codex", StringComparison.OrdinalIgnoreCase) ||
            item.ProcessName.Contains("credential", StringComparison.OrdinalIgnoreCase)) return "protected_application";
        if (item.ClassName is "ConsoleWindowClass" or "CASCADIA_HOSTING_WINDOW_CLASS" ||
            item.ClassName == "#32770" && ProtectedText.IsMatch(item.Title))
            return "protected_application";
        if (ProtectedText.IsMatch(item.Title)) return "authentication_or_security_window";
        return null;
    }

    private static void RejectDenied(Target item)
    { if (Denial(item) is { } reason) throw Error("protected_target", reason); }
    private static void RejectSensitive(IUIAutomationElement element)
    {
        if (element.CurrentIsPassword != 0 || ProtectedText.IsMatch(element.CurrentName ?? "") ||
            ProtectedControlName.IsMatch(element.CurrentName ?? "") || LegacyProtected(element))
            throw Error("protected_element", "Authentication, password and security controls require user takeover.");
    }
    private static bool LegacyProtected(IUIAutomationElement element) =>
        IsTrue(element.GetCurrentPropertyValue(30090)) && Pattern<IUIAutomationLegacyIAccessiblePattern>(element, 10018) is { } legacy &&
        (legacy.CurrentState & 0x20000000) != 0;
    private static (string? Text, bool? ReadOnly) ObserveValue(IUIAutomationElement element, string[] actions)
    {
        if (!actions.Contains("set_value") && !actions.Contains("legacy")) return (null, null);
        RejectSensitive(element);
        if (actions.Contains("set_value") && Pattern<IUIAutomationValuePattern>(element, 10002) is { } value)
        {
            RejectSensitive(element);
            return (LimitValue(value.CurrentValue), value.CurrentIsReadOnly != 0);
        }
        if (actions.Contains("legacy") && Pattern<IUIAutomationLegacyIAccessiblePattern>(element, 10018) is { } legacy && legacy.CurrentRole == 42)
        {
            RejectSensitive(element); CheckLegacy(legacy);
            return (LimitValue(legacy.CurrentValue), (legacy.CurrentState & 0x40) != 0);
        }
        return (null, null);
    }
    private static bool ReadOnlyState(IUIAutomationElement element)
    {
        if (Pattern<IUIAutomationValuePattern>(element, 10002) is { } value) return value.CurrentIsReadOnly != 0;
        if (Pattern<IUIAutomationLegacyIAccessiblePattern>(element, 10018) is { } legacy)
        { CheckLegacy(legacy); return (legacy.CurrentState & 0x40) != 0; }
        // The native adapter independently rejects ES_READONLY and protected controls.
        return false;
    }
    private static string SemanticFingerprint(IUIAutomationElement element, string[] actions)
    {
        // Value is requested only after checking IsPassword, never cached for all elements or returned to the caller.
        RejectSensitive(element);
        var parts = new List<string>();
        if (actions.Contains("set_value") && Pattern<IUIAutomationValuePattern>(element, 10002) is { } value)
            parts.Add("v:" + Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(Encoding.UTF8.GetBytes(value.CurrentValue ?? ""))));
        else if (actions.Contains("legacy") && Pattern<IUIAutomationLegacyIAccessiblePattern>(element, 10018) is { } legacy && legacy.CurrentRole == 42)
        {
            CheckLegacy(legacy);
            parts.Add("v:" + Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(Encoding.UTF8.GetBytes(legacy.CurrentValue ?? ""))));
        }
        if (actions.Contains("toggle") && Pattern<IUIAutomationTogglePattern>(element, 10015) is { } toggle)
            parts.Add("t:" + (int)toggle.CurrentToggleState);
        if (actions.Contains("select") && Pattern<IUIAutomationSelectionItemPattern>(element, 10010) is { } selected)
            parts.Add("s:" + selected.CurrentIsSelected);
        if (actions.Contains("expand") && Pattern<IUIAutomationExpandCollapsePattern>(element, 10005) is { } expanded)
            parts.Add("e:" + (int)expanded.CurrentExpandCollapseState);
        return string.Join(";", parts);
    }
    private static void CheckLegacy(IUIAutomationLegacyIAccessiblePattern pattern)
    { if ((pattern.CurrentState & 0x20000000) != 0) throw Error("protected_element", "Legacy control is marked protected."); }
    private static T? Pattern<T>(IUIAutomationElement element, int patternId) where T : class
    {
        try { return element.GetCurrentPattern(patternId) as T; }
        catch (COMException ex) when (ex.HResult == unchecked((int)0x80040204) || ex.HResult == unchecked((int)0x80004002)) { return null; }
    }
    private static string[] CachedActions(IUIAutomationElement element) => PatternProperties
        .Where(p => IsTrue(element.GetCachedPropertyValue(p.Property))).SelectMany(p => p.Actions).Distinct().ToArray();
    private static string[] CurrentActions(IUIAutomationElement element) => PatternProperties
        .Where(p => IsTrue(element.GetCurrentPropertyValue(p.Property))).SelectMany(p => p.Actions).Distinct().ToArray();
    private static bool IsTrue(object value) => value is bool b ? b : value is int n && n != 0;
    private static int[] RuntimeId(IUIAutomationElement element) => element.GetRuntimeId() ?? [];
    private static string Limit(string? value) => value is null ? "" : value.Length <= 1024 ? value : value[..1024];
    private static string LimitValue(string? value) => value is null ? "" : value.Length <= 4096 ? value : value[..4096];
    private static object WindowInfo(Target current) => new { hwnd = current.Hwnd, pid = current.Pid, title = current.Title, process = current.ProcessName };
    private void RequireEpoch(long expected, Target current, bool allowPaused)
    {
        if (Volatile.Read(ref epoch) != expected || !ReferenceEquals(Volatile.Read(ref target), current))
            throw Error("revoked", "Control or observation was revoked by pause, stop, or a target change.");
        if (!allowPaused && Paused) throw Error("paused", "Control is paused.");
        if (current.Lease is null || !current.Lease.IsScopeCurrent()) throw Error("lease_lost", "Window lease is no longer held.");
    }
    public void ValidateActiveSession()
    {
        var expected = Volatile.Read(ref epoch);
        var current = ValidateTarget();
        RequireEpoch(expected, current, allowPaused: false);
    }
    public Action CaptureActiveSessionGuard()
    {
        // Resolve UIA/provider identity before a caller acquires a short-lived external lock.
        var expected = Volatile.Read(ref epoch);
        var current = ValidateTarget();
        RequireEpoch(expected, current, allowPaused: false);
        return () =>
        {
            // Atomic session checks and local Win32 owner-chain/lease checks only; no UIA or provider IPC.
            CheckDisposed();
            RequireEpoch(expected, current, allowPaused: false);
        };
    }
    private void InvalidateObservation() { observationId = null; elements.Clear(); Interlocked.Increment(ref epoch); }
    private void CheckDisposed() { if (Volatile.Read(ref disposed) != 0) throw Error("disposed", "Controller is disposed."); }
    private void CheckFocus(InputState before, InputState after, Target current)
    {
        bool wasTarget = IsInScope(before.foreground, current) || IsFocusInScope(before.focus, current);
        bool nowTarget = IsInScope(after.foreground, current) || IsFocusInScope(after.focus, current);
        if (!wasTarget && nowTarget) Pause();
    }
    private static bool IsFocusInScope(long hwnd, Target current) => hwnd != 0 && IsInScope(Native.GetAncestor(new nint(hwnd), 2).ToInt64(), current);
    private static InvalidOperationException Error(string code, string message) => new(code + ": " + message);
    private static InvalidOperationException Unsupported(string action, string[] available) =>
        Error("unsupported", $"'{action}' has no permitted semantic provider on this element. Available patterns/actions: {string.Join(", ", available)}. No physical input fallback was attempted.");
    public void Dispose() { if (Interlocked.Exchange(ref disposed, 1) == 0) Stop(); }

    private sealed record Target(long Hwnd, int Pid, long StartTicks, uint ThreadId, string ProcessName, string Title, string ClassName, int[] RuntimeId, WindowLease? Lease = null);
    private sealed record ElementBinding(IUIAutomationElement Element, Target Root, int[] RuntimeId, string Name, string AutomationId,
        int Type, int Left, int Top, int Right, int Bottom, string SemanticState, nint NativeHwnd);
    private sealed record InputState(long foreground, long focus, bool focusQuerySucceeded, int cursorX, int cursorY, bool cursorQuerySucceeded);
    private static class Native
    {
        internal delegate bool EnumWindowCallback(nint hwnd, nint lParam);
        [StructLayout(LayoutKind.Sequential)] private struct Point { public int X, Y; }
        [StructLayout(LayoutKind.Sequential)] private struct Rect { public int Left, Top, Right, Bottom; }
        [StructLayout(LayoutKind.Sequential)] private struct GuiInfo
        { public int Size, Flags; public nint Active, Focus, Capture, MenuOwner, MoveSize, Caret; public Rect CaretRectangle; }
        [DllImport("user32.dll")] internal static extern bool EnumWindows(EnumWindowCallback callback, nint lParam);
        [DllImport("user32.dll")] internal static extern bool IsWindow(nint hwnd);
        [DllImport("user32.dll")] internal static extern bool IsWindowVisible(nint hwnd);
        [DllImport("user32.dll")] internal static extern bool IsIconic(nint hwnd);
        [DllImport("user32.dll")] internal static extern uint GetWindowThreadProcessId(nint hwnd, out uint pid);
        [DllImport("user32.dll")] internal static extern nint GetWindow(nint hwnd, uint command);
        [DllImport("user32.dll")] internal static extern nint GetAncestor(nint hwnd, uint flags);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] internal static extern int GetWindowTextLength(nint hwnd);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] internal static extern int GetWindowText(nint hwnd, StringBuilder text, int maxCount);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] internal static extern int GetClassName(nint hwnd, StringBuilder text, int maxCount);
        [DllImport("user32.dll")] private static extern nint GetForegroundWindow();
        [DllImport("user32.dll")] private static extern bool GetGUIThreadInfo(uint threadId, ref GuiInfo info);
        [DllImport("user32.dll")] private static extern bool GetCursorPos(out Point point);
        internal static InputState Snapshot()
        {
            var info = new GuiInfo { Size = Marshal.SizeOf<GuiInfo>() };
            bool focusOk = GetGUIThreadInfo(0, ref info), cursorOk = GetCursorPos(out var cursor);
            return new(GetForegroundWindow().ToInt64(), info.Focus.ToInt64(), focusOk, cursor.X, cursor.Y, cursorOk);
        }
    }
}
