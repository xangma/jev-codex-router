"""Publish Clef mode metadata idempotently in an isolated user-model overlay."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ConfigureAutoModes(unittest.TestCase):
    def test_clef_modes_repeat_cleanly_and_jev_roundtrip_preserves_other_models(self):
        with tempfile.TemporaryDirectory() as directory:
            models_path = Path(directory, "user-models.json")
            other = {"slug": "other/model", "provider": "other", "displayName": "Keep me",
                     "reasoningLevels": [{"effort": "low", "description": "User setting"}]}
            models_path.write_text(json.dumps({"version": 1, "models": [other]}))
            env = {**os.environ, "CODEX_ROUTER_STATE_DIR": directory,
                   "MODEL_ROUTER_USER_MODELS": str(models_path), "JEV_DECISION_PROVIDER": "clef"}
            command = ["node", str(ROOT / "server/configure-model.mjs")]
            subprocess.run(command, env=env, check=True, capture_output=True)
            first = models_path.read_bytes()
            subprocess.run(command, env=env, check=True, capture_output=True)
            self.assertEqual(models_path.read_bytes(), first)
            models = json.loads(first)["models"]
            self.assertEqual(models[0], other)
            efforts = ["low", "medium", "high", "xhigh", "max"]
            profiles = {
                "jev/auto-cheap": ("auto-cheap", "Auto Cheap (Clef)", "low"),
                "jev/auto": ("auto", "Auto Standard (Clef)", "medium"),
                "jev/auto-deep": ("auto-deep", "Auto Deep (Clef)", "high"),
            }
            self.assertEqual(len(models), 4)
            for entry in models[1:]:
                upstream, name, default_effort = profiles[entry["slug"]]
                self.assertEqual(entry["upstreamModel"], upstream)
                self.assertEqual(entry["displayName"], name)
                self.assertEqual([value["effort"] for value in entry["reasoningLevels"]], efforts)
                self.assertEqual(entry["defaultEffort"], default_effort)
                self.assertNotIn("Astra", entry["description"])
                self.assertNotIn("through", entry["description"])
            self.assertEqual(models_path.stat().st_mode & 0o777, 0o600)
            env.pop("JEV_DECISION_PROVIDER")
            subprocess.run(command, env=env, check=True, capture_output=True)
            models = json.loads(models_path.read_text())["models"]
            self.assertEqual(models[0], other)
            self.assertEqual([entry["slug"] for entry in models], ["other/model", "jev/auto"])
            self.assertEqual(models[1]["displayName"], "Auto (Jev)")
            self.assertEqual([value["effort"] for value in models[1]["reasoningLevels"]],
                             efforts)
            env["JEV_DECISION_PROVIDER"] = "clef"
            subprocess.run(command, env=env, check=True, capture_output=True)
            self.assertEqual(models_path.read_bytes(), first)


if __name__ == "__main__":
    unittest.main()
