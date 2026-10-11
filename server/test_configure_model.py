"""Publish the enabled Clef pickers and preserve hidden legacy routes."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ConfigureAutoModes(unittest.TestCase):
    def test_local_mode_config_reload_disable_and_jev_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory, "executor.json")
            models_path = Path(directory, "user-models.json")
            config_path.write_text(json.dumps({"model": "my-llama/qwen-model",
                                               "context_window": 131072}))
            env = {**os.environ, "CODEX_ROUTER_STATE_DIR": directory,
                   "MODEL_ROUTER_USER_MODELS": str(models_path), "JEV_DECISION_PROVIDER": "clef",
                   "CLEF_LOCAL_EXECUTOR_FILE": str(config_path)}
            command = ["node", str(ROOT / "server/configure-model.mjs")]
            result = subprocess.run(command, env=env, check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(result.stdout)["configuredModels"], ["jev/auto", "jev/auto-local"])
            first = models_path.read_bytes()
            subprocess.run(command, env=env, check=True, capture_output=True)
            self.assertEqual(models_path.read_bytes(), first)
            models = json.loads(first)["models"]
            local = next(model for model in models if model["slug"] == "jev/auto-local")
            self.assertEqual(local["displayName"], "Auto (Clef Local)")
            self.assertEqual(local["upstreamModel"], "auto-local")
            self.assertEqual(local["contextWindow"], 131072)
            self.assertEqual(local["autoCompact"], int(131072 * .85))
            self.assertEqual(local["inputModalities"], ["text"])
            self.assertNotIn("searchTool", local)
            self.assertFalse(local["supportsSearchHistory"])
            native = next(model for model in models if model["slug"] == "jev/auto")
            self.assertEqual(native["searchTool"], {"mode": "hosted"})
            self.assertEqual([entry["effort"] for entry in local["reasoningLevels"]],
                             ["low", "medium", "high", "xhigh", "max"])
            self.assertTrue(all("Maximum Sol effort" in entry["description"]
                                for entry in local["reasoningLevels"]))
            config_path.write_text("{broken")
            failed = subprocess.run(command, env=env, capture_output=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(models_path.read_bytes(), first)
            config_path.unlink()
            subprocess.run(command, env=env, check=True, capture_output=True)
            models = json.loads(models_path.read_text())["models"]
            self.assertNotIn("jev/auto-local", [model["slug"] for model in models])
            config_path.write_text(json.dumps({"model": "my-llama/qwen-model"}))
            env["JEV_DECISION_PROVIDER"] = "jev"
            subprocess.run(command, env=env, check=True, capture_output=True)
            models = json.loads(models_path.read_text())["models"]
            self.assertEqual([model["slug"] for model in models], ["jev/auto"])

    def test_clef_modes_repeat_cleanly_and_jev_roundtrip_preserves_other_models(self):
        with tempfile.TemporaryDirectory() as directory:
            models_path = Path(directory, "user-models.json")
            other = {"slug": "other/model", "provider": "other", "displayName": "Keep me",
                     "reasoningLevels": [{"effort": "low", "description": "User setting"}]}
            old = [{"slug": slug, "provider": "jev", "listed": True,
                    "displayName": "Old auto mode"}
                   for slug in ("jev/auto-cheap", "jev/auto", "jev/auto-deep")]
            models_path.write_text(json.dumps({"version": 1, "models": [other, *old]}))
            env = {**os.environ, "CODEX_ROUTER_STATE_DIR": directory,
                   "MODEL_ROUTER_USER_MODELS": str(models_path), "JEV_DECISION_PROVIDER": "clef",
                   "CLEF_LOCAL_EXECUTOR_FILE": str(Path(directory, "executor.json"))}
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
                "jev/auto": ("auto", "Auto (Clef)", "medium"),
                "jev/auto-deep": ("auto-deep", "Auto Deep (Clef)", "high"),
            }
            self.assertEqual(len(models), 4)
            self.assertEqual([entry["slug"] for entry in models[1:] if entry["listed"]], ["jev/auto"])
            for entry in models[1:]:
                upstream, name, default_effort = profiles[entry["slug"]]
                self.assertEqual(entry["upstreamModel"], upstream)
                self.assertEqual(entry["displayName"], name)
                self.assertEqual([value["effort"] for value in entry["reasoningLevels"]], efforts)
                self.assertEqual(entry["defaultEffort"], default_effort)
                self.assertEqual(entry["listed"], entry["slug"] == "jev/auto")
                self.assertIn("maximum Sol effort", entry["description"])
                self.assertTrue(all("Luna follows your effort bounds" in level["description"]
                                    for level in entry["reasoningLevels"]))
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
