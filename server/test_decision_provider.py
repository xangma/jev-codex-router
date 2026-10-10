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

    def route(self):
        status, _ = self.request()
        self.assertEqual(status, 200)
        self.assertTrue(self.logged.wait(3))
        return self.records[-1]

    def test_local_body_auth_and_choice_decode_use_the_four_existing_questions(self):
        record = self.route()
        path, headers, body = DecisionEndpoint.requests[0]
        self.assertEqual(path, "/v1/systemone")
        self.assertEqual(headers["Authorization"], "Bearer local-fixture")
        self.assertEqual(body["model"], "clef-flash")
        self.assertEqual(body["questions"], jev.QUESTIONS)
        self.assertEqual(set(body["questions"]), {"model", "effort", "astra_policy", "lease"})
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

    def test_mandatory_astra_overrides_capability_choice_in_active_mode(self):
        DecisionEndpoint.answer = routing.answer(jev.LUNA, "high", astra_required=True)
        record = self.route()
        self.assertEqual((record["model"], record["effort"]), (jev.ASTRA, "high"))
        self.assertEqual(record["decision_source"], "clef")

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
        self.assertEqual(record["would"]["model"], jev.ASTRA)
        self.assertEqual(record["decision_source"], "clef")

    def test_off_uses_configured_baseline_without_decision(self):
        Path(jev.OFF_PATH).touch()
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
