"""Native control fixtures: no subprocess or model inference is used."""

import contextlib
import io
import json
import queue
import subprocess
import sys
import time
import unittest
from unittest import mock
import uuid

import claude_auto as ca


SONNET = "claude-sonnet-fixture"
OPUS = "claude-opus-fixture"
HAIKU = "claude-haiku-fixture"
CATALOG = [
    {"value": "default", "description": "Sonnet", "supportsEffort": True,
     "supportedEffortLevels": ["low", "medium", "high", "max"]},
    {"value": "sonnet", "description": "Duplicate Sonnet", "supportsEffort": True,
     "supportedEffortLevels": ["low", "medium", "high", "max"]},
    {"value": "opus", "description": "Opus", "supportsEffort": True,
     "supportedEffortLevels": ["low", "medium", "high", "max"]},
    {"value": "haiku", "description": "Haiku"},
]


class Output:
    def __init__(self):
        self.lines = queue.Queue()
        self.closed = False

    def readline(self, size):
        return self.lines.get()

    def put(self, message):
        self.lines.put(json.dumps(message) + "\n")

    def close(self):
        self.closed = True


class Input:
    def __init__(self, process):
        self.process = process

    def write(self, line):
        self.process.accept(json.loads(line))

    def flush(self):
        pass

    def close(self):
        if self.process.returncode is None and not self.process.hang_on_close:
            self.process.exit(0)


class FixtureProcess:
    pid = None

    def __init__(self, args, *, catalog=None, reject=None, omit_ack=None,
                 bad_applied=None, max_live=False, ignore_initial_effort=False,
                 ignore_effort=None, permission_requests=None, wrong_session=False,
                 hang_on_close=False, malformed=False, premature_exit=False,
                 keep_pipe_open=False, history=None, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.catalog = CATALOG if catalog is None else catalog
        self.reject = reject
        self.omit_ack = omit_ack
        self.bad_applied = bad_applied
        self.max_live = max_live
        self.ignore_effort = ignore_effort
        self.permission_requests = permission_requests or []
        self.wrong_session = wrong_session
        self.hang_on_close = hang_on_close
        self.malformed = malformed
        self.premature_exit = premature_exit
        self.keep_pipe_open = keep_pipe_open
        self.history = history if history is not None else {}
        self.returncode = None
        self.stdin = Input(self)
        self.stdout = Output()
        self.stderr = io.StringIO("private native stderr that must not be mirrored")
        self.messages = []
        self.permission_answers = []
        self.aliases = {"default": SONNET, "sonnet": SONNET, "opus": OPUS, "haiku": HAIKU}
        self.model = self.aliases.get(self.arg("--model"), self.arg("--model")) or SONNET
        self.effort = None if ignore_initial_effort else self.arg("--effort")
        self.effort = self.effort or "medium"
        self.session_id = next(a.split("=", 1)[1] for a in args
                               if a.startswith(("--session-id=", "--resume=")))
        self.history.setdefault(self.session_id, [])

    def arg(self, name):
        return self.args[self.args.index(name) + 1] if name in self.args else None

    def poll(self):
        return self.returncode

    def exit(self, code):
        self.returncode = code
        if not self.keep_pipe_open:
            self.stdout.lines.put("")

    def wait(self, timeout):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fixture", timeout)
        return self.returncode

    def terminate(self):
        self.exit(-15)

    def kill(self):
        self.exit(-9)

    def ack(self, request_id, body=None):
        response = {"subtype": "success", "request_id": request_id}
        if body is not None:
            response["response"] = body
        self.stdout.put({"type": "control_response", "response": response})

    def finish_turn(self):
        sid = str(uuid.uuid4()) if self.wrong_session else self.session_id
        self.stdout.put({"type": "system", "subtype": "init", "session_id": sid})
        self.stdout.put({"type": "assistant", "session_id": sid,
                         "message": {"model": self.model, "content": [
                             {"type": "text", "text": "native answer"}]}})
        self.stdout.put({"type": "result", "subtype": "success", "is_error": False,
                         "result": "native answer", "session_id": sid})

    def accept(self, message):
        self.messages.append(message)
        if message["type"] == "user":
            self.history[self.session_id].append(message["message"]["content"])
            if self.permission_requests:
                self.stdout.put({"type": "control_request", "request_id": "permission-1",
                                 "request": self.permission_requests.pop(0)})
            else:
                self.finish_turn()
            return
        if message["type"] == "control_response":
            self.permission_answers.append(message)
            if self.permission_requests:
                self.stdout.put({"type": "control_request", "request_id": "permission-2",
                                 "request": self.permission_requests.pop(0)})
            else:
                self.finish_turn()
            return
        request, rid = message["request"], message["request_id"]
        subtype = request["subtype"]
        if subtype == self.omit_ack:
            # An unrelated acknowledgment must never unlock the user prompt.
            self.ack("wrong-request-id")
            return
        if subtype == self.reject:
            self.stdout.put({"type": "control_response", "response": {
                "subtype": "error", "request_id": rid, "error": "private error contents"}})
            return
        if subtype == "initialize":
            self.ack(rid, {"models": self.catalog, "commands": [], "agents": [],
                           "account": {"secret": "private account fixture"}})
        elif subtype == "get_settings":
            applied = self.bad_applied or {"model": self.model,
                                          "effort": None if self.model == HAIKU else self.effort}
            self.ack(rid, {"applied": applied, "effective": {"secret": "private settings fixture"},
                           "sources": [{"secret": "private settings fixture"}]})
        elif subtype == "set_model":
            self.model = self.aliases.get(request["model"], request["model"])
            self.ack(rid)
        elif subtype == "apply_flag_settings":
            if self.malformed:
                self.stdout.lines.put("private invalid protocol contents\n")
                return
            if self.premature_exit:
                self.exit(1)
                return
            settings = request["settings"]
            self.model = self.aliases.get(settings["model"], settings["model"])
            effort = settings.get("effortLevel")
            if effort != self.ignore_effort and (effort != "max" or self.max_live):
                self.effort = effort
            self.ack(rid)
        elif subtype == "interrupt":
            self.ack(rid)
        else:
            raise AssertionError(subtype)


class NativeFactory:
    def __init__(self, **fixture_options):
        self.fixture_options = fixture_options
        self.processes = []
        self.history = {}

    def __call__(self, **native_options):
        def popen(args, **kwargs):
            p = FixtureProcess(args, history=self.history, **self.fixture_options, **kwargs)
            self.processes.append(p)
            return p
        return ca.NativeClaude(popen=popen, request_timeout=0.05, turn_timeout=0.1, **native_options)


def decision(model=SONNET, effort="medium"):
    return {"model": model, "effort": effort, "source": "clef"}


class ControllerTests(unittest.TestCase):
    def controller(self, *, choice=None, factory=None, **options):
        factory = factory or NativeFactory()
        c = ca.ClaudeAuto(native_factory=factory,
                          decider=lambda *args, **kwargs: choice or decision(), **options)
        self.addCleanup(c.close)
        return c, factory

    def test_initial_and_continuation_preserve_exact_prompt_and_native_history(self):
        calls = []
        def decider(task, candidates, **kwargs):
            calls.append((task, candidates, kwargs))
            return decision()
        c, factory = self.controller()
        c._decider = decider
        prompt = "  Original\n雪 `literal` $not_shell\n "
        self.assertEqual(c.run_turn(prompt)["result"], "native answer")
        c.run_turn("Continue exactly")
        p = factory.processes[0]
        users = [m for m in p.messages if m["type"] == "user"]
        self.assertEqual([u["message"]["content"] for u in users], [prompt, "Continue exactly"])
        self.assertTrue(all(u["session_id"] == c.session_id for u in users))
        self.assertEqual(factory.history[c.session_id], [prompt, "Continue exactly"])
        self.assertEqual(calls[1][2]["context"]["previous_task"], prompt)
        self.assertEqual(len(factory.processes), 1)
        self.assertNotIn("env", p.kwargs)
        self.assertNotIn("cwd", p.kwargs)
        self.assertFalse(any(a in p.args for a in ["--bare", "--settings", "--dangerously-skip-permissions"]))

    def test_inventory_resolves_aliases_deduplicates_and_uses_native_caps(self):
        c, _ = self.controller()
        c.start()
        candidates = c.candidates()
        self.assertEqual(len(candidates), 9)
        self.assertEqual({x["model"] for x in candidates}, {SONNET, OPUS, HAIKU})
        self.assertEqual([x["effort"] for x in candidates if x["model"] == HAIKU], [None])
        self.assertEqual([x["effort"] for x in candidates if x["model"] == SONNET], list(ca.EFFORTS))

    def test_every_mode_keeps_haiku_eligible_and_uses_only_set_model(self):
        for mode in ca.MODES:
            with self.subTest(mode=mode):
                c, factory = self.controller(choice=decision(HAIKU, None), mode=mode)
                c.start()
                before = len(factory.processes[0].messages)
                c.run_turn("A simple task")
                outgoing = factory.processes[0].messages[before:]
                self.assertEqual(outgoing[0]["request"], {"subtype": "set_model", "model": HAIKU})
                self.assertEqual(c.last_decision["effort"], None)
                c.close()

    def test_route_is_verified_before_user_input(self):
        c, factory = self.controller(choice=decision(OPUS, "high"))
        c.run_turn("task")
        messages = factory.processes[0].messages
        user_index = next(i for i, m in enumerate(messages) if m["type"] == "user")
        self.assertEqual(messages[user_index - 2]["request"], {
            "subtype": "apply_flag_settings", "settings": {"model": OPUS, "effortLevel": "high"}})
        self.assertEqual(messages[user_index - 1]["request"], {"subtype": "get_settings"})

    def test_route_receipt_precedes_native_input_and_stays_on_stderr(self):
        c, factory = self.controller(choice=decision(OPUS, "high"), mode="auto-deep")
        receipt = []
        def on_event(event):
            if event["type"] == "clef_route":
                self.assertFalse(any(m["type"] == "user" for m in factory.processes[0].messages))
                receipt.append(event)
        c.run_turn("task", on_event=on_event)
        self.assertEqual(receipt, [{"type": "clef_route", "mode": "auto-deep", "model": OPUS,
                                    "effort": "high", "source": "clef"}])
        with contextlib.redirect_stdout(io.StringIO()) as stdout, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            ca.route_receipt(receipt[0])
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("auto-deep", stderr.getvalue())
        self.assertIn("high", stderr.getvalue())

    def test_unknown_model_or_effort_never_reaches_native_user_input(self):
        for choice in [decision("unavailable-model"), decision(HAIKU, "high"),
                       decision(SONNET, "xhigh"), {"effort": "high"}, None]:
            with self.subTest(choice=choice):
                c, factory = self.controller()
                c._decider = lambda *a, **k: choice
                with self.assertRaises(ca.ControllerError):
                    c.run_turn("private task")
                self.assertFalse(any(m["type"] == "user" for m in factory.processes[0].messages))

    def test_mismatched_effort_fails_closed(self):
        c, factory = self.controller(choice=decision(SONNET, "low"),
                                     factory=NativeFactory(ignore_effort="low"))
        with self.assertRaisesRegex(ca.ControllerError, "do not match"):
            c.run_turn("task")
        self.assertFalse(any(m["type"] == "user" for m in factory.processes[0].messages))

    def test_rejected_control_is_sanitized_and_fails_closed(self):
        c, factory = self.controller(factory=NativeFactory(reject="apply_flag_settings"))
        with self.assertRaises(ca.ControllerError) as error:
            c.run_turn("private task")
        self.assertNotIn("private", str(error.exception))
        self.assertFalse(any(m["type"] == "user" for m in factory.processes[0].messages))

    def test_unrelated_ack_does_not_unlock_prompt_and_timeout_is_bounded(self):
        c, factory = self.controller(factory=NativeFactory(omit_ack="apply_flag_settings"))
        with self.assertRaisesRegex(ca.ControllerError, "timed out"):
            c.run_turn("task")
        self.assertFalse(any(m["type"] == "user" for m in factory.processes[0].messages))

    def test_malformed_output_and_early_exit_fail_closed_without_private_details(self):
        for fixture_options in [{"malformed": True}, {"premature_exit": True}]:
            with self.subTest(options=fixture_options):
                c, factory = self.controller(factory=NativeFactory(**fixture_options))
                with self.assertRaises(ca.ControllerError) as error:
                    c.run_turn("private task")
                self.assertNotIn("private", str(error.exception))
                self.assertFalse(any(m["type"] == "user" for m in factory.processes[0].messages))

    def test_max_transition_resumes_same_uuid_and_preserves_history(self):
        c, factory = self.controller()
        c.run_turn("First task")
        c._decider = lambda *a, **k: decision(OPUS, "max")
        c.run_turn("Continue with deep analysis")
        self.assertEqual(len(factory.processes), 2)
        first, resumed = factory.processes
        self.assertEqual(first.returncode, 0)
        self.assertIn(f"--resume={c.session_id}", resumed.args)
        self.assertNotIn(f"--session-id={c.session_id}", resumed.args)
        self.assertEqual(resumed.arg("--effort"), "max")
        self.assertEqual(factory.history[c.session_id], ["First task", "Continue with deep analysis"])
        self.assertEqual(c.native.applied, {"model": OPUS, "effort": "max"})

    def test_initial_max_reuses_new_uuid_without_resuming_absent_transcript(self):
        c, factory = self.controller(choice=decision(OPUS, "max"))
        c.run_turn("First task")
        self.assertEqual(len(factory.processes), 2)
        restarted = factory.processes[1]
        self.assertIn(f"--session-id={c.session_id}", restarted.args)
        self.assertFalse(any(a.startswith("--resume=") for a in restarted.args))
        self.assertEqual(factory.history[c.session_id], ["First task"])

    def test_existing_session_max_restarts_with_resume(self):
        sid = str(uuid.uuid4())
        c, factory = self.controller(choice=decision(OPUS, "max"), resume=sid)
        c.run_turn("Next task")
        self.assertTrue(all(f"--resume={sid}" in p.args for p in factory.processes))

    def test_newer_cli_live_max_does_not_restart(self):
        c, factory = self.controller(choice=decision(OPUS, "max"),
                                     factory=NativeFactory(max_live=True))
        c.run_turn("task")
        self.assertEqual(len(factory.processes), 1)
        self.assertEqual(c.native.applied, {"model": OPUS, "effort": "max"})

    def test_failed_max_startup_setting_never_sends_user_prompt(self):
        c, factory = self.controller(choice=decision(OPUS, "max"),
                                     factory=NativeFactory(ignore_initial_effort=True))
        with self.assertRaisesRegex(ca.ControllerError, "do not match"):
            c.run_turn("task")
        self.assertTrue(all(not any(m["type"] == "user" for m in p.messages) for p in factory.processes))

    def test_max_requires_clean_native_exit(self):
        c, factory = self.controller(choice=decision(OPUS, "max"),
                                     factory=NativeFactory(hang_on_close=True))
        with self.assertRaisesRegex(ca.ControllerError, "did not close cleanly"):
            c.run_turn("task")
        self.assertEqual(len(factory.processes), 1)
        self.assertEqual(factory.processes[0].returncode, -15)

    def test_cleanup_is_bounded_when_background_job_keeps_pipe_open(self):
        factory = NativeFactory(keep_pipe_open=True)
        native = factory(session_id=str(uuid.uuid4())).start()
        started = time.monotonic()
        try:
            self.assertFalse(native.close())
            self.assertLess(time.monotonic() - started, 2)
            self.assertFalse(factory.processes[0].stdout.closed)
        finally:
            # Release the fixture's inherited pipe without launching a job.
            factory.processes[0].stdout.lines.put("")
            native.close()

    def test_permissions_are_denied_without_callback(self):
        request = {"subtype": "can_use_tool", "tool_name": "Bash",
                   "input": {"command": "pytest"}, "tool_use_id": "tool-1"}
        c, factory = self.controller(factory=NativeFactory(permission_requests=[request]))
        c.run_turn("task")
        answer = factory.processes[0].permission_answers[0]["response"]
        self.assertEqual(answer["request_id"], "permission-1")
        self.assertEqual(answer["response"]["behavior"], "deny")
        self.assertNotIn("updatedPermissions", answer["response"])

    def test_each_permission_requires_explicit_approval_and_preserves_input(self):
        request = {"subtype": "can_use_tool", "tool_name": "Bash",
                   "input": {"command": "pytest", "timeout_ms": 1234}, "tool_use_id": "tool-1"}
        seen = []
        approvals = iter([True, False])
        def permission(req):
            seen.append(req)
            return next(approvals)
        c, factory = self.controller(factory=NativeFactory(permission_requests=[request, request]),
                                     permission=permission)
        c.run_turn("task")
        answers = [m["response"]["response"] for m in factory.processes[0].permission_answers]
        self.assertEqual(len(seen), 2)
        self.assertEqual(answers[0], {"behavior": "allow", "updatedInput": request["input"]})
        self.assertEqual(answers[1]["behavior"], "deny")

    def test_invalid_permission_message_is_not_approved(self):
        request = {"subtype": "can_use_tool", "tool_name": "Bash", "input": {}}
        c, factory = self.controller(factory=NativeFactory(permission_requests=[request]),
                                     permission=lambda req: True)
        with self.assertRaisesRegex(ca.ControllerError, "Invalid native tool permission"):
            c.run_turn("task")
        self.assertFalse(factory.processes[0].permission_answers)

    def test_response_session_mismatch_is_rejected(self):
        c, _ = self.controller(factory=NativeFactory(wrong_session=True))
        with self.assertRaisesRegex(ca.ControllerError, "different session"):
            c.run_turn("task")

    def test_resume_and_session_ids_are_validated_before_spawn(self):
        for sid in ["not-a-uuid", "--flag", None, ""]:
            if sid is None:
                continue
            with self.subTest(sid=sid), self.assertRaises(ca.ControllerError):
                ca.NativeClaude(resume=sid)
        with self.assertRaises(ca.ControllerError):
            ca.NativeClaude(resume=str(uuid.uuid4()), session_id=str(uuid.uuid4()))
        with self.assertRaises(ca.ControllerError):
            ca.ClaudeAuto(resume="")

    def test_timeouts_must_be_finite_and_positive(self):
        for value in [0, -1, float("nan"), float("inf"), True, "30"]:
            with self.subTest(value=value), self.assertRaises(ca.ControllerError):
                ca.NativeClaude(request_timeout=value)

    def test_empty_catalog_and_missing_caps_fail_before_inference(self):
        for catalog in [[], [{"value": "sonnet", "supportsEffort": True}]]:
            with self.subTest(catalog=catalog):
                c, factory = self.controller(factory=NativeFactory(catalog=catalog))
                with self.assertRaises(ca.ControllerError):
                    c.run_turn("task")
                self.assertEqual(factory.processes[0].returncode, 0)
                self.assertFalse(any(m["type"] == "user" for m in factory.processes[0].messages))

    def test_doctor_only_prints_sanitized_catalog_and_applied_metadata(self):
        c, factory = self.controller()
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(ca, "ClaudeAuto", return_value=c), \
                mock.patch.object(ca, "cli_version", return_value="2.1.110"), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(ca.main(["--doctor"]), 0)
        data = json.loads(stdout.getvalue())
        self.assertEqual(set(data), {"cli_version", "models", "applied"})
        self.assertNotIn("private", stdout.getvalue() + stderr.getvalue())
        self.assertFalse(any(m["type"] == "user" for m in factory.processes[0].messages))

    def test_print_sends_one_exact_turn_and_returns_native_json(self):
        c, factory = self.controller()
        stdout, stderr = io.StringIO(), io.StringIO()
        prompt = " exact\nuser task "
        with mock.patch.object(ca, "ClaudeAuto", return_value=c), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(ca.main(["--print", prompt, "--output-format", "json"]), 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["type"], "result")
        self.assertEqual(factory.history[c.session_id], [prompt])
        self.assertNotIn("private", stdout.getvalue() + stderr.getvalue())

    def test_noninteractive_tool_approval_is_denied_without_output(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()) as stdout, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertFalse(ca.approve_tool({"tool_name": "Bash", "input": {"command": "pytest"}}))
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")

    def test_permission_ui_keeps_json_stdout_clean(self):
        stdin = io.StringIO("yes\n")
        stderr = io.StringIO()
        with mock.patch.object(sys, "stdin", stdin), \
                mock.patch.object(sys, "stderr", stderr), \
                mock.patch.object(stdin, "isatty", return_value=True), \
                mock.patch.object(stderr, "isatty", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertTrue(ca.approve_tool({"tool_name": "Bash", "input": {"command": "pytest"}}))
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("pytest", stderr.getvalue())

    def test_context_remains_bounded(self):
        c, _ = self.controller()
        c.run_turn("x" * 20000)
        self.assertEqual(len(c.context["previous_task"]), 4000)


if __name__ == "__main__":
    unittest.main()
