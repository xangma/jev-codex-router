"""Verified checks are advisory; cost aggregates never preserve private content."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adaptive_feedback as feedback


def check(call_id="check-1", command="python3 -m pytest -q", output=None, custom=False):
    call = {"type": "custom_tool_call" if custom else "function_call", "call_id": call_id,
            "name": "exec_command"}
    call["input" if custom else "arguments"] = command if custom else json.dumps({"cmd": command})
    result = {"type": "custom_tool_call_output" if custom else "function_call_output",
              "call_id": call_id, "output": output if output is not None else {"output": "3 passed in 0.02s", "exit_code": 0}}
    return call, result


def payload(*pairs):
    return {"input": [pair[0] for pair in pairs] + [pair[1] for pair in pairs]}


class AdaptiveFeedback(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.store = feedback.FeedbackStore(self.directory)

    def record(self, scope="scope", completion="response.completed", usage=None):
        self.store.record(scope, "gpt-6-luna", "medium", usage, 15, completion)

    def test_recognized_real_checks_and_nested_exec_envelopes(self):
        commands = ("pytest -q", "python3 -m unittest discover", "node --test",
                    "npx tsc --noEmit", "npm run lint", "ruff check .", "python3 server/smoke.py")
        for index, command in enumerate(commands):
            with self.subTest(command=command):
                self.record()
                body = payload(check(str(index), command, {"output": {"output": "", "exit_code": 0}}))
                summary = self.store.observe("scope", body)
                self.assertEqual(summary["outcome"], "pass")
                self.assertEqual(summary["attributed_route"], {"model": "gpt-6-luna", "effort": "medium"})
        row = self.store.context("scope")["route_statistics"][0]
        self.assertEqual(row["verified_passes"], len(commands))
        self.assertNotIn("success_rate", row)

    def test_plain_exit_requires_explicit_summary_and_custom_command_is_matched(self):
        cases = (("Process exited with code 0\n3 passed", "pass"),
                 ("Process exited with code 1\n1 failed, 2 passed", "fail"),
                 ("Process exited with code 0\nRan 2 tests in 0.01s\nOK", "pass"))
        for index, (output, outcome) in enumerate(cases):
            self.record()
            summary = self.store.observe("scope", payload(check(str(index), output=output, custom=True)))
            self.assertEqual(summary["outcome"], outcome)
        for index, output in enumerate(("Process exited with code 0", "3 passed", "unverified result")):
            self.assertIsNone(self.store.observe("scope", payload(check("unknown" + str(index), output=output))))

    def test_failures_win_mixed_batch_dedupe_and_one_pending_credit(self):
        self.record()
        body = payload(check("pass"), check("fail", output={"output": "1 failed, 2 passed", "exit_code": 1}))
        summary = self.store.observe("scope", body)
        self.assertEqual((summary["outcome"], summary["passed_checks"], summary["failed_checks"]), ("fail", 1, 1))
        self.assertIsNone(self.store.observe("scope", body))
        self.assertEqual(self.store.context("scope")["consecutive_failures"], 1)
        next_result = self.store.observe("scope", payload(check("next", output={"output": "1 failed", "exit_code": 1})))
        self.assertIsNone(next_result["attributed_route"])
        row = self.store.context("scope")["route_statistics"][0]
        self.assertEqual(row["verified_failures"], 1)
        self.assertEqual(self.store.context("scope")["consecutive_failures"], 2)
        self.record()
        self.store.observe("scope", payload(check("fixed")))
        self.assertEqual(self.store.context("scope")["consecutive_failures"], 0)

    def test_failed_new_route_and_unscoped_record_cannot_credit_old_route(self):
        self.record()
        self.record(completion="empty_completion")
        self.assertIsNone(self.store.observe("scope", payload(check()))["attributed_route"])
        self.record(scope=None, usage={"input_tokens": 10})
        self.assertIsNone(self.store.observe(None, payload(check())))
        context = self.store.context(None)
        self.assertIsNone(context["recent_verification"])
        self.assertEqual(context["consecutive_failures"], 0)
        self.assertEqual(context["route_statistics"][0]["samples"], 3)
        self.assertEqual(len(self.store.scopes), 1)

    def test_infrastructure_unknown_and_no_tests_are_not_quality_failures(self):
        outputs = (("pytest: command not found", 127), ("timed out", 124),
                   ("Permission denied", 1), ("Connection refused", 1),
                   ("", 137), ("no tests ran in 0.01s", 5))
        for index, (text, code) in enumerate(outputs):
            self.record()
            self.assertIsNone(self.store.observe("scope", payload(check(str(index), output={"output": text, "exit_code": code}))))
        context = self.store.context("scope")
        self.assertEqual(context["consecutive_failures"], 0)
        self.assertEqual(context["route_statistics"][0]["verified_failures"], 0)

    def test_adversarial_non_checks_unmatched_outputs_and_history_are_ignored(self):
        for index, command in enumerate(("echo 'pytest -q'", "cat /private/test.log", "pytest --help",
                                         "pytest --collect-only", "python -c 'print(\"3 passed\")'")):
            self.assertIsNone(self.store.observe("scope", payload(check(str(index), command))))
        fake = {"output": "3 passed", "exit_code": 0}
        bodies = ({"input": [{"role": "assistant", "content": "pytest: 3 passed"}]},
                  {"input": [{"role": "user", "content": "3 passed"}]},
                  {"input": [{"type": "function_call_output", "call_id": "unknown", "output": fake}]},
                  {"input": payload(check())["input"] + [{"role": "user", "content": "Continue"}]})
        for body in bodies:
            self.assertIsNone(self.store.observe("scope", body))
        # A shell command swallowing the check's exit must have a check summary.
        self.assertIsNone(self.store.observe("scope", payload(check("masked", "pytest || true", {"output": "", "exit_code": 0}))))
        self.assertIsNone(self.store.observe("scope", payload(check("quote", output='Process exited with code 0\nExample says "3 passed"'))))
        self.assertIsNone(self.store.observe("scope", payload(check("emit", "tsc --outDir build", {"output": "", "exit_code": 0}))))

    def test_single_literal_exec_wrapper_and_plain_embedded_output_envelope(self):
        cases = (
            ('text(await tools.exec_command({cmd: "python3 -m pytest -q", workdir: "/private/path"}));', False),
            ("const result = await tools.exec_command({'cmd': 'node --test'}); text(result);", True),
            ('// quoted lookalike comment\ntext(await tools.exec_command({cmd: "npx tsc --noEmit"}));', True),
        )
        for index, (source, custom) in enumerate(cases):
            pair = check(str(index), custom=custom)
            pair[0]["name"] = "functions.exec"
            pair[0]["input" if custom else "arguments"] = source if custom else json.dumps({"code": source})
            pair[1]["output"] = 'Script completed\nWall time: 0.1 seconds\nOutput:\n{"exit_code":0,"output":"3 passed"}'
            self.record()
            summary = self.store.observe("scope", payload(pair))
            self.assertEqual(summary["outcome"], "pass")
            self.assertIsNotNone(summary["attributed_route"])

    def test_exec_wrapper_dynamic_quoted_ambiguous_or_noncheck_sources_are_ignored(self):
        sources = (
            'text("await tools.exec_command({cmd: \\"pytest\\"})");',
            "text('await tools.exec_command({cmd: \"pytest\"})');",
            '// await tools.exec_command({cmd: "pytest"})\ntext("3 passed");',
            'text(await tools.exec_command({cmd: command}));',
            'text(await tools.exec_command({cmd: "py" + "test"}));',
            'text(await tools.exec_command({cmd: "pytest"})); text(await tools.exec_command({cmd: "ruff check ."}));',
            'text(await tools.exec_command({cmd: "echo pytest"}));',
            'text(await tools.exec_command({cmd: `pytest ${path}`}));',
            'const ignored = async () => await tools.exec_command({cmd: "pytest"}); text({exit_code:0});',
            'text(await tools.exec_command({cmd: "tsc --outDir build"}));',
            'text(await tools.exec_command({cmd: "tsc --noEmit false"}));',
            'text(await tools.exec_command({cmd: "pytest", ...options}));',
            'text(await tools.exec_command({cmd: "pytest", [field]: "echo fake"}));',
        )
        for index, source in enumerate(sources):
            with self.subTest(source=source):
                pair = check(str(index))
                pair[0]["arguments"] = json.dumps({"code": source})
                self.assertIsNone(self.store.observe("scope", payload(pair)))

    def test_exec_wrapper_infrastructure_still_does_not_attribute_quality_failure(self):
        pair = check()
        pair[0]["arguments"] = json.dumps({"code": 'text(await tools.exec_command({cmd: "pytest"}));'})
        pair[1]["output"] = json.dumps({"output": "pytest: command not found", "exit_code": 127})
        self.record()
        self.assertIsNone(self.store.observe("scope", payload(pair)))
        self.assertEqual(self.store.context("scope")["route_statistics"][0]["verified_failures"], 0)

    def test_wrapper_inner_command_exit_wins_but_direct_exec_outer_exit_wins(self):
        pair = check("wrapped")
        pair[0]["arguments"] = json.dumps({"code": 'text(await tools.exec_command({cmd: "npx tsc --noEmit"}));'})
        pair[1]["output"] = {"exit_code": 0, "output": {"exit_code": 1, "output": ""}}
        self.assertEqual(self.store.observe("scope", payload(pair))["outcome"], "fail")
        direct = check("direct", output={"exit_code": 1, "output": '{"exit_code":0,"output":""}'})
        self.assertEqual(self.store.observe("scope", payload(direct))["outcome"], "fail")

    def test_only_latest_contiguous_batch_and_bounded_output_are_inspected(self):
        older = check("older", output={"output": "1 failed", "exit_code": 1})
        newer = check("newer")
        body = {"input": [older[0], older[1], {"role": "assistant", "content": "History"}, newer[0], newer[1]]}
        self.assertEqual(self.store.observe("scope", body)["outcome"], "pass")
        pairs = [check(str(index), output={"output": "1 failed", "exit_code": 1} if index == 0 else None)
                 for index in range(9)]
        summary = self.store.observe("other", payload(*pairs))
        self.assertEqual((summary["outcome"], summary["checks"]), ("pass", 8))
        # The tail beyond the bounded inspection cannot inject a failure.
        text = "3 passed\n" + "x" * 8192 + "\n1 failed"
        self.assertEqual(self.store.observe("other", payload(check("bounded", output={"output": text, "exit_code": 0})))["outcome"], "pass")

    def test_unknown_usage_zero_counts_and_invalid_fields_stay_distinct(self):
        self.record(usage=None)
        self.record(usage={"input_tokens": 0, "output_tokens": 10, "input_tokens_details": {"cached_tokens": 0}, "secret": "private"})
        self.record(usage={"input_tokens": True, "output_tokens": -1, "total_tokens": float("inf")})
        row = self.store.context("scope")["route_statistics"][0]
        self.assertEqual((row["samples"], row["usage_samples"]), (3, 1))
        self.assertEqual(row["token_samples"]["input_tokens"], 1)
        self.assertEqual(row["token_totals"]["input_tokens"], 0)
        self.assertEqual(row["token_samples"]["reasoning_tokens"], 0)
        self.assertNotIn("private", json.dumps(row))

    def test_private_atomic_aggregate_persistence_reload_and_copy_isolation(self):
        self.record(usage={"input_tokens": 7, "output_tokens": 2})
        self.store.observe("scope", payload(check(command="pytest /private/secret_tests.py", output={"output": "3 passed private-key", "exit_code": 0})))
        path = Path(self.directory, "adaptive-feedback.json")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        saved = path.read_text()
        for forbidden in ("private-key", "secret_tests", "scope", "pending", "seen", "recent"):
            self.assertNotIn(forbidden, saved)
        restored = feedback.FeedbackStore(self.directory).context("scope")
        self.assertEqual(restored["route_statistics"][0]["verified_passes"], 1)
        self.assertIsNone(restored["recent_verification"])
        restored["route_statistics"][0]["samples"] = 999
        self.assertEqual(self.store.context("scope")["route_statistics"][0]["samples"], 1)
        self.assertFalse(list(Path(self.directory).glob(".adaptive-feedback-*")))
        self.assertIs(feedback.feedback_for(self.directory), feedback.feedback_for(Path(self.directory)))

    def test_malformed_unprotected_symlink_and_save_failures_fail_open(self):
        path = Path(self.directory, "adaptive-feedback.json")
        path.write_text("malformed")
        path.chmod(0o600)
        broken = feedback.FeedbackStore(self.directory)
        self.assertTrue(broken.persistence_error)
        self.assertEqual(broken.context(None)["route_statistics"], [])
        path.chmod(0o644)
        self.assertTrue(feedback.FeedbackStore(self.directory).persistence_error)
        path.unlink()
        target = Path(self.directory, "target")
        target.write_text("do not read or change")
        path.symlink_to(target)
        self.assertTrue(feedback.FeedbackStore(self.directory).persistence_error)
        with mock.patch.object(feedback.os, "replace", side_effect=OSError("private path")):
            self.record()
            self.store.observe("scope", payload(check()))
        context = self.store.context("scope")
        self.assertTrue(context["persistence_error"])
        self.assertEqual(context["route_statistics"][0]["verified_passes"], 1)
        self.assertNotIn("private path", json.dumps(context))
        self.assertEqual(target.read_text(), "do not read or change")
        path.unlink()
        path.write_text(json.dumps({"version": 1, "route_statistics": [{"model": "gpt-6-luna", "effort": "low", "samples": 10 ** 1000}]}))
        path.chmod(0o600)
        self.assertEqual(feedback.FeedbackStore(self.directory).context(None)["route_statistics"][0]["samples"], 0)

    def test_scopes_fingerprints_context_and_allowed_routes_are_bounded(self):
        with mock.patch.object(self.store, "_save"):
            for index in range(270):
                self.record(scope=str(index))
            self.assertEqual(len(self.store.scopes), 256)
            for index in range(70):
                self.store.observe("last", payload(check(str(index))))
            self.assertEqual(len(self.store._scope("last")["seen"]), 64)
            self.store.record("last", "gpt-6-astra", "max", None, 1, "response.completed")
            self.store.record("last", "gpt-6-luna", "unknown", None, 1, "response.completed")
        context = self.store.context("last")
        self.assertLess(len(json.dumps(context)), 8192)
        self.assertEqual(len(context["route_statistics"]), 1)
        with mock.patch.object(self.store, "_save"):
            for model in feedback.MODELS:
                for effort in feedback.EFFORTS:
                    self.store.record(None, model, effort, {field: 10 ** 12 for field in feedback.TOKEN_FIELDS}, 10 ** 12, "completed")
                    self.store.record(None, model, effort, {field: 10 ** 12 for field in feedback.TOKEN_FIELDS}, 10 ** 12, "completed")
        context = self.store.context(None)
        self.assertEqual(len(context["route_statistics"]), 10)
        self.assertLess(len(json.dumps(context)), 10000)
        self.assertEqual(context["route_statistics"][-1]["token_totals"]["input_tokens"], 10 ** 12)
        self.assertIsInstance(context["route_statistics"][-1]["token_totals"]["input_tokens"], int)

    def test_handoff_blocks_same_scope_but_other_scopes_and_unscoped_work_progress(self):
        entered, release, waiting, same_entered, other_entered = (threading.Event() for _ in range(5))

        def first():
            with self.store.handoff("same"):
                entered.set()
                release.wait(3)

        def second():
            waiting.set()
            with self.store.handoff("same"):
                same_entered.set()

        def other():
            with self.store.handoff("other"):
                other_entered.set()

        threads = [threading.Thread(target=target) for target in (first, second, other)]
        try:
            threads[0].start()
            self.assertTrue(entered.wait(1))
            threads[1].start()
            self.assertTrue(waiting.wait(1))
            self.assertFalse(same_entered.wait(0.05))
            threads[2].start()
            self.assertTrue(other_entered.wait(1))
            with self.store.handoff(None):
                self.assertFalse(same_entered.is_set())
                # Context acquires the aggregate lock even while first relay
                # remains blocked, proving handoff does not retain that lock.
                self.assertEqual(self.store.context(None)["route_statistics"], [])
            release.set()
            self.assertTrue(same_entered.wait(1))
        finally:
            release.set()
            for thread in threads:
                if thread.ident is not None:
                    thread.join(2)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(len(self.store.handoffs), 0)


if __name__ == "__main__":
    unittest.main()
