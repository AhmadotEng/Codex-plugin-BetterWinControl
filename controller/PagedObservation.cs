using System.Diagnostics;
using System.Runtime.InteropServices;
using Interop.UIAutomationClient;

namespace BackgroundControl;

public sealed partial class WindowController
{
    private Discovery? discovery;
    private sealed record ScanNode(IUIAutomationElement Element, Target Root, int Depth, string? Parent, bool Siblings);
    private sealed class Discovery
    {
        public string Token = Guid.NewGuid().ToString("N");
        public readonly Queue<ScanNode> Queue = new();
        public readonly HashSet<string> Visited = new();
        public readonly List<string> Warnings = new();
        public string? Search;
        public long Epoch;
        public bool Incomplete;
        public List<Target> Roots = new();
    }

    /// <summary>Bounded provider traversal; continuation preserves pending siblings instead of dropping them.</summary>
    public object ObservePage(int maxElements = 200, string? continuation = null, string? search = null,
        string? subtreeId = null, int maxNodes = 2000)
    {
        CheckDisposed();
        maxElements = Math.Clamp(maxElements, 1, 2000);
        maxNodes = Math.Clamp(maxNodes, 1, 10000);
        var current = ValidateTarget();
        var operationEpoch = SessionEpoch;
        var before = Native.Snapshot();
        Discovery scan;
        if (continuation is not null)
        {
            scan = discovery ?? throw Error("stale_continuation", "Start a new observation.");
            if (scan.Token != continuation || scan.Epoch != operationEpoch ||
                observationId is null || DateTimeOffset.UtcNow >= observationExpiry)
                throw Error("stale_continuation", "Selection or observation changed; start a new search.");
            if (search is not null || subtreeId is not null)
                throw Error("invalid_continuation", "Search/subtree are fixed by the first page.");
        }
        else
        {
            ElementBinding? subtree = null;
            if (subtreeId is not null)
            {
                if (observationEpoch != operationEpoch || DateTimeOffset.UtcNow >= observationExpiry ||
                    !elements.TryGetValue(subtreeId, out subtree))
                    throw Error("stale_subtree", "Choose a subtree from a current observation.");
                ValidateBinding(subtree, current);
            }
            var scopeRoots = ScopeWindows(current, out var scopeIncomplete);
            scan = new Discovery { Search = search, Epoch = operationEpoch, Roots = scopeRoots, Incomplete = scopeIncomplete };
            if (scopeIncomplete) scan.Warnings.Add("Owned-window scope was limited or changed; select a narrower interaction target to continue.");
            discovery = scan;
            elements.Clear();
            observationId = Guid.NewGuid().ToString("N");
            observationEpoch = operationEpoch;
            observationExpiry = DateTimeOffset.UtcNow + ObservationLifetime;
            if (subtree is not null)
                scan.Queue.Enqueue(new(subtree.Element.BuildUpdatedCache(cache), subtree.Root, 0, null, false));
            else
                foreach (var root in scan.Roots)
                    try
                    {
                        var element = automation.ElementFromHandleBuildCache(new nint(root.Hwnd), cache);
                        if (element is not null) scan.Queue.Enqueue(new(element, root, 0, null, false));
                        else { scan.Incomplete = true; scan.Warnings.Add("Accessibility root unavailable."); }
                    }
                    catch (COMException ex) { scan.Incomplete = true; scan.Warnings.Add("Root unavailable: " + ex.HResult); }
        }
        var result = new List<object>();
        var watch = Stopwatch.StartNew();
        int examined = 0;
        while (scan.Queue.Count > 0 && result.Count < maxElements && examined < maxNodes &&
               watch.ElapsedMilliseconds < 4000 && scan.Visited.Count < 20000)
        {
            RequireEpoch(operationEpoch, current, allowPaused: true);
            var node = scan.Queue.Dequeue();
            var element = node.Element;
            // Queue a sibling as its own work item, so budgets never silently drop later siblings.
            if (node.Siblings)
                try
                {
                    var next = walker.GetNextSiblingElementBuildCache(element, cache);
                    if (next is not null) scan.Queue.Enqueue(node with { Element = next });
                }
                catch (COMException) { scan.Incomplete = true; scan.Warnings.Add("Sibling unavailable."); }
            examined++;
            try
            {
                var runtime = RuntimeId(element);
                var key = node.Root.Hwnd + ":" + string.Join(",", runtime);
                if (runtime.Length == 0) { scan.Incomplete = true; continue; }
                if (!scan.Visited.Add(key))
                {
                    // A provider cycle must not leave an unbounded sibling queue.
                    scan.Incomplete = true;
                    scan.Warnings.Add("Provider repeated an element identity.");
                    scan.Queue.Clear();
                    break;
                }
                var password = element.CachedIsPassword != 0 || LegacyProtected(element);
                var name = Limit(element.CachedName);
                var sensitive = password || ProtectedText.IsMatch(name) || ProtectedControlName.IsMatch(name);
                var bounds = element.CachedBoundingRectangle;
                var typeId = element.CachedControlType;
                var actions = sensitive ? Array.Empty<string>() : CachedActions(element);
                var value = sensitive ? (Text: (string?)null, ReadOnly: (bool?)null) : ObserveValue(element, actions);
                var native = element.CachedNativeWindowHandle;
                if (!sensitive && NativeEditActions.Supports(native, new nint(node.Root.Hwnd), node.Root.Pid,
                    isPasswordOrProtected: false, isReadOnly: value.ReadOnly ?? false))
                    actions = actions.Append("insert_text").ToArray();
                var id = "e" + scan.Visited.Count;
                if (!sensitive)
                    elements[id] = new(element, node.Root, runtime, name, element.CachedAutomationId,
                        typeId, bounds.left, bounds.top, bounds.right, bounds.bottom, SemanticFingerprint(element, actions), native);
                if (string.IsNullOrEmpty(scan.Search) || (!password &&
                    (name.Contains(scan.Search, StringComparison.OrdinalIgnoreCase) ||
                     element.CachedAutomationId.Contains(scan.Search, StringComparison.OrdinalIgnoreCase))))
                    result.Add(new { id, parent = node.Parent, name = password ? "[protected]" : name,
                        automationId = password ? "" : Limit(element.CachedAutomationId),
                        controlType = element.CachedLocalizedControlType, controlTypeId = typeId,
                        bounds = new { left = bounds.left, top = bounds.top, right = bounds.right, bottom = bounds.bottom },
                        enabled = element.CachedIsEnabled != 0, offscreen = element.CachedIsOffscreen != 0,
                        isPassword = password, protectedElement = sensitive, patterns = actions,
                        rootHwnd = node.Root.Hwnd, value = value.Text, readOnly = value.ReadOnly });
                if (sensitive) continue;
                if (node.Depth >= MaxDepth) { scan.Incomplete = true; continue; }
                var child = walker.GetFirstChildElementBuildCache(element, cache);
                if (child is not null) scan.Queue.Enqueue(new(child, node.Root, node.Depth + 1, id, true));
            }
            catch (COMException ex) { scan.Incomplete = true; scan.Warnings.Add("Element unavailable: " + ex.HResult); }
        }
        ValidateTarget();
        RequireEpoch(operationEpoch, current, allowPaused: true);
        var after = Native.Snapshot();
        CheckFocus(before, after, current);
        bool limited = scan.Visited.Count >= 20000;
        bool pending = scan.Queue.Count > 0 && !limited;
        if (limited) scan.Warnings.Add("Session node limit reached; narrow the search to a subtree.");
        bool incomplete = pending || scan.Incomplete || limited;
        return new { observationId, window = WindowInfo(current), elements = result,
            truncated = incomplete, complete = !incomplete, continuation = pending ? scan.Token : null,
            examined, totalExamined = scan.Visited.Count, traversal = "bounded_subtree_with_sibling_continuation",
            warnings = scan.Warnings.Distinct().Take(10).ToArray(), expiresAt = observationExpiry,
            paused = Paused, focusBefore = before, focusAfter = after,
            scope = scan.Roots.Select(r => new { hwnd = r.Hwnd, pid = r.Pid }).ToArray() };
    }
}
