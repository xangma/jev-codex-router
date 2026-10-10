import json
import unittest
from unittest import mock

import clef_route as route

CANDIDATES = [
    {"model": "haiku", "effort": None, "description": "Fast routine work"},
    {"model": "sonnet", "effort": "low", "description": "Complex coding"},
    {"model": "sonnet", "effort": "high", "description": "Complex coding"},
]


class NativeDecision(unittest.TestCase):
    def test_modes_keep_every_native_route_eligible_and_joint(self):
        bodies = [route.decision_body("fixture", CANDIDATES, mode)[0] for mode in route.MODES]
        self.assertEqual(len({b["questions"]["route"]["instructions"] for b in bodies}), 3)
        for body in bodies:
            self.assertEqual(body["required_decision_provider"], "clef")
            self.assertEqual(len(body["questions"]), 1)
            self.assertEqual(len(body["questions"]["route"]["criteria"]), 3)

    def test_context_is_bounded_without_truncating_native_prompt(self):
        task = "start " + "a" * 10000 + " end"
        body, _ = route.decision_body(task, CANDIDATES, context={"previous": "b" * 12000})
        self.assertLess(len(json.dumps(body["state"])), 10000)
        self.assertTrue(body["state"]["task"].endswith(" end"))
        self.assertEqual(len(task), 10010)

    def test_invalid_candidates_and_modes_never_make_a_request(self):
        for candidates in ([], [{"model": "x", "effort": "bogus"}], [{"model": "", "effort": None}]):
            with self.assertRaises(route.RoutingError):
                route.decision_body("fixture", candidates)
        with self.assertRaises(route.RoutingError):
            route.decision_body("fixture", CANDIDATES, "unknown")

    def test_choice_cannot_escape_verified_inventory_and_proxy_redirects_disabled(self):
        answer = {"decision_provider": "clef", "answers": {"route": {"choice": "route_00"}},
                  "usage": {"input_tokens": 123}, "decision_cache": {"hit": False}}
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(answer).encode()
        opener = mock.Mock(); opener.open.return_value = response
        with mock.patch.object(route, "local_secret", return_value="fixture-private"), \
             mock.patch.object(route.urllib.request, "build_opener", return_value=opener) as build:
            chosen = route.decide("fixture", CANDIDATES)
            self.assertEqual((chosen["model"], chosen["effort"]), ("haiku", None))
            self.assertEqual(build.call_args.args[0].proxies, {})
            self.assertIsInstance(build.call_args.args[1], route.NoRedirect)
            for override in ({"decision_provider": "jev"}, {"answers": {"route": {"choice": "external"}}}):
                response.__enter__.return_value.read.return_value = json.dumps({**answer, **override}).encode()
                with self.assertRaises(route.RoutingError):
                    route.decide("fixture", CANDIDATES)

    def test_missing_credentials_prevent_inference(self):
        with mock.patch.object(route, "local_secret", return_value=""), \
             mock.patch.object(route.urllib.request, "build_opener") as opener:
            with self.assertRaises(route.RoutingError):
                route.decide("fixture", CANDIDATES)
            opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
