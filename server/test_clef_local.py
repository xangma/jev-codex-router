"""Local/Sol policy, portable replay, cache isolation and bounded recovery."""
import copy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jev_server as jev
from local_executor import validate_executor, write_executor
import test_decision_provider as provider
import test_per_call_routing as routing

EXECUTOR = validate_executor({"model": "local/qwen-example", "name": "Qwen example",
                              "context_window": 131072})


def answer(choice, lease="one_call"):
    return {"answers": {"route": {"choice": choice, "confidence": 0.4},
                        "lease": {"choice": lease, "confidence": 0.4}}}


class LocalPolicy(unittest.TestCase):
    def test_joint_choices_replace_luna_and_honor_sol_bounds(self):
        bounds = {"luna": {"min": "medium", "max": "max"},
                  "sol": {"min": "medium", "max": "high"}}
        choices = jev.clef_questions("auto-local", "medium", bounds, EXECUTOR)["route"]["criteria"]
        self.assertEqual(set(choices), {"local_medium", "local_high", "sol_medium"})
        decision = jev.provider_decision(answer("local_high")["answers"], "clef",
                                         "auto-local", "medium", bounds, EXECUTOR)
        self.assertEqual((decision["model"], decision["effort"]), (EXECUTOR["model"], "high"))
        for choice in ("luna_medium", "astra_medium", "sol_high"):
            with self.subTest(choice=choice), self.assertRaises(ValueError):
                jev.provider_decision(answer(choice)["answers"], "clef", "auto-local",
                                     "medium", bounds, EXECUTOR)
        self.assertEqual(jev.clef_route(jev.ASTRA, "max", "auto-local", "medium", bounds, EXECUTOR),
                         (jev.SOL, "medium"))
        self.assertEqual(jev.clef_route(jev.LUNA, "low", "auto-local", "low", bounds, EXECUTOR),
                         (EXECUTOR["model"], "medium"))

    def test_unoffered_historical_choices_cannot_enter_local_profile(self):
        with self.assertRaises(ValueError):
            jev.provider_decision(routing.answer(jev.LUNA, "high")["answers"], "clef",
                                 "auto-local", "high", executor=EXECUTOR)
        with self.assertRaises(ValueError):
            jev.clef_route("other/arbitrary", "high", "auto-local", executor=EXECUTOR)

    def test_measured_local_cache_does_not_leak_into_native_inventory(self):
        payload = routing.payload_for([routing.message("user", "A fact")])
        scope = jev.cache_scope(payload, "A fact")
        with jev._cache_affinity_lock:
            jev._cache_affinity.clear()
        jev.remember_cache_model(scope, payload, EXECUTOR["model"], 200,
            usage={"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 800}},
            now=100, permitted_models=(EXECUTOR["model"], jev.SOL))
        self.assertIsNone(jev.cache_affinity(scope, payload, now=101))
        local = jev.cache_affinity(scope, payload, now=101, permitted_models=(EXECUTOR["model"], jev.SOL))
        self.assertEqual(local["models"]["qwen-example"]["state"], "hot")
        self.assertEqual(local["models"]["qwen-example"]["read_pct"], 80)
        changed = {**payload, "instructions": "Changed instruction"}
        self.assertEqual(jev.cache_affinity(scope, changed, now=101,
            permitted_models=(EXECUTOR["model"], jev.SOL))["models"]["qwen-example"]["state"], "stale_prefix")
        jev.remember_cache_model(scope, payload, EXECUTOR["model"], 200, now=102,
                                permitted_models=(EXECUTOR["model"], jev.SOL))
        self.assertEqual(jev.cache_affinity(scope, payload, now=103,
            permitted_models=(EXECUTOR["model"], jev.SOL))["models"]["qwen-example"]["state"], "unknown")


class LocalWorkflow(unittest.TestCase):
    setUpClass = classmethod(provider.ClefRoutingWorkflow.setUpClass.__func__)
    tearDownClass = classmethod(provider.ClefRoutingWorkflow.tearDownClass.__func__)
    tearDown = provider.ClefRoutingWorkflow.tearDown
    request = provider.ClefRoutingWorkflow.request
    route = provider.ClefRoutingWorkflow.route
    write_bounds = provider.ClefRoutingWorkflow.write_bounds

    def setUp(self):
        provider.ClefRoutingWorkflow.setUp(self)
        self.executor_path = Path(jev.STATE, "clef-local-executor.json")
        write_executor(EXECUTOR, self.executor_path)
        self.providers_path = Path(jev.STATE, "generic-providers.json")
        self.providers_path.write_text(json.dumps({"version": 1, "providers": [{
            "id": "local", "baseUrl": "http://127.0.0.1:8999/v1", "adapter": "openai-chat", "enabled": True}]}))
        self.models_path = Path(jev.STATE, "user-models.json")
        self.models_path.write_text(json.dumps({"models": [
            {"slug": "local/qwen-example", "provider": "local", "upstreamModel": "qwen-example",
             "contextWindow": 131072, "reasoningLevels": [{"effort": "medium"}, {"effort": "high"}]},
            {"slug": "local/qwen-next", "provider": "local", "upstreamModel": "qwen-next",
             "contextWindow": 131072, "reasoningLevels": [{"effort": "medium"}, {"effort": "high"}]}]}))
        self.enterContext(mock.patch.dict(os.environ, {"CLEF_LOCAL_EXECUTOR_FILE": str(self.executor_path),
            "MODEL_ROUTER_GENERIC_PROVIDERS": str(self.providers_path),
            "MODEL_ROUTER_USER_MODELS": str(self.models_path)}))
        provider.DecisionEndpoint.answer = answer("local_medium")

    def payload(self, items=None, **extra):
        return routing.payload_for(items or [routing.message("user", "Explain the supplied fact")],
                                   model="auto-local", reasoning={"effort": "high"}, **extra)

    def test_model_list_and_health_advertise_both_profiles_only_when_local_is_ready(self):
        status, body = self.request(path="/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual([(row["id"], row["name"]) for row in json.loads(body)["data"]],
                         [("auto", "Auto (Clef)"), ("auto-local", "Auto (Clef Local)")])
        status, body = self.request(path="/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["local_executor_configured"])
        self.executor_path.unlink()
        status, body = self.request(path="/v1/models")
        self.assertEqual([row["id"] for row in json.loads(body)["data"]], ["auto"])

    def test_registry_rejects_native_alias_missing_model_efforts_and_oversized_context(self):
        original = json.loads(self.models_path.read_text())
        variants = []
        for field, value in (("upstreamModel", jev.ASTRA), ("upstreamModel", "openai/" + jev.SOL),
                             ("reasoningLevels", [{"effort": "high"}]), ("contextWindow", 32768),
                             ("provider", "other")):
            document = copy.deepcopy(original)
            document["models"][0][field] = value
            variants.append(document)
        variants.append({"models": []})
        for value in variants:
            with self.subTest(value=value):
                self.models_path.write_text(json.dumps(value))
                status, body = self.request(body=self.payload())
                self.assertEqual(status, 400)
                self.assertNotIn(jev.ASTRA, body.decode())
                status, body = self.request(path="/health")
                self.assertEqual(status, 200)
                self.assertTrue(json.loads(body)["ok"])
                self.assertFalse(json.loads(body)["local_executor_configured"])
        self.assertEqual(provider.DecisionEndpoint.requests, [])

    def test_local_replay_keeps_history_tools_and_cache_key_with_request_effort(self):
        history = [routing.message("system", "Preserve this context"),
                   routing.message("user", "Run the tool and explain its output"),
                   routing.tool_call("c1"), routing.tool_step("c1", "Verified output")]
        body = self.payload(history, include=["reasoning.encrypted_content"])
        record = self.route(body)
        sent = routing.Edge.payloads[-1]
        self.assertEqual(sent["model"], EXECUTOR["model"])
        self.assertEqual(sent["input"], history)
        self.assertEqual(sent["tools"], body["tools"])
        self.assertEqual(sent["prompt_cache_key"], body["prompt_cache_key"])
        self.assertEqual(sent["reasoning"]["effort"], "medium")
        self.assertEqual(record["effort_transport"], "request")
        self.assertNotIn("include", sent)
        self.assertEqual(routing.Edge.headers_seen[-1]["x-codex-router-canonical-replay"], "1")
        self.assertEqual(routing.Edge.headers_seen[-1]["x-codex-router-exact-route"], "1")
        self.assertEqual(record["decision_source"], "clef")

    def test_local_inline_native_controls_cannot_change_selected_route(self):
        body = self.payload([{"type": "configuration_update", "model": jev.ASTRA,
                              "reasoning": {"effort": "max"}},
                             routing.message("user", "Explain the supplied fact")])
        self.route(body)
        sent = routing.Edge.payloads[-1]
        self.assertEqual(sent["model"], EXECUTOR["model"])
        self.assertEqual(sent["input"], [body["input"][-1]])
        self.assertEqual(sent["reasoning"]["effort"], "medium")

    def test_missing_or_invalid_local_settings_do_not_break_native_auto(self):
        for value in (None, "{broken"):
            if value is None:
                self.executor_path.unlink()
            else:
                self.executor_path.write_text(value)
            status, data = self.request(body=self.payload())
            self.assertEqual(status, 400)
            self.assertIn("local executor", data.decode().lower())
            provider.DecisionEndpoint.answer = provider.clef_answer(jev.LUNA, "medium")
            record = self.route(routing.payload_for([routing.message("user", "A native fact")]))
            self.assertEqual(record["model"], jev.LUNA)
        self.assertEqual(len(provider.DecisionEndpoint.requests), 2)

    def test_same_clef_serving_endpoint_and_disabled_provider_are_rejected(self):
        for base_url, enabled in ((os.environ["CLEF_ENDPOINT"].replace("/systemone", ""), True),
                                  ("http://127.0.0.1:8999/v1", False)):
            self.providers_path.write_text(json.dumps({"providers": [{"id": "local", "baseUrl": base_url,
                "adapter": "openai-chat", "enabled": enabled}]}))
            status, data = self.request(body=self.payload())
            self.assertEqual(status, 400)
            self.assertNotIn(base_url, data.decode())
        self.assertEqual(provider.DecisionEndpoint.requests, [])

    def test_local_sol_local_switch_replays_complete_history_and_measured_cache(self):
        history = [routing.message("user", "A simple fact")]
        self.route(self.payload(history))
        history += [routing.message("assistant", "The fact"), routing.message("user", "Investigate a new difficult question")]
        provider.DecisionEndpoint.answer = answer("sol_high")
        self.route(self.payload(history))
        history += [routing.message("assistant", "Diagnosis"), routing.message("user", "Summarize the established answer")]
        provider.DecisionEndpoint.answer = answer("local_medium")
        self.route(self.payload(history))
        self.assertEqual([item["model"] for item in routing.Edge.payloads],
                         [EXECUTOR["model"], jev.SOL, EXECUTOR["model"]])
        self.assertEqual(routing.Edge.payloads[-1]["input"], history)
        state = provider.DecisionEndpoint.requests[-1][2]["state"]["cache_state"]
        self.assertEqual(set(state["models"]), {"qwen-example", "sol"})
        self.assertEqual(state["models"]["qwen-example"]["state"], "hot")

    def test_repeated_local_effort_reuses_canonical_evidence_despite_sol_cap_and_include(self):
        opening = [routing.message("user", "A supplied fact")]
        self.route(self.payload(opening, include=["reasoning.encrypted_content"]))
        history = opening + [routing.message("assistant", "The fact"),
                             routing.message("user", "Summarize that fact")]
        self.route(self.payload(history, include=["reasoning.encrypted_content"]))
        cache = provider.DecisionEndpoint.requests[-1][2]["state"]["cache_state"]["models"]["qwen-example"]
        self.assertEqual(cache["state"], "hot")
        self.assertEqual(cache["prefix"], "matched")
        self.assertEqual(cache["effort"], "medium")
        self.assertEqual(cache["reuse_requires_effort"], "medium")
        self.assertTrue(all(payload["reasoning"]["effort"] == "medium" for payload in routing.Edge.payloads))
        self.assertTrue(all("include" not in payload for payload in routing.Edge.payloads))

    def test_clean_lease_invalidates_after_executor_config_change(self):
        opening = [routing.message("user", "Read matching files")]
        provider.DecisionEndpoint.answer = answer("local_medium", "user_turn")
        self.route(self.payload(opening))
        continued = opening + [routing.tool_call("c1"), routing.tool_step("c1", "exit 0")]
        self.assertTrue(self.route(self.payload(continued))["lease_hit"])
        replacement = {**EXECUTOR, "model": "local/qwen-next"}
        write_executor(replacement, self.executor_path)
        record = self.route(self.payload(continued))
        self.assertFalse(record["lease_hit"])
        self.assertEqual(record["model"], replacement["model"])
        self.assertEqual(len(provider.DecisionEndpoint.requests), 2)

    def test_local_transport_change_invalidates_lease_and_old_cache_evidence(self):
        opening = [routing.message("user", "Read matching files")]
        provider.DecisionEndpoint.answer = answer("local_medium", "user_turn")
        self.route(self.payload(opening))
        document = json.loads(self.providers_path.read_text())
        document["providers"][0]["baseUrl"] = "http://127.0.0.1:8998/v1"
        self.providers_path.write_text(json.dumps(document))
        continued = opening + [routing.tool_call("c1"), routing.tool_step("c1", "exit 0")]
        record = self.route(self.payload(continued))
        self.assertFalse(record["lease_hit"])
        state = provider.DecisionEndpoint.requests[-1][2]["state"]["cache_state"]
        self.assertEqual(state["models"]["qwen-example"]["state"], "stale_prefix")
        self.assertIsNone(state["models"]["qwen-example"]["read_pct"])

    def test_context_or_hosted_search_excludes_local_without_truncating(self):
        provider.DecisionEndpoint.answer = answer("sol_medium")
        for extra in ({"tools": [{"type": "web_search"}]},
                      {"previous_response_id": "resp_private"},
                      {"max_output_tokens": 200000}):
            body = self.payload(**extra)
            record = self.route(body)
            self.assertEqual(record["model"], jev.SOL)
            self.assertFalse(record["local_eligible"])
            self.assertIsNotNone(record["local_unavailable_reason"])
            choices = provider.DecisionEndpoint.requests[-1][2]["questions"]["route"]["criteria"]
            self.assertTrue(all(choice.startswith("sol_") for choice in choices))
            self.assertTrue(provider.DecisionEndpoint.requests[-1][2]["state"]["effort_policy"]["local_unavailable_reason"])
        self.write_bounds(sol_min="max")
        status, _ = self.request(body=self.payload(max_output_tokens=200000))
        self.assertEqual(status, 400)

    def test_local_preoutput_failure_retries_sol_without_marking_native_dry(self):
        original = jev.Handler._forward
        served = []
        def forward(handler, payload, path, stream, debug, marker, model, signature, effort, exact_route=False):
            served.append((model, effort, copy.deepcopy(payload)))
            if len(served) == 1:
                handler._attempts.append({"model": model, "status": 429, "completion": "http_error"})
                return 429, "json", "application/json", True, b"{}", None
            return original(handler, payload, path, stream, debug, marker, model, signature, effort, exact_route)
        with mock.patch.object(jev.Handler, "_forward", new=forward), \
             mock.patch.object(jev, "mark_native_dry") as dry:
            record = self.route(self.payload(include=["reasoning.encrypted_content"]))
        self.assertEqual([item[:2] for item in served], [(EXECUTOR["model"], "medium"), (jev.SOL, "medium")])
        self.assertEqual(served[-1][2]["include"], ["reasoning.encrypted_content"])
        self.assertEqual(record["gate"], "local_executor_retry")
        dry.assert_not_called()

    def test_sol_quota_retries_only_configured_local_with_full_replay(self):
        provider.DecisionEndpoint.answer = answer("sol_high")
        original = jev.Handler._forward
        served = []
        def forward(handler, payload, path, stream, debug, marker, model, signature, effort, exact_route=False):
            served.append(model)
            if len(served) == 1:
                handler._attempts.append({"model": model, "status": 429, "completion": "http_error"})
                return 429, "json", "application/json", True, b"{}", None
            return original(handler, payload, path, stream, debug, marker, model, signature, effort, exact_route)
        with mock.patch.object(jev.Handler, "_forward", new=forward), \
             mock.patch.object(jev, "fallback_candidates") as discovered:
            record = self.route(self.payload())
        self.assertEqual(served, [jev.SOL, EXECUTOR["model"]])
        self.assertTrue(record["retried"])
        self.assertEqual(record["decision_source"], "clef")
        discovered.assert_not_called()

    def test_local_failed_terminal_before_output_retries_sol_with_clean_stream(self):
        failed = (b'data: {"type":"response.created","response":{"id":"resp_failed"}}\n\n'
                  b'data: {"type":"response.failed","response":{"id":"resp_failed","status":"failed",'
                  b'"error":{"code":"server_error","message":"Private upstream details"}}}\n\n')
        def edge(handler):
            body = json.loads(handler.rfile.read(int(handler.headers["Content-Length"])))
            routing.Edge.payloads.append(body)
            data = failed if body["model"] == EXECUTOR["model"] else routing.COMPLETED
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
        with mock.patch.object(routing.Edge, "do_POST", new=edge):
            status, data = self.request(body=self.payload())
        self.assertEqual(status, 200)
        self.assertTrue(self.logged.wait(3))
        self.assertEqual([body["model"] for body in routing.Edge.payloads], [EXECUTOR["model"], jev.SOL])
        self.assertNotIn(b"resp_failed", data)
        self.assertNotIn(b"Private upstream details", data)
        self.assertEqual(self.records[-1]["gate"], "local_executor_retry")
        self.assertEqual(self.records[-1]["outcome_status"], 200)

    def test_off_and_shadow_without_eligible_sol_use_local_never_luna(self):
        self.write_bounds(sol_min="max")
        for switch in (jev.OFF_PATH, jev.SHADOW_PATH):
            Path(switch).touch()
            record = self.route(self.payload())
            self.assertEqual(record["model"], EXECUTOR["model"])
            Path(switch).unlink()
        self.assertTrue(all(payload["model"] == EXECUTOR["model"] for payload in routing.Edge.payloads))

    def test_streamed_output_prevents_retry_even_when_local_transport_breaks(self):
        exposed = (b'data: {"type":"response.created","response":{"id":"resp_local"}}\n\n'
                   b'data: {"type":"response.output_text.delta","delta":"Visible answer"}\n\n')
        def edge(handler):
            body = json.loads(handler.rfile.read(int(handler.headers["Content-Length"])))
            routing.Edge.payloads.append(body)
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Content-Length", str(len(exposed)))
            handler.end_headers()
            handler.wfile.write(exposed)
        with mock.patch.object(routing.Edge, "do_POST", new=edge):
            status, data = self.request(body=self.payload())
        self.assertEqual(status, 200)
        self.assertTrue(self.logged.wait(3))
        self.assertIn(b"Visible answer", data)
        self.assertEqual(len(routing.Edge.payloads), 1)
        self.assertEqual(self.records[-1]["outcome_status"], 502)
        self.assertFalse(self.records[-1]["retried"])


if __name__ == "__main__":
    unittest.main()
