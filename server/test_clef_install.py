"""The local profile must not overwrite or accept exposed credential files."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import io
import json
import urllib.request
import configparser
import argparse

spec = importlib.util.spec_from_file_location("clef_install", Path(__file__).with_name("install-clef.py"))
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class ClefCredentialFiles(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.key = Path(self.directory.name) / "private" / "clef.key"

    def test_create_and_reuse_private_key(self):
        installer.ensure_key(self.key)
        original = self.key.read_bytes()
        self.assertEqual(self.key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(original.strip()), 64)
        installer.ensure_key(self.key)
        self.assertEqual(self.key.read_bytes(), original)

    def test_reject_public_key_without_silently_changing_permissions(self):
        installer.ensure_key(self.key)
        self.key.chmod(0o644)
        with self.assertRaises(RuntimeError):
            installer.ensure_key(self.key)
        self.assertEqual(self.key.stat().st_mode & 0o777, 0o644)

    def test_reject_symlink_even_to_private_file(self):
        installer.ensure_key(self.key)
        alias = self.key.with_name("alias.key")
        alias.symlink_to(self.key)
        with self.assertRaises(RuntimeError):
            installer.ensure_key(alias)

    def test_private_write_never_overwrites_existing_file(self):
        installer.private_file(self.key, b"original")
        with self.assertRaises(FileExistsError):
            installer.private_file(self.key, b"replacement")
        self.assertEqual(self.key.read_bytes(), b"original")


class ClefInstallTransport(unittest.TestCase):
    def test_redirect_cannot_forward_authorization(self):
        request = urllib.request.Request("http://127.0.0.1:8082/v1/models",
                                         headers={"Authorization": "Bearer private-fixture"})
        self.assertIsNone(installer.NoRedirect().redirect_request(
            request, None, 302, "Found", {}, "https://example.invalid/"))
        handlers = installer.local_opener().handlers
        self.assertTrue(any(isinstance(handler, installer.NoRedirect) for handler in handlers))

    def test_verify_rejects_hosted_or_misconfigured_adapter(self):
        expected = {"ok": True, "decision_provider": "clef", "decision_model": "clef-flash",
                    "decision_configured": True, "baseline_model": "gpt-6.1-sol"}
        for overrides in [{"decision_provider": "jev"}, {"decision_configured": False},
                          {"decision_model": "other"}, {"baseline_model": "gpt-6-astra"}]:
            with self.subTest(overrides=overrides), patch.object(installer, "local_opener") as opener:
                opener.return_value.open.return_value = io.BytesIO(json.dumps(expected | overrides).encode())
                with self.assertRaises(RuntimeError):
                    installer.verify_clef_adapter()
        with patch.object(installer, "local_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(json.dumps(expected).encode())
            installer.verify_clef_adapter()


class ClefRouterPreset(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.preset = Path(self.directory.name) / "clef" / "models.ini"
        self.model = Path(self.directory.name) / "Clef-Flash-Q8_0.gguf"

    def read_preset(self):
        preset = configparser.ConfigParser(interpolation=None)
        preset.read_string("[metadata]\n" + self.preset.read_text())
        return preset

    def test_lazy_default_and_idempotent_private_write(self):
        self.assertTrue(installer.write_clef_preset(self.preset, self.model))
        preset = self.read_preset()
        self.assertEqual(preset["metadata"]["version"], "1")
        self.assertEqual(preset["*"]["sleep-idle-seconds"], "300")
        self.assertFalse(preset["clef-flash"].getboolean("load-on-startup"))
        self.assertEqual(preset["clef-flash"]["model"], str(self.model))
        self.assertEqual(self.preset.stat().st_mode & 0o777, 0o600)
        before = self.preset.stat()
        self.assertFalse(installer.write_clef_preset(self.preset, self.model))
        self.assertEqual(self.preset.stat().st_ino, before.st_ino)

    def test_refresh_preserves_added_models_and_removes_old_managed_idle_override(self):
        installer.private_file(self.preset, b"""; Existing llama.cpp presets
version = 1

[*]
sleep-idle-seconds = 300
flash-attn = on
[clef-flash]
model = /old.gguf
c = 4096
sleep-idle-seconds = 60
[later-model]
model = /later.gguf
ctx-size = 32768
load-on-startup = false
sleep-idle-seconds = 900
""")
        previous = dict(self.read_preset()["later-model"])
        self.assertTrue(installer.write_clef_preset(self.preset, self.model, 120))
        preset = self.read_preset()
        self.assertEqual(dict(preset["later-model"]), previous)
        self.assertEqual(preset["*"]["flash-attn"], "on")
        self.assertEqual(preset["*"]["sleep-idle-seconds"], "120")
        self.assertNotIn("sleep-idle-seconds", preset["clef-flash"])
        self.assertTrue(self.preset.read_text().startswith("; Existing llama.cpp presets\nversion = 1\n"))
        self.assertNotIn("c", preset["clef-flash"])
        self.assertEqual(preset["clef-flash"]["ctx-size"], "8192")

    def test_exposed_or_symlinked_presets_are_not_overwritten(self):
        installer.private_file(self.preset, b"[future]\nmodel = /future.gguf\n")
        original = self.preset.read_bytes()
        self.preset.chmod(0o644)
        with self.assertRaises(RuntimeError):
            installer.write_clef_preset(self.preset, self.model)
        self.assertEqual(self.preset.read_bytes(), original)
        self.preset.chmod(0o600)
        link = self.preset.with_name("alias.ini")
        link.symlink_to(self.preset)
        with self.assertRaises(RuntimeError):
            installer.write_clef_preset(link, self.model)
        self.assertEqual(self.preset.read_bytes(), original)

    def test_invalid_preset_does_not_replace_existing_configuration(self):
        installer.private_file(self.preset, b"[future\nmodel = /future.gguf\n")
        original = self.preset.read_bytes()
        with self.assertRaises(configparser.Error):
            installer.write_clef_preset(self.preset, self.model)
        self.assertEqual(self.preset.read_bytes(), original)

    def test_idle_timeout_rejects_disabled_or_invalid_values(self):
        for value in ("0", "-1", "1.5", "invalid"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                installer.positive_seconds(value)
        self.assertEqual(installer.positive_seconds("75"), 75)

    def test_router_arguments_keep_credentials_and_model_settings_separate(self):
        args = installer.clef_service_arguments(Path("/llama-server"), self.preset,
                                                Path("/private/key"))
        self.assertIn("--models-autoload", args)
        options = {
            flag: args[args.index(flag) + 1]
            for flag in ("--models-preset", "--models-max", "--host", "--port", "--api-key-file")
        }
        self.assertEqual(options, {"--models-preset": str(self.preset), "--models-max": "1",
                                  "--host": "127.0.0.1", "--port": "8082",
                                  "--api-key-file": "/private/key"})
        for flag in ("--model", "-c", "-b", "-ub", "-ngl", "--sleep-idle-seconds"):
            self.assertNotIn(flag, args)
