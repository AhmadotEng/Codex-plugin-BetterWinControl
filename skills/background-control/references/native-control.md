# Native input and accessibility

Read capabilities for the selected target before choosing a backend. Available
operations do not prove compatibility with every application. The compatibility
matrix records fixture, framework and real-app evidence separately.

## Accessibility

- Use observe for current screenshots/elements. Search, subtree_id and continuation
  find controls beyond the first page. An incomplete search does not prove absence.
- Use IDs from the latest observation for act: invoke, set_value, toggle, select,
  expand, collapse or scroll when exposed. Re-observe to verify the result.
- insert_text supports validated native Edit/RichEdit controls at their existing
  selection. It is not generic keyboard typing or browser injection.
- Missing UIA elements do not rule out a supported native-input backend.

## Native input

- Pass the latest inputFrame.frameId and ordered steps to input. Coordinates are
  physical pixels in the reported captured client bounds.
- Resize, target replacement, pause, expiry or a completed batch requires a fresh
  observation. Do not reuse stale frames or re-label old images as fresh.
- shortcut accepts numeric keys or one keysym chord such as Control_L+Shift_L+p.
  Unsupported layout/keypad distinctions fail explicitly.
- Observe after a failed or partial batch before deciding whether to retry.
- Respect Pause/Stop and inspect teardown evidence. Broker exit alone does not
  prove hook removal. Never substitute shared foreground input.
- Paused, closed, minimized or unavailable targets are not live. Do not restore a
  minimized window without authorization.

Ordinary Settings and terminal windows are eligible targets, but framework input
compatibility remains subject to testing. Do not automate Codex, credential or
security-consent interfaces or protected applications. The PiP belongs to this
plugin, not Codex's built-in Computer Use.

