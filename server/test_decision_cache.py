import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import unittest

from decision_cache import DecisionCache, model_version

PAYLOAD = {"model": "fixture", "state": {"task": "private fixture", "feedback": [1, 2]},
           "questions": {"model": {"criteria": {"luna": "routine", "sol": "difficult"}}}}
RESULT = {"model": "fixture", "answers": {"model": {"choice": "luna", "confidence": 0.9}},
          "usage": {"input_tokens": 700, "output_tokens": 0}}


class DecisionResultCache(unittest.TestCase):
    def setUp(self):
        self.now = 10
        self.cache = DecisionCache(ttl=60, capacity=2, clock=lambda: self.now)
        self.calls = 0

    def loader(self, remaining):
        self.calls += 1
        self.assertGreater(remaining, 0)
        return copy.deepcopy(RESULT)

    def call(self, payload=None, endpoint="local", key="credential", version=(1,)):
        return self.cache.call(endpoint, key, payload or PAYLOAD, self.loader, 30, version)

    def test_exact_repeat_avoids_inference_and_returns_independent_result(self):
        first = self.call()
        first["answers"]["model"]["choice"] = "sol"
        second = self.call()
        self.assertEqual(self.calls, 1)
        self.assertEqual(second["answers"], RESULT["answers"])
        self.assertEqual(second["usage"], {"input_tokens": 0, "output_tokens": 0})
        self.assertEqual(second["decision_cache"],
                         {"kind": "exact_result", "hit": True, "saved_input_tokens": 700})
        self.assertEqual(self.cache.statistics()["hits"], 1)
        self.assertNotIn("private fixture", repr(self.cache._entries))
        self.assertNotIn("credential", repr(self.cache._entries))

    def test_task_feedback_questions_model_endpoint_credentials_and_revision_invalidate(self):
        self.call()
        variants = []
        for change in (lambda p: p["state"].update(task="different"),
                       lambda p: p["state"].update(feedback=[2, 1]),
                       lambda p: p.update(model="different"),
                       lambda p: p["questions"]["model"].update(instructions="different")):
            payload = copy.deepcopy(PAYLOAD); change(payload); variants.append(payload)
        for payload in variants:
            self.assertFalse(self.call(payload)["decision_cache"]["hit"])
        for overrides in ({"endpoint": "another"}, {"key": "rotated"}, {"version": (2,)}):
            self.assertFalse(self.call(**overrides)["decision_cache"]["hit"])
        self.assertEqual(self.calls, 8)
        self.assertEqual(self.cache.statistics()["entries"], 2)

    def test_state_object_and_array_order_require_inference(self):
        self.call()
        reordered = copy.deepcopy(PAYLOAD)
        reordered["state"] = dict(reversed(list(reordered["state"].items())))
        self.assertFalse(self.call(reordered)["decision_cache"]["hit"])
        reordered = copy.deepcopy(PAYLOAD); reordered["state"]["feedback"].reverse()
        self.assertFalse(self.call(reordered)["decision_cache"]["hit"])

    def test_question_order_cannot_reuse_a_different_native_prompt(self):
        payload = copy.deepcopy(PAYLOAD)
        payload["questions"]["second"] = copy.deepcopy(payload["questions"]["model"])
        result = copy.deepcopy(RESULT)
        result["answers"]["second"] = copy.deepcopy(result["answers"]["model"])
        calls = []
        def loader(_):
            calls.append(1)
            return result
        first = self.cache.call("local", "key", payload, loader, 30, (1,))
        payload["questions"] = dict(reversed(list(payload["questions"].items())))
        second = self.cache.call("local", "key", payload, loader, 30, (1,))
        self.assertFalse(first["decision_cache"]["hit"])
        self.assertFalse(second["decision_cache"]["hit"])
        self.assertEqual(len(calls), 2)

    def test_ttl_expiry_and_unknown_model_revision_require_inference(self):
        self.call(); self.now += 60
        self.assertFalse(self.call()["decision_cache"]["hit"])
        for _ in range(2):
            self.assertNotIn("decision_cache", self.call(version=None))
        self.assertEqual(self.calls, 4)

    def test_provider_errors_and_invalid_answers_are_never_cached(self):
        def failed(_):
            raise OSError("fixture unavailable")
        for _ in range(2):
            with self.assertRaises(OSError):
                self.cache.call("local", "key", PAYLOAD, failed, 1, (1,))
        invalid = ({}, {"answers": {}}, {"answers": {"model": {"choice": []}}},
                   {**RESULT, "usage": "invalid"}, {**RESULT, "confidence": float("nan")})
        for result in invalid:
            for _ in range(2):
                self.assertNotIn("decision_cache", self.cache.call("local", "key", PAYLOAD,
                    lambda _: copy.deepcopy(result), 1, (1,)))
        self.assertEqual(self.cache.statistics()["entries"], 0)

    def test_identical_concurrent_requests_share_one_inference(self):
        cache = DecisionCache()
        started, release = threading.Event(), threading.Event()
        calls = []
        def loader(_):
            calls.append(1); started.set()
            self.assertTrue(release.wait(2))
            return copy.deepcopy(RESULT)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(cache.call, "local", "key", PAYLOAD, loader, 2, (1,))
            self.assertTrue(started.wait(1))
            second = pool.submit(cache.call, "local", "key", PAYLOAD, loader, 2, (1,))
            release.set()
            answers = [first.result(), second.result()]
        self.assertEqual(len(calls), 1)
        self.assertEqual(sum(answer["decision_cache"]["hit"] for answer in answers), 1)

    def test_singleflight_wait_respects_each_callers_timeout(self):
        cache = DecisionCache()
        started, release = threading.Event(), threading.Event()
        def loader(_):
            started.set(); release.wait(2)
            return copy.deepcopy(RESULT)
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(cache.call, "local", "key", PAYLOAD, loader, 2, (1,))
            self.assertTrue(started.wait(1))
            try:
                with self.assertRaises(TimeoutError):
                    cache.call("local", "key", PAYLOAD, loader, 0.01, (1,))
            finally:
                release.set()
            first.result()

    def test_managed_preset_and_weight_replacements_change_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory, "model.gguf"); model.write_bytes(b"fixture")
            preset = Path(directory, "models.ini")
            preset.write_text(f"version = 1\n\n[*]\nsleep-idle-seconds = 300\n\n[fixture]\nmodel = {model}\n")
            before = model_version("fixture", preset)
            self.assertIsNotNone(before)
            model.write_bytes(b"changed fixture")
            self.assertNotEqual(model_version("fixture", preset), before)
            self.assertIsNone(model_version("unknown", preset))
            model.unlink()
            self.assertIsNone(model_version("fixture", preset))


if __name__ == "__main__":
    unittest.main()
