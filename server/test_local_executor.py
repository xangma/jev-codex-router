"""Opt-in executor settings stay portable and reject unsafe or broken routes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_executor as executor


class LocalExecutorSettings(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state" / "executor.json"

    def test_missing_file_is_disabled_without_writing(self):
        self.assertIsNone(executor.load_executor(self.path))
        self.assertFalse(self.path.exists())

    def test_portable_defaults_sorted_efforts_and_private_atomic_reload(self):
        with patch.dict(os.environ, {"CLEF_LOCAL_EXECUTOR_FILE": str(self.path)}):
            first = executor.write_executor({"model": "my-llama/qwen-model"})
            self.assertEqual(first, {"model": "my-llama/qwen-model", **executor.DEFAULT_EXECUTOR})
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
            original_inode = self.path.stat().st_ino
            second = executor.write_executor({"model": "another-llama/owner/qwen:latest",
                                               "efforts": ["high", "low"], "context_window": 262144})
            self.assertEqual(second["efforts"], ["low", "high"])
            self.assertNotEqual(self.path.stat().st_ino, original_inode)
            self.assertEqual(executor.load_executor(), second)

    def test_bad_settings_do_not_overwrite_valid_file(self):
        executor.write_executor({"model": "my-llama/qwen-model"}, self.path)
        original = self.path.read_bytes()
        invalid = [[], {}, {"model": "openai/gpt-6.1-sol"}, {"model": "jev/auto"},
                   {"model": "my-llama/gpt-6-astra"}, {"model": "no-provider"},
                   {"model": "my-llama/qwen\nsecret"}, {"model": "my-llama/../qwen"},
                   {"model": "my-llama/qwen", "endpoint": "https://secret.invalid"},
                   {"model": "my-llama/qwen", "efforts": []},
                   {"model": "my-llama/qwen", "efforts": ["medium", "medium"]},
                   {"model": "my-llama/qwen", "efforts": ["none"]},
                   {"model": "my-llama/qwen", "context_window": True},
                   {"model": "my-llama/qwen", "context_window": 10000001},
                   {"model": "my-llama/qwen", "name": "bad\x00name"},
                   {"model": "my-llama/qwen", "description": "x" * 1201}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                executor.write_executor(value, self.path)
            self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_files_fail_without_fallback_or_raw_error_details(self):
        self.path.parent.mkdir(parents=True)
        for raw in (b'{"secret":', b"\xff", b"x" * (executor.MAX_FILE_BYTES + 1)):
            self.path.write_bytes(raw)
            with self.subTest(raw=raw[:12]), self.assertRaises(ValueError) as caught:
                executor.load_executor(self.path)
            self.assertNotIn("secret", str(caught.exception))

    def test_cli_set_show_disable_and_error_preserves_configuration(self):
        command = [sys.executable, executor.__file__, "--file", str(self.path)]
        result = subprocess.run([*command, "set", "--model", "my-llama/qwen",
                                 "--name", "My Qwen", "--efforts", "medium,high",
                                 "--context-window", "262144"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("refresh-catalog", result.stderr)
        first = self.path.read_bytes()
        self.assertEqual(json.loads(result.stdout)["executor"]["name"], "My Qwen")
        result = subprocess.run([*command, "set", "--model", "jev/auto"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.path.read_bytes(), first)
        result = subprocess.run([*command, "show"], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["executor"]["model"], "my-llama/qwen")
        result = subprocess.run([*command, "disable"], capture_output=True, text=True, check=True)
        self.assertIsNone(json.loads(result.stdout)["executor"])
        self.assertFalse(self.path.exists())
        subprocess.run([*command, "disable"], capture_output=True, check=True)


if __name__ == "__main__":
    unittest.main()
