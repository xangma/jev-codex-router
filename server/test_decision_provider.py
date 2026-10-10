"""Clef provider isolation and real loopback decision/routing workflows."""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jev_server as jev
import test_per_call_routing as routing


def clef_env(key_file="/not/read/in/config", endpoint="http://127.0.0.1:8082/v1/systemone"):
    return {"JEV_DECISION_PROVIDER": "clef", "CLEF_ENDPOINT": endpoint,
            "CLEF_MODEL": "clef-flash", "CLEF_API_KEY_FILE": key_file,
            "JEV_DECISION_TIMEOUT": "30", "JEV_BASELINE_MODEL": jev.SOL}


class DecisionConfiguration(unittest.TestCase):
    def test_default_profile_preserves_jev_and_astra(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(jev, "call_jev", return_value={"answers": {}}) as hosted:
            config = jev.decision_config()
            self.assertEqual(config, {"provider": "jev", "endpoint": jev.API,
                                     "model": jev.MODEL, "timeout": 4.0, "baseline": jev.ASTRA})
            jev.call_jev_routed("hosted-fixture", "state", timeout=15)
            hosted.assert_called_once_with("hosted-fixture", "state", None, timeout=15)

    def test_rejects_malformed_local_configuration_before_loading_credentials(self):
        cases = [
            {"JEV_DECISION_PROVIDER": "unknown"}, {"CLEF_ENDPOINT": ""},
            {"CLEF_ENDPOINT": "https://example.com/v1/systemone"},
            {"CLEF_ENDPOINT": "http://localhost/v1/systemone"},
            {"CLEF_ENDPOINT": "http://0.0.0.0/v1/systemone"},
            {"CLEF_ENDPOINT": "file://127.0.0.1/v1/systemone"},
            {"CLEF_ENDPOINT": "http://127.0.0.1/v1/chat/completions"},
            {"CLEF_ENDPOINT": "http://private:secret@127.0.0.1/v1/systemone"},
            {"CLEF_ENDPOINT": "http://127.0.0.1:99999/v1/systemone"},
            {"CLEF_ENDPOINT": "http://127.0.0.1:0/v1/systemone"},
            {"CLEF_ENDPOINT": "http://127.0.0.1/v1/systemone?key=private"},
            {"CLEF_ENDPOINT": "http://127.0.0.1/v1/systemone#private"},
            {"CLEF_MODEL": ""}, {"CLEF_API_KEY_FILE": ""},
            {"JEV_DECISION_TIMEOUT": "nan"}, {"JEV_DECISION_TIMEOUT": "inf"},
            {"JEV_DECISION_TIMEOUT": "-1"}, {"JEV_DECISION_TIMEOUT": "0"},
            {"JEV_DECISION_TIMEOUT": "bad"}, {"JEV_BASELINE_MODEL": "external/model"},
        ]
        for override in cases:
            with self.subTest(override=override), \
                 mock.patch.dict(os.environ, {**clef_env(), **override}, clear=True), \
                 mock.patch.object(jev, "load_key") as hosted_key, \
                 mock.patch.object(jev, "load_clef_key") as local_key, \
                 mock.patch.object(jev.urllib.request, "build_opener") as opener:
                with self.assertRaises(ValueError) as error:
                    jev.call_jev_routed("hosted-fixture", "state")
                self.assertNotIn("private", str(error.exception))
                hosted_key.assert_not_called()
                local_key.assert_not_called()
                opener.assert_not_called()

    def test_accepts_ipv6_loopback_and_alias_baseline(self):
        env = {**clef_env(endpoint="http://[::1]:8082/v1/systemone"), "JEV_BASELINE_MODEL": "sol"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(jev.decision_config()["baseline"], jev.SOL)

    def test_clef_caps_explicit_astra_baseline_and_new_two_choice_answers(self):
        with mock.patch.dict(os.environ, {**clef_env(), "JEV_BASELINE_MODEL": jev.ASTRA}, clear=True):
            self.assertEqual(jev.decision_config()["baseline"], jev.SOL)
        answers = routing.answer(jev.SOL, "high")["answers"]
        answers.pop("astra_policy")
        answers["model"]["probabilities"] = {"luna": 0.1, "sol": 0.9}
        self.assertEqual(jev.provider_decision(answers, "clef")["model"], jev.SOL)
        old = routing.answer(jev.LUNA, "high", astra_required=True)["answers"]
        self.assertEqual(jev.provider_decision(old, "clef")["model"], jev.SOL)
        self.assertEqual(jev.provider_decision(old, "jev")["model"], jev.ASTRA)

    def test_soft_profiles_preserve_every_model_effort_choice(self):
        for mode, profile in jev.CLEF_MODES.items():
            for model in jev.CLEF_MODELS:
                for effort in jev.EFFORTS:
                    with self.subTest(mode=mode, model=model, effort=effort):
                        answer = routing.answer(model, effort)["answers"]
                        decision = jev.provider_decision(answer, "clef", mode)
                        self.assertEqual(decision["model"], model)
                        self.assertEqual(decision["effort"], effort)
                        self.assertEqual(jev.provider_decision(answer, "jev")["effort"], effort)
            answers = routing.answer(jev.LUNA, profile["efforts"][0])["answers"]
            answers["effort"]["probabilities"] = {value: int(value == profile["efforts"][0])
                                                 for value in profile["efforts"]}
            self.assertEqual(jev.provider_decision(answers, "clef", mode)["model"], jev.LUNA)
            self.assertEqual(set(jev.clef_questions(mode)["effort"]["criteria"]), set(profile["efforts"]))
            self.assertEqual(set(profile["efforts"]), set(jev.EFFORTS))
            self.assertIn(profile["preference"], jev.clef_questions(mode)["effort"]["instructions"])
        self.assertNotEqual(jev.clef_questions("auto-cheap"), jev.clef_questions("auto-deep"))

    def test_clef_key_is_explicit_protected_and_never_discovers_hosted_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "clef.key")
            path.write_text("local-fixture\n")
            path.chmod(0o600)
            env = {**clef_env(str(path)), "JEV_API_KEY": "hosted-fixture",
                   "TYPESAFE_API_KEY": "hosted-fixture", "JEV_API_KEY_FILE": str(path)}
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(jev, "load_key") as hosted:
                self.assertEqual(jev.load_decision_key(), "local-fixture")
                path.chmod(0o644)
                self.assertEqual(jev.load_decision_key(), "")
                path.chmod(0o600)
                link = Path(tmp, "link")
                link.symlink_to(path)
                with mock.patch.dict(os.environ, {"CLEF_API_KEY_FILE": str(link)}):
                    self.assertEqual(jev.load_decision_key(), "")
                path.unlink()
                self.assertEqual(jev.load_decision_key(), "")
                hosted.assert_not_called()

    def test_local_timeout_overrides_ask_timeout_without_proxy_or_redirect_handlers(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"answers":{}}'
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.dict(os.environ, clef_env(), clear=True), \
             mock.patch.object(jev, "load_clef_key", return_value="local-fixture"), \
             mock.patch.object(jev.urllib.request, "build_opener", return_value=opener) as build:
            jev.call_jev_routed("hosted-fixture", "state", timeout=15)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 30)
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertIsInstance(build.call_args.args[1], jev._NoDecisionRedirect)


class DecisionEndpoint(BaseHTTPRequestHandler):
    requests = []
    answer = None
    status = 200
    redirect = None

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append((self.path, dict(self.headers), body))
        data = json.dumps(type(self).answer).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if type(self).redirect:
            self.send_header("Location", type(self).redirect)
        self.end_headers()
        self.wfile.write(data)


class ClefRoutingWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.servers = []
        for handler in (DecisionEndpoint, routing.Edge, jev.Handler):
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            server.daemon_threads = True
            threading.Thread(target=server.serve_forever, daemon=True).start()
            cls.servers.append(server)
        cls.provider, cls.edge, cls.router = cls.servers

    @classmethod
    def tearDownClass(cls):
        for server in cls.servers:
            server.shutdown()
            server.server_close()

    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        key = Path(tmp, "clef.key")
        key.write_text("local-fixture\n")
        key.chmod(0o600)
        endpoint = f"http://127.0.0.1:{self.provider.server_address[1]}/v1/systemone"
        self.enterContext(mock.patch.dict(os.environ, clef_env(str(key), endpoint), clear=True))
        self.hosted = self.enterContext(mock.patch.object(jev, "call_jev"))
        self.hosted_key = self.enterContext(mock.patch.object(jev, "load_key"))
        self.enterContext(mock.patch.object(jev, "local_secret", return_value="caller-fixture"))
        self.enterContext(mock.patch.object(jev, "caller_secret", return_value="edge-fixture"))
        self.enterContext(mock.patch.object(jev, "ROUTER", self.edge.server_address))
        self.enterContext(mock.patch.object(jev, "native_dry", return_value=None))
        self.enterContext(mock.patch.object(jev, "STATE", tmp))
        for name in ("OFF_PATH", "SHADOW_PATH", "DEBUG_PATH", "HIDE_SIGNATURE_PATH",
                     "LOG_PATH", "DRY_STATE_PATH", "DRY_MANUAL_PATH"):
            self.enterContext(mock.patch.object(jev, name, os.path.join(tmp, name)))
        self.records = []
        self.logged = threading.Event()

        def record(entry):
            self.records.append(entry)
            self.logged.set()

        self.enterContext(mock.patch.object(jev, "log_line", side_effect=record))
        with jev._cache_affinity_lock:
            jev._cache_affinity.clear()
        with jev._route_lease_lock:
            jev._route_leases.clear()
            jev._route_failures.clear()
        DecisionEndpoint.requests = []
        DecisionEndpoint.answer = routing.answer(jev.LUNA, "low")
        DecisionEndpoint.status = 200
        DecisionEndpoint.redirect = None
        routing.Edge.payloads = []
        routing.Edge.headers_seen = []

    def tearDown(self):
        self.hosted.assert_not_called()
        self.hosted_key.assert_not_called()

    def request(self, path="/v1/responses", body=None, authorization="Bearer caller-fixture"):
        if body is None and path == "/v1/responses":
            body = routing.payload_for([routing.message("user", "Explain this simple supplied fact.")])
        headers = {"Content-Type": "application/json"}
        if authorization:
            headers["Authorization"] = authorization
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.router.server_address[1]}{path}",
            data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.read()

    def route(self, body=None):
        self.logged.clear()
        status, _ = self.request(body=body)
        self.assertEqual(status, 200)
        self.assertTrue(self.logged.wait(3))
        return self.records[-1]

    def test_local_body_auth_and_choice_decode_use_only_luna_sol_questions(self):
        record = self.route()
        path, headers, body = DecisionEndpoint.requests[0]
        self.assertEqual(path, "/v1/systemone")
        self.assertEqual(headers["Authorization"], "Bearer local-fixture")
        self.assertEqual(body["model"], "clef-flash")
        self.assertEqual(body["questions"], jev.clef_questions("auto"))
        self.assertEqual(set(body["questions"]), {"model", "effort", "lease"})
        self.assertEqual(set(body["questions"]["model"]["criteria"]), {"luna", "sol"})
        self.assertIn("task", body["state"])
        self.assertEqual((record["model"], record["effort"]), (jev.LUNA, "low"))
        self.assertEqual(record["decision_source"], "clef")
        self.assertEqual(record["decision_provider"], "clef")
        self.assertIsNotNone(record["decision_ms"])
        self.assertIsNone(record["jev_ms"])
        self.assertIsNone(record["jev_usage"])
        self.assertEqual(record["decision_usage"]["input_tokens"], 100)

    def test_valid_low_confidence_choice_is_not_replaced_by_baseline(self):
        for answer in DecisionEndpoint.answer["answers"].values():
            answer["confidence"] = 0.01
        self.assertEqual(self.route()["model"], jev.LUNA)

    def test_all_modes_preserve_both_models_and_bound_final_replayed_effort(self):
        for mode, profile in jev.CLEF_MODES.items():
            for model, requested in ((jev.LUNA, "max"), (jev.SOL, "low")):
                with self.subTest(mode=mode, model=model):
                    DecisionEndpoint.answer = routing.answer(model, requested)
                    update = {"type": "configuration_update", "model": jev.ASTRA,
                              "reasoning": {"effort": "max" if requested == "low" else "low"}}
                    body = routing.payload_for([routing.message("user", "Continue."), update],
                                               model=mode, reasoning={"effort": "low"})
                    record = self.route(body)
                    sent = routing.Edge.payloads[-1]
                    self.assertEqual(record["model"], model)
                    self.assertEqual(record["auto_mode"], mode)
                    self.assertIn(record["effort"], profile["efforts"])
                    self.assertIn(sent["reasoning"]["effort"], profile["efforts"])
                    final_update = sent["input"][-1]
                    self.assertEqual(final_update["reasoning"]["effort"], record["effort"])
                    self.assertNotIn("model", final_update)

    def test_clef_dispatch_suppresses_nested_router_failover_and_jev_default_remains(self):
        for mode in jev.CLEF_MODES:
            self.route(routing.payload_for([routing.message("user", "Continue.")], model=mode))
            self.assertEqual(routing.Edge.headers_seen[-1].get("x-codex-router-exact-route"), "1")
        Path(jev.OFF_PATH).touch()
        with mock.patch.dict(os.environ, {"JEV_DECISION_PROVIDER": "jev"}):
            self.route()
        self.assertNotIn("x-codex-router-exact-route", routing.Edge.headers_seen[-1])

    def test_mode_switch_does_not_reuse_lease_and_keeps_cache_scope_shared(self):
        DecisionEndpoint.answer = routing.answer(jev.LUNA, "max", lease="user_turn")
        opening = [routing.message("user", "Complete this focused task.")]
        cheap = self.route(routing.payload_for(opening, model="auto-cheap"))
        continuation = opening + [routing.tool_call("c1"), routing.tool_step("c1", "OK")]
        leased = self.route(routing.payload_for(continuation, model="auto-cheap"))
        deep = self.route(routing.payload_for(continuation, model="auto-deep"))
        self.assertEqual(leased["decision_source"], "lease")
        self.assertEqual(deep["decision_source"], "clef")
        self.assertEqual(len(DecisionEndpoint.requests), 2)
        self.assertEqual(cheap["cache_scope"], deep["cache_scope"])
        self.assertEqual((cheap["effort"], deep["effort"]), ("max", "max"))
        self.assertNotEqual(jev._lease_contract_fingerprint({"_router_lease_mode": "auto-cheap"}),
                            jev._lease_contract_fingerprint({"_router_lease_mode": "auto-deep"}))

    def test_verified_checks_reconsider_leases_and_feed_actual_outcomes_to_clef(self):
        DecisionEndpoint.answer = routing.answer(jev.LUNA, "low", lease="user_turn")
        items = [routing.message("user", "Fix the failing unit test and verify it.")]
        self.route(routing.payload_for(items))
        initial = jev.feedback_for(jev.STATE).context("pck-thread-1")
        self.assertIsNone(initial["recent_verification"])
        self.assertEqual(sum(row["verified_passes"] for row in initial["route_statistics"]), 0)

        for number, code in enumerate((1, 1, 0), 1):
            call_id = f"check-{number}"
            items += [{"type": "function_call", "name": "exec_command", "call_id": call_id,
                       "arguments": json.dumps({"cmd": "python -m unittest"})},
                      {"type": "function_call_output", "call_id": call_id,
                       "output": json.dumps({"exit_code": code,
                                             "output": "FAILED (failures=1)" if code else "Ran 1 test\nOK"})}]
            DecisionEndpoint.answer = routing.answer(jev.SOL if code else jev.LUNA,
                                                     "max" if code else "low", lease="user_turn")
            record = self.route(routing.payload_for(items))
            self.assertEqual(record["decision_source"], "clef")
            self.assertEqual(record["verification"]["outcome"], "fail" if code else "pass")
            context = DecisionEndpoint.requests[-1][2]["state"]["effort_policy"]["feedback"]
            self.assertEqual(context["consecutive_failures"], number if code else 0)
            self.assertEqual(context["recent_verification"], record["verification"])
            self.assertEqual(sum(row["verified_failures"] for row in context["route_statistics"]), min(number, 2))
            self.assertEqual(sum(row["verified_passes"] for row in context["route_statistics"]), int(not code))
            self.assertEqual(record["effort"], "max" if code else "low")

        requests = len(DecisionEndpoint.requests)
        replay = self.route(routing.payload_for(items))
        self.assertIsNone(replay["verification"])
        self.assertEqual(replay["decision_source"], "lease")
        self.assertEqual(len(DecisionEndpoint.requests), requests)

    def test_scope_without_stable_cache_key_does_not_attribute_check_to_another_request(self):
        self.route(routing.payload_for([routing.message("user", "Check this task.")], cache_key=""))
        items = [routing.message("user", "Check this task."),
                 {"type": "function_call", "name": "exec_command", "call_id": "check",
                  "arguments": json.dumps({"cmd": "python -m unittest"})},
                 {"type": "function_call_output", "call_id": "check",
                  "output": json.dumps({"exit_code": 1, "output": "FAILED (failures=1)"})}]
        record = self.route(routing.payload_for(items, cache_key=""))
        self.assertIsNone(record["verification"])
        context = DecisionEndpoint.requests[-1][2]["state"]["effort_policy"]["feedback"]
        self.assertEqual(context["consecutive_failures"], 0)
        self.assertEqual(sum(row["verified_failures"] for row in context["route_statistics"]), 0)

    def test_fast_tool_result_waits_for_preceding_route_record(self):
        from contextlib import contextmanager
        store = jev.feedback_for(jev.STATE)
        original_record, original_handoff = store.record, store.handoff
        recording, release, next_handoff = threading.Event(), threading.Event(), threading.Event()
        finished = threading.Event()
        original_log = jev.log_line
        calls = 0

        def delayed_record(*args):
            recording.set()
            self.assertTrue(release.wait(3))
            return original_record(*args)

        def finished_log(entry):
            original_log(entry)
            if entry.get("verification"):
                finished.set()

        @contextmanager
        def observed_handoff(scope):
            nonlocal calls
            calls += 1
            if calls == 2:
                next_handoff.set()
            with original_handoff(scope):
                yield

        opening = [routing.message("user", "Check this unit test.")]
        continuation = opening + [
            {"type": "function_call", "name": "exec_command", "call_id": "fast-check",
             "arguments": json.dumps({"cmd": "python -m unittest"})},
            {"type": "function_call_output", "call_id": "fast-check",
             "output": json.dumps({"exit_code": 1, "output": "FAILED (failures=1)"})}]
        result = []
        with mock.patch.object(store, "record", side_effect=delayed_record), \
             mock.patch.object(store, "handoff", side_effect=observed_handoff), \
             mock.patch.object(jev, "log_line", side_effect=finished_log):
            self.assertEqual(self.request(body=routing.payload_for(opening))[0], 200)
            self.assertTrue(recording.wait(3))
            thread = threading.Thread(target=lambda: result.append(
                self.request(body=routing.payload_for(continuation))))
            thread.start()
            try:
                self.assertTrue(next_handoff.wait(3))
                self.assertEqual(result, [])
                self.assertEqual(len(DecisionEndpoint.requests), 1)
            finally:
                release.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result[0][0], 200)
            self.assertTrue(finished.wait(3))
        context = DecisionEndpoint.requests[-1][2]["state"]["effort_policy"]["feedback"]
        self.assertEqual(context["recent_verification"]["attributed_route"],
                         {"model": jev.LUNA, "effort": "low"})
        self.assertEqual(sum(row["verified_failures"] for row in context["route_statistics"]), 1)

    def test_disconnected_error_response_still_records_attempt_cost(self):
        store = jev.feedback_for(jev.STATE)
        handler = object.__new__(jev.Handler)
        handler.path = "/v1/responses"
        handler._body = lambda _: routing.payload_for([routing.message("user", "Continue.")])

        def disconnected(payload, config, path):
            handler._attempts.append({"model": jev.LUNA, "effort": "low", "usage": {"input_tokens": 7},
                                      "latency_ms": 12, "completion": "http_error"})
            raise BrokenPipeError("disconnected")

        handler._route_payload = disconnected
        with self.assertRaises(BrokenPipeError):
            handler._post()
        row = store.context(None)["route_statistics"][0]
        self.assertEqual(row["samples"], 1)
        self.assertEqual(row["token_totals"]["input_tokens"], 7)
        self.assertEqual(row["latency_ms_total"], 12)
        self.assertEqual(row["verified_passes"], 0)

    def test_leased_effort_and_baseline_paths_are_bounded_in_each_mode(self):
        for mode, profile in jev.CLEF_MODES.items():
            body = routing.payload_for([routing.message("user", "Continue.")], model=mode,
                                       reasoning={"effort": "max"})
            decision = jev.decision_from_answers(routing.answer(jev.SOL, "max")["answers"])
            with self.subTest(mode=mode, path="lease"), mock.patch.object(jev, "route_lease", return_value=decision):
                self.assertIn(self.route(body)["effort"], profile["efforts"])
            with self.subTest(mode=mode, path="error"):
                DecisionEndpoint.status = 503
                self.assertIn(self.route(body)["effort"], profile["efforts"])
                DecisionEndpoint.status = 200
            for sentinel in (jev.OFF_PATH, jev.SHADOW_PATH):
                with self.subTest(mode=mode, path=sentinel):
                    Path(sentinel).touch()
                    record = self.route(body)
                    Path(sentinel).unlink()
                    self.assertEqual(record["model"], jev.SOL)
                    self.assertIn(record["effort"], profile["efforts"])

    def test_empty_recovery_and_quota_retry_bound_every_attempt(self):
        for mode, profile in jev.CLEF_MODES.items():
            for failure in ("empty_completion", "quota"):
                with self.subTest(mode=mode, failure=failure):
                    served = []

                    def forward(handler, payload, path, stream, debug, marker, model, signature, effort,
                                exact_route=False):
                        served.append((model, effort, payload["reasoning"]["effort"], exact_route))
                        first = len(served) == 1
                        status = (429 if failure == "quota" else 502) if first else 200
                        handler._attempts.append({"model": model, "status": status,
                                                  "completion": failure if first else "response.completed",
                                                  "terminal_type": None if first else "response.completed"})
                        return status, "json", "application/json", first and failure == "quota", b"{}", None

                    DecisionEndpoint.answer = routing.answer(jev.LUNA, "max")
                    body = routing.payload_for([routing.message("user", "Continue.")], model=mode)
                    with mock.patch.object(jev.Handler, "_forward", new=forward), \
                         mock.patch.object(jev, "fallback_candidates", return_value=[jev.SOL]):
                        record = self.route(body)
                    self.assertEqual([model for model, _, _, _ in served], [jev.LUNA, jev.SOL])
                    self.assertTrue(all(effort in profile["efforts"] and base in profile["efforts"]
                                        for _, effort, base, _ in served))
                    self.assertTrue(all(exact_route for _, _, _, exact_route in served))
                    self.assertIn(record["effort"], profile["efforts"])

    def test_models_surface_and_health_describe_three_modes(self):
        status, raw = self.request("/v1/models")
        self.assertEqual(status, 200)
        entries = json.loads(raw)["data"]
        self.assertEqual({entry["id"] for entry in entries}, set(jev.CLEF_MODES))
        self.assertEqual({entry["name"] for entry in entries},
                         {"Auto Cheap (Clef)", "Auto Standard (Clef)", "Auto Deep (Clef)"})
        _, raw = self.request("/health")
        self.assertEqual(json.loads(raw)["auto_modes"]["auto"]["efforts"], list(jev.EFFORTS))
        self.assertEqual(json.loads(raw)["effort_policy"], "adaptive-v1")

    def test_historical_mandatory_astra_is_capped_to_sol_in_active_mode(self):
        DecisionEndpoint.answer = routing.answer(jev.LUNA, "high", astra_required=True)
        record = self.route()
        self.assertEqual((record["model"], record["effort"]), (jev.SOL, "high"))
        self.assertEqual(record["gate"], "clef_sol_ceiling")
        self.assertEqual(record["decision_source"], "clef")

    def test_historical_astra_lease_is_capped_without_a_new_decision(self):
        decision = jev.decision_from_answers(routing.answer(jev.ASTRA, "high")["answers"])
        with mock.patch.object(jev, "route_lease", return_value=decision):
            record = self.route()
        self.assertEqual(record["model"], jev.SOL)
        self.assertEqual(record["decision_source"], "lease")
        self.assertEqual(DecisionEndpoint.requests, [])

    def test_replayed_configuration_updates_cannot_reintroduce_astra(self):
        DecisionEndpoint.answer = routing.answer(jev.SOL, "high")
        reasoning_update = {"type": "configuration_update", "model": jev.ASTRA,
                            "reasoning": {"effort": "high"}}
        model_only_update = {"type": "configuration_update", "model": jev.ASTRA}
        body = routing.payload_for([reasoning_update, model_only_update,
                                    routing.message("user", "Continue this work.")])
        status, _ = self.request(body=body)
        self.assertEqual(status, 200)
        self.assertTrue(self.logged.wait(3))
        sent = routing.Edge.payloads[-1]
        self.assertEqual(sent["model"], jev.SOL)
        self.assertEqual(sent["input"][0], {"type": "configuration_update", "reasoning": {"effort": "high"}})
        self.assertEqual(len(sent["input"]), 2)
        self.assertNotIn(jev.ASTRA, json.dumps(sent))

    def test_empty_completion_retry_uses_sol_and_never_astra(self):
        served = []

        def forward(handler, payload, path, stream, debug, marker, model, signature, effort,
                    exact_route=False):
            served.append(model)
            handler._attempts.append({"model": model, "status": 502 if len(served) == 1 else 200,
                                      "completion": "empty_completion" if len(served) == 1 else "response.completed",
                                      "terminal_type": None if len(served) == 1 else "response.completed"})
            return (502 if len(served) == 1 else 200, "json", "application/json", False, b"{}", None)

        with mock.patch.object(jev.Handler, "_forward", new=forward):
            record = self.route()
        self.assertEqual(served, [jev.LUNA, jev.SOL])
        self.assertEqual(record["model"], jev.SOL)
        self.assertEqual(record["gate"], "empty_completion_retry")

    def test_quota_fallback_rejects_astra_and_opaque_aliases(self):
        with mock.patch.object(jev, "native_dry", return_value="manual"), \
             mock.patch.object(jev, "fallback_candidates", return_value=[jev.ASTRA, "provider/alias"]):
            status, _ = self.request()
        self.assertEqual(status, 503)
        self.assertTrue(self.logged.wait(3))
        self.assertEqual(self.records[-1]["attempts"], [])
        self.assertEqual(routing.Edge.payloads, [])

    def test_quota_retry_cannot_reintroduce_astra(self):
        served = []

        def forward(handler, payload, path, stream, debug, marker, model, signature, effort,
                    exact_route=False):
            served.append(model)
            handler._attempts.append({"model": model, "status": 429, "completion": "quota"})
            return 429, "json", "application/json", True, b"{}", None

        with mock.patch.object(jev.Handler, "_forward", new=forward), \
             mock.patch.object(jev, "fallback_candidates", return_value=[jev.ASTRA, "provider/alias"]):
            status, _ = self.request()
        self.assertEqual(status, 429)
        self.assertTrue(self.logged.wait(3))
        self.assertEqual(served, [jev.LUNA])

    def test_bad_schema_and_provider_error_use_sol_medium_without_hosted_retry(self):
        for status, answer in ((200, {"answers": {"model": {"choice": "invalid"}}}),
                               (503, {"error": "unavailable"})):
            with self.subTest(status=status):
                DecisionEndpoint.status = status
                DecisionEndpoint.answer = answer
                self.logged.clear()
                record = self.route()
                self.assertEqual((record["model"], record["effort"]), (jev.SOL, "medium"))
                self.assertEqual(record["decision_source"], "technical_fallback")
                self.assertTrue(record["gate"].startswith("clef_error:"))
        self.assertEqual(len(DecisionEndpoint.requests), 2)

    def test_timeout_uses_technical_baseline_without_hosted_retry(self):
        with mock.patch.object(jev.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = TimeoutError("fixture timeout")
            record = self.route()
        self.assertEqual(record["model"], jev.SOL)
        self.assertEqual(record["gate"], "clef_error:TimeoutError")

    def test_redirect_is_not_followed(self):
        DecisionEndpoint.status = 307
        DecisionEndpoint.redirect = f"http://127.0.0.1:{self.provider.server_address[1]}/leak"
        record = self.route()
        self.assertEqual(len(DecisionEndpoint.requests), 1)
        self.assertEqual(record["decision_source"], "technical_fallback")

    def test_missing_clef_key_never_uses_jev_credentials(self):
        with mock.patch.dict(os.environ, {"CLEF_API_KEY_FILE": "/missing/clef.key",
                                          "JEV_API_KEY": "hosted-fixture"}):
            record = self.route()
        self.assertEqual(record["model"], jev.SOL)
        self.assertEqual(record["decision_source"], "technical_fallback")
        self.assertEqual(DecisionEndpoint.requests, [])

    def test_shadow_serves_configured_sol_even_for_hypothetical_mandatory_astra(self):
        Path(jev.SHADOW_PATH).touch()
        DecisionEndpoint.answer = routing.answer(jev.LUNA, "high", astra_required=True)
        record = self.route()
        self.assertEqual(record["model"], jev.SOL)
        self.assertEqual(record["gate"], "shadow(sol)")
        self.assertEqual(record["would"]["model"], jev.SOL)
        self.assertEqual(record["decision_source"], "clef")

    def test_off_uses_configured_baseline_without_decision(self):
        Path(jev.OFF_PATH).touch()
        with mock.patch.dict(os.environ, {"JEV_BASELINE_MODEL": jev.ASTRA}):
            record = self.route()
        self.assertEqual(record["model"], jev.SOL)
        self.assertEqual(record["gate"], "off")
        self.assertEqual(DecisionEndpoint.requests, [])

    def test_ask_and_health_report_actual_provider_and_keep_caller_auth(self):
        status, raw = self.request("/ask", {"state": "fixture", "questions": jev.QUESTIONS})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["decision_provider"], "clef")
        status, raw = self.request("/health")
        health = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(health["decision_provider"], "clef")
        self.assertEqual(health["decision_model"], "clef-flash")
        self.assertEqual(health["baseline_model"], jev.SOL)
        self.assertEqual(health["allowed_execution_models"], [jev.LUNA, jev.SOL])
        self.assertTrue(health["decision_configured"])
        count = len(DecisionEndpoint.requests)
        status, _ = self.request("/ask", {}, authorization=None)
        self.assertEqual(status, 401)
        self.assertEqual(len(DecisionEndpoint.requests), count)

    def test_health_configuration_errors_do_not_expose_endpoint_auth(self):
        with mock.patch.dict(os.environ, {"CLEF_ENDPOINT": "http://private:secret@example.com/v1/systemone"}):
            status, raw = self.request("/health")
        self.assertEqual(status, 200)
        health = json.loads(raw)
        self.assertFalse(health["ok"])
        self.assertFalse(health["decision_configured"])
        self.assertNotIn("private", raw.decode())
        self.assertNotIn("secret", raw.decode())


if __name__ == "__main__":
    unittest.main()
