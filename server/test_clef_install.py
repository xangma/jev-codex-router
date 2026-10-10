"""The local profile must not overwrite or accept exposed credential files."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import io
import json
import urllib.request

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
