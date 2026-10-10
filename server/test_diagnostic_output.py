import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from diagnostic_output import MAX_OUTPUT_BYTES, preview_tool_outputs


class DiagnosticOutput(unittest.TestCase):
    def test_text_is_bounded_retrievable_and_stable_on_replay(self):
        raw = "Process exited with code 0\n" + "diagnostic 🙂\n" * 5000 + "FINAL SUMMARY"
        original = {"type": "function_call_output", "call_id": "call-1", "output": raw}
        with tempfile.TemporaryDirectory() as state:
            body = {"input": [dict(original)]}
            stats = preview_tool_outputs(body, state)
            text = body["input"][0]["output"]
            self.assertEqual(stats["count"], 1)
            self.assertLessEqual(len(text.encode()), MAX_OUTPUT_BYTES)
            self.assertIn("Process exited with code 0", text)
            self.assertTrue(text.endswith("FINAL SUMMARY"))
            path = Path(state) / "tool-output" / (hashlib.sha256(raw.encode()).hexdigest() + ".txt")
            self.assertIn(str(path), text)
            self.assertEqual(path.read_bytes(), raw.encode())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            replay = {"input": [dict(original)]}
            preview_tool_outputs(replay, state)
            self.assertEqual(replay, body)
            self.assertEqual(preview_tool_outputs(body, state)["count"], 0)
            self.assertEqual(body["input"][0]["call_id"], "call-1")

    def test_only_large_text_results_change(self):
        large = "x" * (MAX_OUTPUT_BYTES + 1)
        items = [{"role": "user", "content": large},
                 {"type": "function_call", "arguments": large},
                 {"type": "reasoning", "encrypted_content": large},
                 {"type": "function_call_output", "output": {"content": large}},
                 {"type": "custom_tool_call_output", "output": "small"}]
        with tempfile.TemporaryDirectory() as state:
            self.assertEqual(preview_tool_outputs({"input": items}, state)["count"], 0)
            self.assertFalse((Path(state) / "tool-output").exists())

    def test_archive_failure_preserves_full_output(self):
        body = {"input": [{"type": "function_call_output", "output": "x" * 20000}]}
        before = body["input"][0]
        with mock.patch("diagnostic_output._archive", side_effect=OSError("disk full")):
            self.assertEqual(preview_tool_outputs(body, "/unused")["count"], 0)
        self.assertIs(body["input"][0], before)

    def test_symlink_directory_and_archive_are_not_followed(self):
        raw = "x" * 20000
        with tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as target:
            root = Path(state) / "tool-output"
            root.symlink_to(target, target_is_directory=True)
            body = {"input": [{"type": "function_call_output", "output": raw}]}
            self.assertEqual(preview_tool_outputs(body, state)["count"], 0)
            self.assertEqual(body["input"][0]["output"], raw)
            root.unlink()
            root.mkdir(mode=0o700)
            victim = Path(target) / "victim"
            victim.write_text("untouched")
            (root / (hashlib.sha256(raw.encode()).hexdigest() + ".txt")).symlink_to(victim)
            self.assertEqual(preview_tool_outputs(body, state)["count"], 0)
            self.assertEqual(victim.read_text(), "untouched")

    def test_corrupt_archive_preserves_original(self):
        raw = "x" * 20000
        with tempfile.TemporaryDirectory() as state:
            root = Path(state) / "tool-output"
            root.mkdir(mode=0o700)
            archive = root / (hashlib.sha256(raw.encode()).hexdigest() + ".txt")
            archive.write_text("y" * len(raw))
            archive.chmod(0o600)
            body = {"input": [{"type": "function_call_output", "output": raw}]}
            self.assertEqual(preview_tool_outputs(body, state)["count"], 0)
            self.assertEqual(body["input"][0]["output"], raw)


if __name__ == "__main__":
    unittest.main()
