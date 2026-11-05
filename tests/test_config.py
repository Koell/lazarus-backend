"""Config: the "auto" lazarus_command built from the llama_* keys, `~` expansion and the start-up errors."""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from lazarus_backend.config import AUTO_COMMAND, ConfigError, Config, build_lazarus_command, load_config


class LazarusCommandTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.home = os.path.join(self.dir, "home")
        self.model = os.path.join(self.home, "models", "qwen2.5-1.5b-instruct-q4_k_m.gguf")
        os.makedirs(os.path.dirname(self.model))
        open(self.model, "wb").close()
        environ = mock.patch.dict(os.environ, {"HOME": self.home, "USERPROFILE": self.home})
        environ.start()
        self.addCleanup(environ.stop)

    def load(self, **values):
        path = os.path.join(self.dir, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(values, f)
        return load_config(path)

    def on_path(self, name):
        return os.path.join(self.dir, "bin", name) if name == "llama-server" else None

    def test_default_runs_llama_server_and_lazarus_ai(self):
        self.assertEqual(AUTO_COMMAND, Config().lazarus_command)
        config = self.load()
        self.assertEqual(self.model, os.path.normpath(config.llama_model), "~ is the user's home")
        command = build_lazarus_command(config, which=self.on_path)
        log = os.path.join(self.dir, "logs/llama-server.log")
        lazarus_dir = os.path.join(self.dir, "../lazarus-ai")
        self.assertEqual(["{python}", "scripts/run_lazarus.py", "--lazarus-dir", lazarus_dir,
                          "--llama-health", "http://127.0.0.1:8080/health", "--llama-log", log,
                          "--", "llama-server", "-m", config.llama_model, "--host", "127.0.0.1", "--port", "8080",
                          "-c", "4096", "-t", "4"], command)

    def test_llama_keys_shape_the_command(self):
        binary = os.path.join(self.dir, "llama.cpp/llama-server")
        os.makedirs(os.path.dirname(binary))
        open(binary, "wb").close()
        config = self.load(llama_server_bin="llama.cpp/llama-server", llama_model=self.model, llama_port=8090,
                           llama_context=None, llama_threads=8, llama_args=["--flash-attn"], llama_log=None,
                           lazarus_folder="lazarus")
        command = build_lazarus_command(config, which=lambda name: None)
        self.assertEqual(["{python}", "scripts/run_lazarus.py", "--lazarus-dir", os.path.join(self.dir, "lazarus"),
                          "--llama-health", "http://127.0.0.1:8090/health",
                          "--", binary, "-m", self.model, "--host", "127.0.0.1", "--port", "8090",
                          "-t", "8", "--flash-attn"], command)

    def test_null_binary_or_model_starts_only_lazarus_ai(self):
        for key in ("llama_server_bin", "llama_model"):
            command = build_lazarus_command(self.load(**{key: None}), which=self.on_path)
            self.assertEqual(["{python}", "scripts/run_lazarus.py", "--lazarus-dir",
                              os.path.join(self.dir, "../lazarus-ai")], command)

    def test_lazarus_config_is_passed_on_only_when_set(self):
        command = build_lazarus_command(self.load(llama_model=None, lazarus_config="config.local.json"))
        self.assertEqual(["{python}", "scripts/run_lazarus.py", "--lazarus-dir", os.path.join(self.dir, "../lazarus-ai"),
                          "--lazarus-config", "config.local.json"], command)
        self.assertNotIn("--lazarus-config", build_lazarus_command(self.load(llama_model=None)))

    def test_a_missing_model_or_binary_is_a_clear_start_up_error(self):
        with self.assertRaisesRegex(ConfigError, "model file .* doesn't exist: set llama_model"):
            build_lazarus_command(self.load(llama_model="~/models/missing.gguf"), which=self.on_path)
        with self.assertRaisesRegex(ConfigError, "llama-server not found .*set llama_server_bin"):
            build_lazarus_command(self.load(), which=lambda name: None)
        with self.assertRaisesRegex(ConfigError, "llama-server not found"):
            build_lazarus_command(self.load(llama_server_bin="bin/llama-server"), which=self.on_path)

    def test_an_explicit_command_or_null_is_taken_as_it_is(self):
        explicit = ["sudo", "-u", "phablet", "python3", "run_lazarus.py"]
        self.assertEqual(explicit, build_lazarus_command(self.load(lazarus_command=explicit), which=self.on_path))
        self.assertIsNone(build_lazarus_command(self.load(lazarus_command=None), which=self.on_path))

    def test_invalid_values_are_rejected(self):
        for values in ({"lazarus_command": "llama-server -m x"}, {"lazarus_command": []}, {"llama_port": "8080"},
                       {"llama_threads": 0}, {"llama_context": "4096"}, {"llama_args": "--flash-attn"},
                       {"llama_model": 3}):
            with self.assertRaises(ConfigError, msg=values):
                self.load(**values)


class IncidentConfigTest(unittest.TestCase):
    def load(self, **values):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        path = os.path.join(folder, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(values, f)
        return load_config(path)

    def test_incident_thresholds_default_to_3_crashes_in_10_min_and_24_h_stable(self):
        config = self.load()
        self.assertEqual((3, 600, 86400), (config.incident_crashes, config.incident_window_seconds,
                                           config.stable_seconds))
        config = self.load(incident_crashes=5, incident_window_seconds=300, stable_seconds=3600)
        self.assertEqual((5, 300, 3600), (config.incident_crashes, config.incident_window_seconds,
                                          config.stable_seconds))

    def test_invalid_incident_thresholds_are_rejected(self):
        for values in ({"incident_crashes": 0}, {"incident_window_seconds": "600"}, {"stable_seconds": None},
                       {"incident_crashes": True}):
            with self.assertRaises(ConfigError, msg=values):
                self.load(**values)


if __name__ == "__main__":
    unittest.main()
