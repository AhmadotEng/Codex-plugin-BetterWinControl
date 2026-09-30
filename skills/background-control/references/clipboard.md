# Shared clipboard

Use these tools only for a user-authorized clipboard task. This is the shared
system clipboard, not private state for the selected application.

- clipboard_state reports sequence and text availability without reading content.
- clipboard_read reads text.
- clipboard_write requires the current sequence token and replaces existing formats.
- A sequence conflict means someone changed the clipboard. Report it; do not get
  a new token just to overwrite the newer content.
- A canceled or timed-out write may have changed or cleared the clipboard. Report
  the uncertain outcome; do not assume rollback or automatically retry.
- Do not secretly save/restore clipboard content as a typing mechanism.
