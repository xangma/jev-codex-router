"""Successful exec output may quote diagnostics without failing the tool step."""
import json
import unittest

from tool_results import output_text, tool_result_failed


class ToolResultStatus(unittest.TestCase):
    def test_successful_source_or_log_reads_ignore_error_words_and_diagnostics(self):
        output = "traceback error failed exception fatal panic\nERROR: quoted documentation\nFAILED example"
        for code in (0, 0.0):
            for wrapper in (lambda value: value, json.dumps,
                            lambda value: {"content": [{"type": "text", "text": json.dumps(value)}]},
                            lambda value: {"structuredContent": value, "content": [{"type": "text", "text": output}]}):
                with self.subTest(code=code, wrapper=wrapper):
                    self.assertFalse(tool_result_failed(wrapper({"exit_code": code, "output": output})))

    def test_nonzero_exit_is_failure_even_without_diagnostic_words(self):
        for code in (1, -9, 2.0):
            for output in ("", "All checks passed", "Process exited with code 0"):
                with self.subTest(code=code, output=output):
                    self.assertTrue(tool_result_failed({"exit_code": code, "output": output}))

    def test_native_exec_metadata_outranks_printed_documentation(self):
        self.assertFalse(tool_result_failed(
            "Process exited with code 0\nFinal output:\nERROR: example\nProcess exited with code 1"))
        self.assertTrue(tool_result_failed("Process exited with code 7\nFinal output:\nall good"))
        self.assertTrue(tool_result_failed("Process exited with code -15"))
        self.assertFalse(tool_result_failed(
            "Process exited with code 0\nFinal output:\n" + "x" * 100000 + "\nERROR: example"))

    def test_functions_exec_wrapper_uses_inner_exec_status(self):
        for prefix in ("Script completed\nWall time: 1.2 seconds\nOutput:\n",
                       "Chunk ID: abc\nWall time: 1.2 seconds\nProcess exited with code 0\nFinal output:\n"):
            for code, output in ((1, "quiet nonzero"), (0, "ERROR: documentation\nFAILED quoted log")):
                text = prefix + json.dumps({"exit_code": code, "output": output})
                with self.subTest(prefix=prefix, code=code):
                    self.assertEqual(tool_result_failed(text), bool(code))
                    self.assertEqual(output_text(text), output)
        self.assertFalse(tool_result_failed(
            "Some documentation\nOutput:\n" + json.dumps({"exit_code": 1, "output": "example"})))

    def test_oversized_exec_stdout_retains_exit_metadata(self):
        for code in (0, 1):
            encoded = json.dumps({"chunk_id": "abc", "wall_time_seconds": 1.2,
                                  "exit_code": code, "output": "x" * 100000 + "\nERROR: quoted"})
            for text in (encoded, "Script completed\nWall time: 1.2 seconds\nOutput:\n" + encoded):
                with self.subTest(code=code, wrapped=text.startswith("Script")):
                    self.assertEqual(tool_result_failed(text), bool(code))

    def test_large_stdout_before_trailing_status_in_native_exec_wrapper(self):
        output = "x" * (70 * 1024) + "\nERROR: quoted documentation\nFAILED example"
        for code in (0, 1):
            encoded = json.dumps({"output": output, "wall_time_seconds": 1.2,
                                  "exit_code": code})
            for text in (encoded, "Script completed\nWall time: 1.2 seconds\nOutput:\n" + encoded):
                with self.subTest(code=code, wrapped=text.startswith("Script")):
                    self.assertEqual(tool_result_failed(text), bool(code))
                    self.assertEqual(output_text(text, limit=80), output[-80:])

    def test_running_envelopes_do_not_fail_on_partial_output(self):
        for value in ({"session_id": 42, "exit_code": None, "output": "ERROR: retrying"},
                      {"cell_id": "abc", "output": "FAILED draft check"},
                      {"status": "running", "output": "Traceback (most recent call last):"},
                      "Process running with session ID 42\nFinal output:\nFAILED example",
                      "Script running with cell ID abc\nERROR: partial output"):
            with self.subTest(value=value):
                self.assertFalse(tool_result_failed(value))
        self.assertTrue(tool_result_failed({"session_id": 42, "exit_code": 1, "output": ""}))

    def test_plain_unknown_diagnostics_are_anchored(self):
        for text in ("ERROR: command failed", "FAILED (failures=1)",
                     "error command failed", "Tests failed: race", "lint failed",
                     "Traceback (most recent call last):\n  actual stack", "RuntimeError: bad value"):
            with self.subTest(text=text):
                self.assertTrue(tool_result_failed(text))
        for text in ("Document describes exception handling", "0 failed, 4 passed",
                     "Use traceback to inspect this error", "The word fatal appears in the source."):
            with self.subTest(text=text):
                self.assertFalse(tool_result_failed(text))

    def test_invalid_exit_values_are_not_interpreted_as_nonzero_codes(self):
        for code in (True, False, None, float("nan"), float("inf"), 1.5, "1", {}, []):
            with self.subTest(code=code):
                self.assertFalse(tool_result_failed({"exit_code": code, "output": "exception documentation"}))
        self.assertTrue(tool_result_failed({"exit_code": True, "output": "ERROR: actual diagnostic"}))

    def test_nested_mcp_and_multiple_exec_results(self):
        value = {"content": [
            {"type": "text", "text": json.dumps({"exit_code": 0, "output": "failed word"})},
            {"type": "text", "text": json.dumps({"exit_code": 1, "output": ""})},
        ]}
        self.assertTrue(tool_result_failed(value))
        self.assertTrue(tool_result_failed({"isError": True, "content": [{"type": "text", "text": "denied"}]}))
        self.assertFalse(tool_result_failed({"isError": False, "content": [{"type": "text", "text": "exception"}]}))
        self.assertFalse(tool_result_failed({"isError": False, "content": [{"type": "text", "text": "ERROR: quoted"}]}))
        self.assertTrue(tool_result_failed({"isError": False, "structuredContent": {"exit_code": 1}}))
        self.assertTrue(tool_result_failed({"isError": True, "structuredContent": {"exit_code": 0}}))

    def test_known_zero_or_running_metadata_outranks_other_plain_diagnostics(self):
        for metadata in ("Process exited with code 0", "Process running with session ID 42"):
            value = {"content": [{"type": "text", "text": metadata},
                                 {"type": "text", "text": "ERROR: quoted documentation"}]}
            with self.subTest(metadata=metadata):
                self.assertFalse(tool_result_failed(value))

    def test_unknown_or_cyclic_data_is_bounded_and_conservative(self):
        cyclic = {}; cyclic["content"] = cyclic
        for value in (cyclic, None, True, {"unrelated": "ERROR: metadata"},
                      {"content": [{"type": "image", "data": "ERROR: image bytes"}]},
                      "Process exited with code " + "1" * 5000):
            with self.subTest(value=type(value)):
                self.assertFalse(tool_result_failed(value))


class ToolResultPreview(unittest.TestCase):
    def test_extracts_nested_text_without_dictionary_repr_or_binary_content(self):
        value = {"content": [{"type": "image", "data": "private binary fixture"},
                             {"type": "text", "text": json.dumps({"exit_code": 0, "output": "actual output"})}]}
        self.assertEqual(output_text(value), "actual output")
        self.assertEqual(output_text({"unrelated": "large private metadata"}), "")

    def test_preview_is_bounded_for_large_values_and_cycles(self):
        value = {"exit_code": 0, "output": "x" * 100000 + "exception"}
        self.assertEqual(output_text(value, limit=20), "x" * 11 + "exception")
        cyclic = {}; cyclic["content"] = cyclic
        self.assertEqual(output_text(cyclic), "")
        self.assertEqual(output_text(value, limit=0), "")


if __name__ == "__main__":
    unittest.main()
