# Interaction-target discovery fixture

The test links the actual controller project and uses two disposable Win32 fixture processes. Windows carry `WS_EX_NOACTIVATE`; the test sends no physical input and does not activate windows. Identical fixture titles deliberately cannot distinguish ownership.

The seven checks cover selected/native owner-chain identities, nested descendants, return paths from a selected popup to its owners, rejection of same-PID-only siblings and cross-process ownership links, hidden/minimized eligibility, no implicit target switch, and explicit bounded/incomplete reporting when 80 hidden sibling windows compete with verified descendants. A stopped session must be rejected.

Build from the repository root:

```powershell
dotnet build tests/interaction-targets/InteractionTargetsTests.csproj -c Release --nologo
```

Coordinate the visible fixture slot before running:

```powershell
& tests/interaction-targets/bin/Release/net10.0-windows10.0.22000.0/InteractionTargetsTests.exe tests/interaction-targets/results.json
```

The fixture processes have 15-second watchdogs and close on stdin shutdown/EOF. The driver closes only its owned processes. It records a before/after foreground comparison, not continuous input isolation proof.

This exercises native relationship discovery, not menu-loop input. It deliberately does not open a real native context-menu loop. Production reports ownerless native menu surfaces as unverified and does not infer owner relationships from a shared PID or title. Cross-process native ownership links remain unsupported candidates. Switching requires a separate explicit attachment and revalidation.

Status: all seven checks passed in 345 ms. Initial discovery examined 192 windows in 5 ms and completed. The sibling-flood check examined 272 windows in 8 ms and returned 64 of 89 candidates with `complete:false`, `result_limit`, and 25 explicit omissions; verified descendants remained in the response. Foreground stayed unchanged and both owned fixture processes exited. Details are in the ignored `results.json`.
