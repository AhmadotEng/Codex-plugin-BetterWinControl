"""Readiness/registry boundary fixtures; no real registry, profile or browser writes."""
import importlib.util
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("registration", ROOT/"scripts"/"register_native_host.py")
registration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registration)


class RegistryFixture:
    HKEY_CURRENT_USER, KEY_READ, KEY_WRITE, KEY_WOW64_64KEY, REG_SZ = 1, 2, 4, 8, 1
    HKEY_LOCAL_MACHINE = 3
    def __init__(self, value=None):
        self.value, self.writes, self.hives = value, [], []
    def OpenKey(self, *args):
        self.hives.append(args[0])
        if self.value is None:
            raise FileNotFoundError()
        return 1
    def QueryValueEx(self, *args):
        return self.value, self.REG_SZ
    def CreateKeyEx(self, *args):
        self.hives.append(args[0])
        return 1
    def SetValueEx(self, key, name, reserved, kind, value):
        self.value = value
        self.writes.append(value)
    def CloseKey(self, key):
        pass


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.profile = Path(self.temporary.name)/"existing-profile"
        (self.profile/"extensions").mkdir(parents=True)
        self.expected = registration.current_xpi()
        self.installed = self.profile/"extensions"/(registration.EXTENSION_ID+".xpi")
        self.installed.write_bytes(self.expected.read_bytes())
        self.addon = {"id": registration.EXTENSION_ID, "version": json.loads((ROOT/"extension"/"manifest.json").read_text())["version"], "active": True,
                      "signedState": 2, "path": str(self.installed)}
        self.write_metadata()
    def tearDown(self):
        self.temporary.cleanup()
    def write_metadata(self):
        (self.profile/"extensions.json").write_text(json.dumps({"addons": [self.addon]}), encoding="utf-8")
    def test_browser_signature_record_and_payload_are_both_required(self):
        self.assertTrue(registration.inspect_install(self.profile, self.expected)["ready"])
        self.addon["signedState"] = 0
        self.write_metadata()
        self.assertEqual(registration.inspect_install(self.profile, self.expected)["reason"], "mozilla_signed_addon_required")
    def test_explicit_unsigned_route_uses_existing_active_installation_without_writes(self):
        self.addon["signedState"] = 0
        self.write_metadata()
        (self.profile/"prefs.js").write_text('user_pref("xpinstall.signatures.required", false);\n', encoding="utf-8")
        before = {path.relative_to(self.profile): path.read_bytes() for path in self.profile.rglob("*") if path.is_file()}
        result = registration.inspect_install(self.profile, self.expected, allow_existing_unsigned=True)
        self.assertTrue(result["ready"])
        self.assertTrue(result["payloadMatches"])
        self.assertEqual(result["signedState"], 0)
        self.assertEqual(result["signaturePolicy"], "existing_browser_configuration")
        after = {path.relative_to(self.profile): path.read_bytes() for path in self.profile.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
    def test_unsigned_route_still_requires_active_and_enabled_addon(self):
        self.addon["signedState"] = 0
        for field, value in (("active", False), ("userDisabled", True), ("appDisabled", True)):
            with self.subTest(field=field):
                self.addon.update(active=True, userDisabled=False, appDisabled=False)
                self.addon[field] = value
                self.write_metadata()
                result = registration.inspect_install(self.profile, self.expected, allow_existing_unsigned=True)
                self.assertEqual(result["reason"], "installed_companion_not_active")
    def test_unsigned_route_still_requires_exact_id_profile_and_payload(self):
        self.addon["signedState"] = 0
        self.addon["id"] = "unrelated@example.invalid"
        self.write_metadata()
        self.assertEqual(registration.inspect_install(self.profile, self.expected, allow_existing_unsigned=True)["reason"],
                         "persistent_companion_not_installed")
        self.addon["id"] = registration.EXTENSION_ID
        self.addon["path"] = str(self.expected)
        self.write_metadata()
        self.assertEqual(registration.inspect_install(self.profile, self.expected, allow_existing_unsigned=True)["reason"],
                         "addon_not_in_selected_existing_profile")
        self.addon["path"] = str(self.installed)
        self.write_metadata()
        with zipfile.ZipFile(self.installed, "a") as archive:
            archive.writestr("unexpected.js", "// changed package")
        self.assertEqual(registration.inspect_install(self.profile, self.expected, allow_existing_unsigned=True)["reason"],
                         "installed_payload_differs_from_tested_package")
    def test_unsigned_route_rejects_wrong_id_inside_package(self):
        self.addon["signedState"] = 0
        self.write_metadata()
        with zipfile.ZipFile(self.installed, "w") as archive:
            archive.writestr("manifest.json", json.dumps({"browser_specific_settings": {"gecko": {"id": "other@example.invalid"}}}))
        with self.assertRaisesRegex(ValueError, "addon_id_mismatch"):
            registration.inspect_install(self.profile, self.expected, allow_existing_unsigned=True)
    def test_signing_metadata_does_not_change_compared_payload(self):
        with zipfile.ZipFile(self.installed, "a") as archive:
            archive.writestr("META-INF/test.signature", "fixture only, not a real Mozilla signature")
        self.assertTrue(registration.inspect_install(self.profile, self.expected)["payloadMatches"])
    def test_modified_payload_is_rejected(self):
        with zipfile.ZipFile(self.installed, "a") as archive:
            archive.writestr("unexpected.js", "// changed package")
        self.assertEqual(registration.inspect_install(self.profile, self.expected)["reason"], "installed_payload_differs_from_tested_package")
    def test_missing_and_inactive_addon_not_ready(self):
        self.addon["active"] = False
        self.write_metadata()
        self.assertFalse(registration.inspect_install(self.profile, self.expected)["ready"])
        self.addon["id"] = "unrelated@example.invalid"
        self.write_metadata()
        self.assertFalse(registration.inspect_install(self.profile, self.expected)["installed"])
    def test_register_prepared_host_keeps_unverified_extension_not_ready(self):
        registry = RegistryFixture()
        plan = {"ready": False, "hostReady": True, "manifest": str(self.profile/"host.json")}
        result = registration.register(plan, registry)
        self.assertTrue(result["registered"])
        self.assertFalse(result["ready"])
        self.assertEqual(len(registry.writes), 1)
        self.assertTrue(registration.register(plan, registry)["alreadyRegistered"])
        self.assertEqual(len(registry.writes), 1)
    def test_register_accepts_explicitly_verified_unsigned_installation(self):
        self.addon["signedState"] = 0
        self.write_metadata()
        installation = registration.inspect_install(self.profile, self.expected, allow_existing_unsigned=True)
        plan = {"ready": False, "hostReady": True, "manifest": str(self.profile/"host.json"),
                "installation": installation, "requires": ["native_host_registration_required"]}
        result = registration.register(plan, RegistryFixture())
        self.assertTrue(result["ready"])
        self.assertEqual(result["requires"], [])
    def test_cli_unsigned_flag_is_explicit_and_forwarded(self):
        for flags, allowed, scope in (([], False, "user"), (["--allow-existing-unsigned"], True, "user"),
                                      (["--allow-existing-unsigned", "--scope", "machine"], True, "machine")):
            with self.subTest(flags=flags), patch.object(registration.sys, "argv", ["register_native_host.py", "--profile", str(self.profile), *flags]), \
                    patch.object(registration, "registration_plan", return_value={"ready": True}) as plan, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(registration.main(), 0)
                plan.assert_called_once_with(self.profile, allow_existing_unsigned=allowed, scope=scope)
    def test_explicit_machine_scope_registers_only_machine_hive_and_checks_conflicts(self):
        plan = {"hostReady": True, "manifest": str(self.profile/"host.json"), "registryHive": "HKEY_LOCAL_MACHINE"}
        registry = RegistryFixture()
        self.assertTrue(registration.register(plan, registry)["registered"])
        self.assertEqual(set(registry.hives), {registry.HKEY_LOCAL_MACHINE})
        self.assertEqual(registry.value, plan["manifest"])
        conflicting = RegistryFixture(str(self.profile/"other.json"))
        with self.assertRaisesRegex(ValueError, "different_native_host_registration_exists"):
            registration.register(plan, conflicting)
        self.assertEqual(conflicting.writes, [])
    def test_registration_readback_failure_is_reported(self):
        registry = RegistryFixture()
        with patch.object(registry, "QueryValueEx", return_value=(str(self.profile/"wrong.json"), registry.REG_SZ)):
            with self.assertRaisesRegex(ValueError, "native_host_registration_readback_failed"):
                registration.register({"hostReady": True, "manifest": str(self.profile/"host.json")}, registry)
    def test_unsupported_registry_hive_is_rejected_before_write(self):
        registry = RegistryFixture()
        with self.assertRaisesRegex(ValueError, "invalid_registration_hive"):
            registration.register({"hostReady": True, "manifest": str(self.profile/"host.json"),
                                   "registryHive": "HKEY_CLASSES_ROOT"}, registry)
        self.assertEqual(registry.writes, [])
    def test_different_registration_is_preserved(self):
        registry = RegistryFixture(str(self.profile/"other.json"))
        with self.assertRaisesRegex(ValueError, "different_native_host_registration_exists"):
            registration.register({"hostReady": True, "manifest": str(self.profile/"host.json")}, registry)
        self.assertEqual(registry.writes, [])
    def test_unprepared_host_cannot_register(self):
        registry = RegistryFixture()
        with self.assertRaisesRegex(ValueError, "registration_not_ready"):
            registration.register({"hostReady": False, "requires": ["prepare_required"]}, registry)
        self.assertEqual(registry.writes, [])


if __name__ == "__main__":
    unittest.main()
