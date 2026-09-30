# Persistent Zen installation and real test

The companion package is **unsigned**. The native host is prepared and registered for the current Windows user. The user's existing Zen profile already allows unsigned extensions, and the user authorized using that configuration for the live test. No Zen profile or signature preference is changed by this setup.

## Current route: existing unsigned-extension configuration

Use **Add-ons Manager → gear → Install Add-on From File** in the existing official Zen profile and select `<repository-root>\extensions\zen\dist\betterwincontrol-zen-0.1.0-unsigned.xpi`. Accept its disclosed permissions. The existing profile is `<existing-zen-profile>`; do not create a replacement profile or use a temporary debugging install.

The readiness helper's explicit `--allow-existing-unsigned` option accepts the companion only after Zen records the fixed extension ID as active and enabled in that profile, and its installed payload matches the tested package. It does not modify preferences, install anything, or infer success merely from a signing preference. Browser acceptance and an actual live pairing remain necessary. The core bridge does not require a Mozilla signature.

## Optional Mozilla signing route

1. Sign in or create a Mozilla account at the [Add-ons Developer Hub](https://addons.mozilla.org/developers/). Choose **Submit a New Add-on → On your own** for private/self distribution.
2. Upload `<repository-root>\extensions\zen\dist\betterwincontrol-zen-0.1.0-unsigned.xpi`. Follow Mozilla's validation and signing steps. The package contains readable JavaScript, HTML, CSS and its manifest; there is no minification or browser rebuild. Signing may require review; it has not happened yet.
3. Download Mozilla's **signed XPI** from the submitted version, then in the existing Zen use **Add-ons Manager → gear → Install Add-on From File**. Select that signed file and accept its disclosed permissions. Do not use temporary debugging installation or change signature settings.

Mozilla's [self-distribution instructions](https://extensionworkshop.com/documentation/publish/submitting-an-add-on/#self-distribution) describe the account, upload and signed download flow. No public add-on listing is required. Do not send account passwords or API secrets into the task.

Artifact: 10,806 bytes; SHA-256 `d5f2a94373a4acd1647591b62fc6007103977dea90674527cc4eb0bff0a252da`.
Fixed add-on ID: `zen-companion@betterwincontrol.local`.
Mozilla-signed copies will have a different archive hash; the readiness helper compares the non-signature file payload against the tested package. Its default policy requires Zen's signed/active installation metadata. The approved unsigned route uses `--allow-existing-unsigned` and retains all active-state, fixed-ID, selected-profile and payload checks.

## Local setup completed

- Configuration: `<repository-root>\runtime\zen-bridge\config.json`. Current-user DPAPI and protected user-only ACL; do not upload this file.
- Native manifest: `<repository-root>\runtime\zen-bridge\native-host\local.betterwincontrol.zen.json`.
- Launcher beside manifest: `betterwincontrol-zen-native.bat`, fixed to Python3.11 and this repository's `native_host.py`.
- Registry: `HKEY_CURRENT_USER\Software\Mozilla\NativeMessagingHosts\local.betterwincontrol.zen` and the explicit `HKEY_LOCAL_MACHINE` 64-bit fallback at the same key, both with the manifest path above as their default value. Read-back verified. The machine entry was added after a live registry trace showed the browser could not find the user entry through its cached HKCU handle; it is an existing supported lookup location in the shipped browser.
- Existing profile metadata inspected: `<existing-zen-profile>`. No cookies, passwords or authentication tokens were read.

The registry/helper setup does not add a startup process. Zen starts the native host only when the companion connects. Host readiness is separate from signing, extension installation and HWND binding.

Readiness command (read-only without `--register`):

```powershell
python extensions/zen/scripts/register_native_host.py --profile '<existing-zen-profile>' --allow-existing-unsigned --scope machine
```

Omit `--allow-existing-unsigned` to require the signed installation policy. `--scope user` (the default) checks HKCU64, and `--scope machine` checks HKLM64. Adding `--register` writes only that selected host registration and verifies readback; machine scope requires an elevated setup process. Existing conflicting values are preserved and reported as an error. A `ready: true` result verifies installation and host setup; it does not prove window pairing, browser actions or restart persistence.

## Real-browser test after installation

1. Root's development controller attaches the existing selected Zen HWND and captures PID, creation time and thread. No replacement profile or browser restart is needed solely for native-host registration.
2. Call `browser.connect`; it returns immediately and listens for 30 seconds. Click the companion toolbar button in that same Zen window. Grant the site permission if prompted, then call `browser.pair` promptly. The nonce lasts 30 seconds. If granting permission takes longer than the listener, reconnect and click again. Binding must corroborate the native host's process ancestry, selected HWND and browser window geometry; a title match is insufficient.
3. `observe({includePage:false})` lists tabs in the paired window without page-content permission. Use the observed Stremio ID with `input({action:'select_tab',tabId,observationId})`, then `verify({actionId})`. Selecting a tab must leave the user's other application focused.
4. YouTube media operations require the user to grant that origin through the toolbar. Observe the actual YouTube tab with `includePage:true`; use its returned media element ID for `media_pause`, then verify the media state separately. A fresh observation is required for each action and expires after 15 seconds.
5. Repeat tab selection/pause with foreground/focus/pointer metadata. Restore only the states actually changed when requested. Report each observed effect and any binding/provider failure. Restart persistence remains a separate follow-up.

The companion provides semantic browser/page operations. It does not claim trusted pointer/key events, hover, drag, browser chrome or arbitrary Windows-app coverage. Real Stremio and YouTube control requires the live test; signing is not a prerequisite under the user's approved existing configuration. See [the live status](../../docs/ZEN-LIVE-STATUS.md) for recorded results.
