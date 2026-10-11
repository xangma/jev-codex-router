"""User-editable ranges and the picker ceiling must agree at execution time."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import effort_bounds as bounds


class EffortBounds(unittest.TestCase):
    def test_missing_file_excludes_luna_low_without_creating_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bounds.json"
            value = bounds.load_bounds(path)
            self.assertEqual(bounds.allowed_efforts("luna", value), ["medium", "high", "xhigh", "max"])
            self.assertEqual(bounds.allowed_efforts("sol", value, "low"), ["low"])
            self.assertFalse(path.exists())

    def test_partial_settings_and_native_model_ids(self):
        value = bounds.validate_bounds({"luna": {"min": "high", "max": "xhigh"}})
        self.assertEqual(bounds.allowed_efforts(bounds.LUNA, value), ["high", "xhigh"])
        self.assertEqual(bounds.clamp_effort(bounds.LUNA, "low", value), "high")
        self.assertEqual(bounds.clamp_effort(bounds.LUNA, "max", value), "xhigh")
        self.assertEqual(bounds.allowed_efforts(bounds.SOL, value, "medium"), ["low", "medium"])

    def test_sol_floor_above_picker_ceiling_disables_sol(self):
        value = bounds.validate_bounds({"sol": {"min": "high"}})
        self.assertEqual(bounds.allowed_efforts("sol", value, "low"), [])
        with self.assertRaises(ValueError):
            bounds.clamp_effort("sol", "high", value, "low")

    def test_rejects_invalid_settings(self):
        for value in ([], {"astra": {}}, {"luna": []}, {"luna": {"floor": "high"}},
                      {"luna": {"min": "high", "max": "medium"}},
                      {"sol": {"max": "ultra"}}, {"luna": {"min": None}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                bounds.validate_bounds(value)

    def test_reload_and_invalid_config_do_not_silently_use_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bounds.json"
            with mock.patch.dict(os.environ, {"CLEF_EFFORT_BOUNDS_FILE": str(path)}):
                bounds.write_bounds({"luna": {"min": "low"}})
                self.assertEqual(bounds.allowed_efforts("luna", bounds.load_bounds())[0], "low")
                bounds.write_bounds({"luna": {"min": "high"}})
                self.assertEqual(bounds.allowed_efforts("luna", bounds.load_bounds())[0], "high")
                path.write_text("{broken")
                with self.assertRaises(ValueError):
                    bounds.load_bounds()

    def test_cli_rejects_inverted_ranges_without_overwriting_valid_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bounds.json"
            command = [sys.executable, str(Path(bounds.__file__)), "--file", str(path)]
            result = subprocess.run([*command, "set", "--luna-min", "high", "--luna-max", "xhigh"],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            original = path.read_bytes()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            result = subprocess.run([*command, "set", "--luna-max", "medium"], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(path.read_bytes(), original)
            result = subprocess.run([*command, "show"], capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout)["bounds"]["luna"]["min"], "high")
            result = subprocess.run([*command, "reset"], capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout)["bounds"], bounds.DEFAULT_BOUNDS)


if __name__ == "__main__":
    unittest.main()
