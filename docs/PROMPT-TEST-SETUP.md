# Prompt-test installation

The user authorized making the tested BetterWinControl build available to a new
Codex task. This is a local test deployment, not a portable release or proof that
the full native-input acceptance gate has passed.

## Exact wiring

- Plugin ID: `windows-background-control@personal`.
- Display name: `Codex plugin - BetterWinControl`.
- Installed source: `<installed-plugin-root>`.
- Version: `0.1.0+codex.20260930041209`.
- `.mcp.json` command: `<python-3.11-executable>`.
- Arguments: `-u`, `<repository-root>\scripts\mcp_server.py`.

The absolute server path preserves the exact development runtime and native-host
configuration that passed three real Stremio/YouTube tests. Do not move or delete
that repository while this local test installation is in use. No registry path,
bridge secret, XPI or browser preference needs to move when the plugin cache updates.

Updated files in the installed source are `.mcp.json`, `.codex-plugin/plugin.json`,
`skills/background-control/SKILL.md`, and the relevant documentation. Its previous
controller sources and runtime remain intact. Original manifest, MCP configuration
and skill, with SHA256 records, are preserved at:

`<repository-root>\.build\prompt-test-rollback\20260930-011914`

## User test

Companion v0.2.0 is installed and active in the existing profile. Read-only
installation validation confirms that its payload matches the tested XPI and the
existing machine-scope native-host registration is ready. The package is
`extensions/zen/dist/betterwincontrol-zen-0.2.0-unsigned.xpi`, SHA256
`9461923ffd894895d38704f9213b3da8adf1f6edfd9e728ed8a1b264940fca20`.
The v0.1.0 artifact is preserved. This update does not change browser preferences,
profiles, native-host registration or existing site permissions.

Start a new Codex task and confirm its actual available tools include `browser`
before attaching the window. The current actual Codex tool connection passed three
times without restarting Zen. If a task still exposes only the obsolete 11-tool
backend, fully quit and reopen Codex once; that earlier cache issue is documented
below. Keep Zen open; routine control does not require extension reinstallation.
Keep the existing Zen window open with its YouTube video and Stremio tabs.

> Use BetterWinControl to control my existing Zen window in the background. Show its live PiP, select my existing Stremio tab, then switch to my YouTube video and pause it. Verify each result and leave my mouse and keyboard alone.

With companion v0.2, no toolbar click is required to connect. The extension keeps
one native host ready while Zen is open; the controller connects only for an explicit
task and verifies the selected window. Each new/stopped session receives a fresh
binding automatically. Stop revokes the current session while the companion remains
idle and ready. Disabling readiness in the extension persists until re-enabled.
The native host configuration is unchanged; no signing or routine `prepare` is
necessary. Existing site permissions remain separate from the connection: selecting
an existing tab needs no new site grant, while page actions on a new origin may do so.

The test covers the live-verified browser semantic subset. General mouse/keyboard,
hover/drag, browser chrome, restart persistence and system-wide coverage retain the
limits in `COMPATIBILITY.md`. Pause/Stop remain available in PiP.

The v0.2 actual Codex readiness check is
`tests/results/zen-always-ready-codex.json`: automatic binding passed in 207, 192
and 42 ms. The third connection followed over 70 seconds idle after Stop, with the
same native host still alive. Read-only tab observation passed. Zen remained
foreground after setup, so this particular run did not attempt background tab
selection; that pending action check is distinct from the passed connection gate.

## Discovery failure and restart check (2026-09-29)

The user's task **Open Streamio in Zen** (`<local-task-id>`)
received the new skill but only the old 11-tool controller. The running desktop
app-server continued spawning `windows-background-control/scripts/mcp_server.py`
even though both installed configuration files pointed to the development repository.
No companion command or tab-selection attempt was made. User typing was not the cause.

A separate, freshly started Codex app-server, using the existing configuration with
no MCP override, successfully discovered all 15 tools including `browser`, `input`,
`interaction_targets` and `capabilities`. See `tests/results/codex-appserver-discovery.json`.
This read-only probe created no task and performed no browser actions. It verifies
fresh-backend discovery, not the still-running desktop app or a user prompt after
restart. Desktop prompt acceptance remains pending until that actual test succeeds.

The earlier `prompt-deployment.json` passing result only verified a manually launched
MCP server. It must not be treated as evidence that a Codex desktop task received the
updated tools. Future readiness checks must include actual Codex tool discovery.

The retry at 03:06 UTC on September 30 obtained the updated tools but checked
pairing after only about two seconds, restarted the listener, and checked again
after about two seconds. Both errors had a null `connectionError`. That evidence
only establishes that the host had not connected yet. The task then abandoned
pairing and attempted native shortcuts, which were rejected because Zen was
foreground. It did not establish failed native-host registration or a failed tab
selection. An initial tested repair extended the manual pairing wait, but was not
deployed after the user rejected that workflow. The user instead authorized the
always-ready v0.2 companion: automatic authenticated connection and verified window
binding on request, without a per-session toolbar gesture. Process ownership and
fresh session/nonce checks remain. Ambiguous native/browser window mappings fail.

## Recovery

### Obsolete running backend after the v0.2 update

At 04:06 UTC on September 30, the existing **Open Streamio in Zen** task returned
the old `connect` instruction to click the toolbar and then
`pairing_ambiguous_stale_or_window_mismatch_click_again`. That response came from
the already-running old Python backend; it is not a v0.2 requirement. The current
source and fresh actual Codex tool tests use automatic binding. Old setup documents
were also still present in the installed package and have now been synchronized.

Fully quit and reopen Codex to replace old running MCP processes, then retry the
authorized task with fresh IDs. No Zen restart, toolbar pairing click, browser
preference change or extension reinstall is required for that backend reload.

### Original deployment rollback

Stop the testing controller. Verify the saved hashes in `rollback.json`, restore only
the three saved files to their listed source paths, run the plugin-creator cachebuster
helper for `<installed-plugin-root>`, and reinstall
`windows-background-control@personal` through `codex plugin add`. Fully restart Codex,
then start a new task and verify the available tools.
This restores the original runtime selection without deleting the development repo,
companion, saved extensions, existing profile or native-host configuration.

