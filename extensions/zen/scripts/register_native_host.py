"""Check persistent addon readiness; optionally register only this native host.

Default is read-only. --register writes one selected NativeMessagingHosts value only
after the controller's protected native configuration and manifest are validated.
The default --scope user uses HKCU; --scope machine explicitly selects HKLM64.
Signed installation is the default. --allow-existing-unsigned accepts the exact
tested payload when it is already active in the user's existing browser profile.
This uses the browser's existing configuration without changing its preferences.
Never installs an add-on, changes a profile, changes signing, or starts a browser.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
EXTENSION_ID = "zen-companion@betterwincontrol.local"
HOST_NAME = "local.betterwincontrol.zen"
REGISTRY_KEY = "Software\\Mozilla\\NativeMessagingHosts\\" + HOST_NAME


def payload_hashes(path):
    with zipfile.ZipFile(path) as archive:
        result = {}
        for entry in archive.infolist():
            if entry.is_dir() or entry.filename.upper().startswith("META-INF/"):
                continue
            if entry.filename in result or entry.file_size > 1_000_000:
                raise ValueError("invalid_or_oversized_addon_payload")
            result[entry.filename] = hashlib.sha256(archive.read(entry)).hexdigest()
        if "manifest.json" not in result:
            raise ValueError("addon_manifest_missing")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("browser_specific_settings", {}).get("gecko", {}).get("id") != EXTENSION_ID:
            raise ValueError("addon_id_mismatch")
        return result


def inspect_install(profile, expected_xpi, *, allow_existing_unsigned=False):
    profile = Path(profile).resolve()
    result = {"profile": str(profile), "installed": False, "ready": False,
              "signaturePolicy": "existing_browser_configuration" if allow_existing_unsigned else "mozilla_signed"}
    database = profile / "extensions.json"
    if not database.is_file():
        return {**result, "reason": "existing_profile_addon_metadata_not_found"}
    entries = json.loads(database.read_text(encoding="utf-8")).get("addons", [])
    matches = [item for item in entries if item.get("id") == EXTENSION_ID]
    if len(matches) != 1:
        return {**result, "reason": "persistent_companion_not_installed"}
    addon = matches[0]
    result.update(installed=True, version=addon.get("version"), active=addon.get("active") is True,
                  signedState=addon.get("signedState"))
    # SIGNEDSTATE_SIGNED=2 is Mozilla-signed ordinary WebExtension status.
    # This is the browser's validation record, not a hand-written crypto check.
    # The explicit unsigned route trusts the browser's active installation record;
    # it never enables unsigned addons or relaxes identity/payload checks.
    if not allow_existing_unsigned and addon.get("signedState") != 2:
        return {**result, "reason": "mozilla_signed_addon_required"}
    if addon.get("active") is not True or addon.get("userDisabled") or addon.get("appDisabled"):
        return {**result, "reason": "installed_companion_not_active"}
    raw_path = addon.get("path")
    if not isinstance(raw_path, str):
        return {**result, "reason": "installed_addon_path_unavailable"}
    installed = Path(raw_path).resolve()
    if not installed.is_relative_to(profile / "extensions") or not installed.is_file():
        return {**result, "reason": "addon_not_in_selected_existing_profile"}
    if payload_hashes(installed) != payload_hashes(expected_xpi):
        return {**result, "reason": "installed_payload_differs_from_tested_package"}
    return {**result, "ready": True, "payloadMatches": True, "addonPath": str(installed)}


def current_xpi():
    version = json.loads((ROOT / "extension" / "manifest.json").read_text(encoding="utf-8"))["version"]
    return ROOT / "dist" / f"betterwincontrol-zen-{version}-unsigned.xpi"


def registration_plan(profile, expected_xpi=None, repo_root=REPO, *, allow_existing_unsigned=False, scope="user"):
    if scope not in {"user", "machine"}:
        raise ValueError("invalid_registration_scope")
    registry_hive = "HKEY_CURRENT_USER" if scope == "user" else "HKEY_LOCAL_MACHINE"
    repo = Path(repo_root).resolve()
    directory = repo / "runtime" / "zen-bridge"
    manifest_path = directory / "native-host" / (HOST_NAME + ".json")
    launcher = directory / "native-host" / "betterwincontrol-zen-native.bat"
    config = directory / "config.json"
    installation = inspect_install(profile, expected_xpi or current_xpi(), allow_existing_unsigned=allow_existing_unsigned)
    host_problems = []
    if not all(path.is_file() for path in (manifest_path, launcher, config)):
        host_problems.append("controller_browser_prepare_required")
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not (manifest.get("name") == HOST_NAME and manifest.get("type") == "stdio" and
                manifest.get("allowed_extensions") == [EXTENSION_ID] and
                Path(manifest.get("path", "")).resolve() == launcher):
            host_problems.append("staged_native_manifest_mismatch")
        # Verify prepared DPAPI configuration can be opened without returning any
        # secret. Importing bridge has no registration/profile side effects.
        sys.path.insert(0, str(ROOT / "native"))
        try:
            import bridge
            bridge.read_config(config)
        except Exception:
            host_problems.append("protected_native_configuration_invalid")
        finally:
            sys.path.pop(0)
    problems = host_problems + ([] if installation["ready"] else [installation["reason"]])
    registered = False
    try:
        import winreg
        with winreg.OpenKey(getattr(winreg, registry_hive), REGISTRY_KEY, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            current, kind = winreg.QueryValueEx(key, "")
        registered = kind == winreg.REG_SZ and Path(current).resolve() == manifest_path
    except (ImportError, FileNotFoundError):
        pass
    if not registered:
        problems.append("native_host_registration_required")
    return {"ready": not problems and registered, "hostReady": not host_problems, "registered": registered, "installation": installation,
            "requires": problems, "registryHive": registry_hive, "registryView": "64", "registryKey": REGISTRY_KEY,
            "manifest": str(manifest_path), "browserProfileModified": False}


def register(plan, _registry=None):
    if not plan.get("hostReady"):
        raise ValueError("registration_not_ready: " + ", ".join(plan.get("requires", [])))
    if _registry is None:
        import winreg as _registry
    reg = _registry
    hive_name = plan.get("registryHive", "HKEY_CURRENT_USER")
    if hive_name not in {"HKEY_CURRENT_USER", "HKEY_LOCAL_MACHINE"}:
        raise ValueError("invalid_registration_hive")
    hive = getattr(reg, hive_name)
    success = {**plan, "registered": True,
               "ready": plan.get("hostReady") is True and plan.get("installation", {}).get("ready") is True,
               "requires": [reason for reason in plan.get("requires", []) if reason != "native_host_registration_required"]}
    key = None
    try:
        try:
            key = reg.OpenKey(hive, REGISTRY_KEY, 0, reg.KEY_READ | reg.KEY_WOW64_64KEY)
            current, kind = reg.QueryValueEx(key, "")
            if kind != reg.REG_SZ or Path(current).resolve() != Path(plan["manifest"]).resolve():
                raise ValueError("different_native_host_registration_exists")
            return {**success, "alreadyRegistered": True}
        except FileNotFoundError:
            pass
        finally:
            if key is not None:
                reg.CloseKey(key)
                key = None
        key = reg.CreateKeyEx(hive, REGISTRY_KEY, 0, reg.KEY_READ | reg.KEY_WRITE | reg.KEY_WOW64_64KEY)
        reg.SetValueEx(key, "", 0, reg.REG_SZ, plan["manifest"])
        current, kind = reg.QueryValueEx(key, "")
        if kind != reg.REG_SZ or Path(current).resolve() != Path(plan["manifest"]).resolve():
            raise ValueError("native_host_registration_readback_failed")
        return {**success, "alreadyRegistered": False}
    finally:
        if key is not None:
            reg.CloseKey(key)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path, help="Existing official Zen profile shown by about:profiles")
    parser.add_argument("--register", action="store_true", help="Register the prepared native host in the selected scope after validation")
    parser.add_argument("--scope", choices=("user", "machine"), default="user",
                        help="Registry scope: user (HKCU64, default) or machine (HKLM64, requires an elevated setup process)")
    parser.add_argument("--allow-existing-unsigned", action="store_true",
                        help="Accept an already-active unsigned companion in this existing profile; never changes browser preferences")
    args = parser.parse_args()
    try:
        plan = registration_plan(args.profile, allow_existing_unsigned=args.allow_existing_unsigned, scope=args.scope)
        result = register(plan) if args.register else plan
        print(json.dumps(result, indent=2))
        return 0 if result["ready"] else 2
    except Exception as exc:
        print(json.dumps({"ready": False, "registered": False, "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
