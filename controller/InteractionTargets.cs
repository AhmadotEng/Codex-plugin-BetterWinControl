using System.Diagnostics;

namespace BackgroundControl;

public sealed partial class WindowController
{
    /// <summary>Read-only native relationship discovery. Selecting another window remains explicit.</summary>
    public object InteractionTargets()
    {
        const int resultLimit = 64, windowLimit = 512, ownerDepthLimit = 16, timeLimitMs = 250;
        var watch = Stopwatch.StartNew();
        var expectedEpoch = Volatile.Read(ref epoch);
        var current = ValidateTarget();
        RequireEpoch(expectedEpoch, current, allowPaused: true);
        var selectedIdentity = ReadIdentity(current.Hwnd);
        var identities = new Dictionary<long, Target> { [current.Hwnd] = selectedIdentity };
        var verified = new List<object>();
        var unverified = new List<object>();
        var included = new HashSet<long>();
        var limitsHit = new HashSet<string>(StringComparer.Ordinal);
        int scanned = 0, ownerLinks = 0, unavailable = 0;

        bool InTime()
        {
            if (watch.ElapsedMilliseconds < timeLimitMs) return true;
            limitsHit.Add("time_limit");
            return false;
        }
        Target Identity(long hwnd)
        {
            if (!identities.TryGetValue(hwnd, out var value))
                identities[hwnd] = value = ReadIdentity(hwnd);
            return value;
        }
        bool SameProcess(Target identity) => identity.Pid == current.Pid && identity.StartTicks == current.StartTicks;
        bool StillExact(Target identity)
        {
            try
            {
                var now = ReadIdentity(identity.Hwnd);
                return now.Pid == identity.Pid && now.StartTicks == identity.StartTicks &&
                       now.ThreadId == identity.ThreadId && now.ClassName == identity.ClassName;
            }
            catch (Exception ex) when (ex is InvalidOperationException or ArgumentException or System.ComponentModel.Win32Exception)
            { return false; }
        }
        bool ExactOwnerChain(IReadOnlyList<Target> chain)
        {
            for (int i = 0; i < chain.Count; i++)
            {
                if (!StillExact(chain[i])) return false;
                if (i + 1 < chain.Count && Native.GetWindow(new nint(chain[i].Hwnd), 4).ToInt64() != chain[i + 1].Hwnd)
                    return false;
            }
            return true;
        }
        void Add(Target identity, string relation, bool ownershipVerified, IReadOnlyList<Target> chain, string evidenceKind)
        {
            if (!included.Add(identity.Hwnd)) return;
            bool exact = ExactOwnerChain(chain);
            if (!exact) { ownershipVerified = false; unavailable++; }
            bool visible = Native.IsWindowVisible(new nint(identity.Hwnd));
            bool minimized = Native.IsIconic(new nint(identity.Hwnd));
            bool topLevel = Native.GetAncestor(new nint(identity.Hwnd), 2).ToInt64() == identity.Hwnd;
            bool menuSurface = identity.ClassName == "#32768";
            string? reason = !exact ? "identity_or_owner_chain_changed" : Denial(identity);
            reason ??= !ownershipVerified ? "ownership_unverified" : !topLevel ? "not_top_level" :
                       !visible ? "hidden" : minimized ? "minimized" : menuSurface ? "native_menu_loop_unsupported" : null;
            var row = new
            {
                window = WindowInfo(identity), relation, ownershipVerified,
                ownerChain = chain.Select(WindowInfo).ToArray(),
                evidence = new { kind = evidenceKind, exactIdentityAndOwnerLinks = exact,
                    sameProcessForEntireChain = chain.All(SameProcess), inferredFromTitle = false },
                visible, minimized, sameThreadAsSelected = identity.ThreadId == current.ThreadId,
                eligibility = new { selectable = reason is null, reason, requiresExplicitSelection = true,
                    nativeMenuLoopSupported = false,
                    note = "Eligibility is for an explicit new attachment, not input through the current window session." }
            };
            (ownershipVerified ? verified : unverified).Add(row);
        }

        Add(selectedIdentity, "selected", true, [selectedIdentity], "validated_selected_native_identity");

        // Owners permit an explicit return from a selected popup/dialog to its parent.
        var ancestors = new List<Target> { selectedIdentity };
        var ancestorHandles = new HashSet<long> { current.Hwnd };
        long nextOwner = Native.GetWindow(new nint(current.Hwnd), 4).ToInt64();
        for (int depth = 0; nextOwner != 0 && depth < ownerDepthLimit && InTime(); depth++)
        {
            ownerLinks++;
            if (!ancestorHandles.Add(nextOwner)) { limitsHit.Add("owner_cycle"); break; }
            try
            {
                var ancestor = Identity(nextOwner);
                ancestors.Add(ancestor);
                bool sameProcess = ancestors.All(SameProcess);
                Add(ancestor, sameProcess ? "owner_ancestor" : "unverified_cross_process_owner_ancestor",
                    sameProcess, ancestors.ToArray(), "native_GW_OWNER_chain_from_selected");
                nextOwner = Native.GetWindow(new nint(nextOwner), 4).ToInt64();
            }
            catch (Exception ex) when (ex is InvalidOperationException or ArgumentException or System.ComponentModel.Win32Exception)
            { unavailable++; break; }
        }
        if (nextOwner != 0 && ancestors.Count > ownerDepthLimit) limitsHit.Add("owner_depth_limit");

        // Do not stop at 64 same-process siblings. Scan within the independent budget,
        // then prioritize proven selected/owner/descendant relationships in the response.
        bool enumerationFinished = Native.EnumWindows((hwnd, _) =>
        {
            if (scanned >= windowLimit) { limitsHit.Add("window_limit"); return false; }
            if (!InTime()) return false;
            scanned++;
            long handle = hwnd.ToInt64();
            if (included.Contains(handle)) return true;
            try
            {
                Native.GetWindowThreadProcessId(hwnd, out var candidatePid);
                bool samePid = candidatePid == current.Pid;
                var chainHandles = new List<long> { handle };
                var visited = new HashSet<long> { handle };
                long owner = Native.GetWindow(hwnd, 4).ToInt64();
                bool reachesSelected = false;
                for (int depth = 0; owner != 0 && depth < ownerDepthLimit; depth++)
                {
                    if (!InTime()) return false;
                    ownerLinks++;
                    if (!visited.Add(owner)) { limitsHit.Add("owner_cycle"); break; }
                    chainHandles.Add(owner);
                    if (owner == current.Hwnd) { reachesSelected = true; break; }
                    owner = Native.GetWindow(new nint(owner), 4).ToInt64();
                }
                if (!reachesSelected && owner != 0 && chainHandles.Count > ownerDepthLimit)
                    limitsHit.Add("owner_depth_limit");

                // A visible native menu without an owner is a candidate, never a verified target.
                bool ownerless = chainHandles.Count == 1 && Native.GetWindow(hwnd, 4) == 0;
                bool menuCandidate = false;
                if (ownerless && Native.IsWindowVisible(hwnd))
                {
                    var className = new System.Text.StringBuilder(256);
                    Native.GetClassName(hwnd, className, className.Capacity);
                    menuCandidate = className.ToString() == "#32768";
                }
                if (!reachesSelected && !samePid && !menuCandidate) return true;
                var identity = Identity(handle);
                if (reachesSelected)
                {
                    var chain = chainHandles.Select(Identity).ToArray();
                    bool sameProcess = chain.All(SameProcess);
                    Add(identity, sameProcess ? "owned_descendant" : "unverified_cross_process_owned_descendant",
                        sameProcess, chain, "native_GW_OWNER_chain_to_selected");
                }
                else if (menuCandidate)
                    Add(identity, "unverified_ownerless_menu", false, [identity], "ownerless_native_menu_surface");
                else
                    Add(identity, "unverified_same_process_only", false, [identity], "same_PID_without_selected_owner_chain");
            }
            catch (Exception ex) when (ex is InvalidOperationException or ArgumentException or System.ComponentModel.Win32Exception)
            { unavailable++; }
            return true;
        }, nint.Zero);
        if (!enumerationFinished && limitsHit.Count == 0) limitsHit.Add("enumeration_incomplete");

        RequireEpoch(expectedEpoch, ValidateTarget(), allowPaused: true);
        int candidateCount = verified.Count + unverified.Count;
        if (candidateCount > resultLimit) limitsHit.Add("result_limit");
        if (unavailable > 0) limitsHit.Add("identity_unavailable_or_changed");
        bool complete = limitsHit.Count == 0;
        return new
        {
            selected = WindowInfo(selectedIdentity), sessionEpoch = expectedEpoch, paused = Paused,
            targets = verified.Concat(unverified).Take(resultLimit).ToArray(), complete,
            scannedWindows = scanned, examinedOwnerLinks = ownerLinks, elapsedMs = watch.ElapsedMilliseconds,
            candidateCount, omittedResults = Math.Max(0, candidateCount - resultLimit), unavailableWindows = unavailable,
            limits = new { results = resultLimit, windows = windowLimit, ownerDepth = ownerDepthLimit, timeMs = timeLimitMs },
            limitsHit = limitsHit.ToArray(), continuation = (string?)null,
            next = complete ? "Explicitly select an eligible exact window identity to switch targets; no window was selected automatically." :
                "Discovery is incomplete. Explicitly select a known narrower window and query again, or retry after transient window changes. No continuation token is available.",
            limitation = "Native ownership metadata only. Same PID/title do not prove ownership. Cross-process candidates and ownerless menus remain unverified. Native nested menu-loop input is not supported. Time is checked between bounded native metadata operations."
        };
    }
}
