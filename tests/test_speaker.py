"""Speaker (with a fake subprocess: no text-to-speech tool runs), QuietHours, their config keys, /status."""

import datetime
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from lazarus_backend.config import Config, ConfigError, load_config
from lazarus_backend.speaker import QuietHours, Speaker
from lazarus_backend.status_probe import Status, format_status


class FakeRun:
    """Stands in for subprocess.run: records each call, answers with `returncode` or raises `error`."""

    def __init__(self, returncode=0, stderr=b"", error=None):
        self.calls = []
        self.returncode = returncode
        self.stderr = stderr
        self.error = error

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(command, self.returncode, None, self.stderr)


def on_path(*names, folder="/usr/bin"):
    return lambda name: os.path.join(folder, name) if name in names else None


class SpeakerTest(unittest.TestCase):
    def test_runs_the_tool_with_its_args_and_the_text_on_stdin(self):
        run = FakeRun()
        speaker = Speaker("espeak-ng", ["-v", "en", "--stdin"], run=run, which=on_path("espeak-ng"))
        self.assertTrue(speaker.ready())
        self.assertTrue(speaker.speak("-rf: Good morning."))
        (command, kwargs), = run.calls
        self.assertEqual([os.path.join("/usr/bin", "espeak-ng"), "-v", "en", "--stdin"], command)
        self.assertEqual("-rf: Good morning.".encode("utf-8"), kwargs["input"])
        self.assertIn("timeout", kwargs)

    def test_missing_tool_is_not_ready_and_warns_once(self):
        run = FakeRun()
        speaker = Speaker("espeak-ng", run=run, which=on_path())
        with self.assertLogs("lazarus_backend.speaker", "WARNING") as logs:
            self.assertFalse(speaker.speak("hello"))
            self.assertFalse(speaker.speak("hello"))
            self.assertFalse(speaker.ready())
        self.assertEqual(1, len(logs.records))
        self.assertIn("not found", logs.output[0])
        self.assertEqual([], run.calls)

    def test_a_tool_that_turns_up_later_is_used_and_warned_about_again_when_gone(self):
        present = []
        speaker = Speaker("espeak-ng", run=FakeRun(), which=lambda name: present[0] if present else None)
        with self.assertLogs("lazarus_backend.speaker", "INFO") as logs:
            self.assertFalse(speaker.speak("a"))
            present.append("/usr/bin/espeak-ng")
            self.assertTrue(speaker.speak("b"))
            present.clear()
            self.assertFalse(speaker.speak("c"))
        self.assertEqual(["WARNING", "INFO", "WARNING"], [r.levelname for r in logs.records])

    def test_a_path_is_checked_as_a_file_not_on_path(self):
        run = FakeRun()
        speaker = Speaker("/opt/tts/espeak-ng", run=run, which=on_path(), isfile=lambda path: True)
        self.assertTrue(speaker.speak("x"))
        self.assertEqual("/opt/tts/espeak-ng", run.calls[0][0][0])
        with self.assertLogs("lazarus_backend.speaker", "WARNING"):
            self.assertFalse(Speaker("/opt/tts/espeak-ng", run=run, isfile=lambda path: False).speak("x"))

    def test_a_tool_in_lazarus_folder_is_never_run(self):
        lazarus = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, lazarus)
        tool = os.path.join(lazarus, "bin", "espeak-ng")
        run = FakeRun()
        for speaker in (Speaker(tool, run=run, isfile=lambda path: True, forbidden_dir=lazarus),
                        Speaker("espeak-ng", run=run, which=on_path("espeak-ng", folder=os.path.dirname(tool)),
                                forbidden_dir=lazarus)):
            with self.assertLogs("lazarus_backend.speaker", "WARNING") as logs:
                self.assertFalse(speaker.speak("x"))
            self.assertIn("Lazarus's folder", logs.output[0])
        self.assertEqual([], run.calls)
        # A sibling folder whose name merely starts the same is fine.
        self.assertTrue(Speaker(lazarus + "-tools/espeak-ng", run=run, isfile=lambda path: True,
                                forbidden_dir=lazarus).speak("x"))

    def test_failing_or_hanging_tool_returns_false(self):
        for run in (FakeRun(returncode=1, stderr=b"no audio device"),
                    FakeRun(error=subprocess.TimeoutExpired("espeak-ng", 120)),
                    FakeRun(error=OSError("exec format error"))):
            with self.subTest(run=run.error or run.returncode):
                speaker = Speaker("espeak-ng", run=run, which=on_path("espeak-ng"))
                with self.assertLogs("lazarus_backend.speaker", "WARNING"):
                    self.assertFalse(speaker.speak("x"))

    def test_from_config(self):
        self.assertIsNone(Speaker.from_config(Config(tts_bin=None)))
        speaker = Speaker.from_config(Config())
        self.assertEqual("espeak-ng", speaker.binary)
        self.assertEqual(["-v", "en", "--stdin"], speaker.args)


class QuietHoursTest(unittest.TestCase):
    def test_window_across_midnight_includes_start_and_excludes_end(self):
        quiet = QuietHours("22:00", "07:00")
        for hour, minute, expected in ((21, 59, False), (22, 0, True), (22, 1, True), (23, 0, True),
                                       (0, 0, True), (6, 59, True), (7, 0, False), (7, 1, False), (12, 0, False)):
            self.assertEqual(expected, quiet.contains(datetime.time(hour, minute)), (hour, minute))
        self.assertEqual("22:00–07:00", quiet.describe())

    def test_window_within_a_day(self):
        quiet = QuietHours("13:00", "14:30")
        self.assertEqual([False, True, True, False],
                         [quiet.contains(datetime.time(*t)) for t in ((12, 59), (13, 0), (14, 29), (14, 30))])

    def test_invalid_windows_are_rejected(self):
        for start, end in (("22:00", "22:00"), ("25:00", "07:00"), ("10pm", "07:00"), (None, "07:00")):
            with self.assertRaises(ValueError, msg=(start, end)):
                QuietHours(start, end)


class SpeechConfigTest(unittest.TestCase):
    def load(self, **values):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)
        path = os.path.join(folder, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(values, f)
        return load_config(path), folder

    def test_defaults(self):
        config = Config()
        self.assertEqual(("espeak-ng", ("-v", "en", "--stdin"), ("22:00", "07:00")),
                         (config.tts_bin, config.tts_args, config.quiet_hours))

    def test_a_tool_name_stays_a_name_and_a_relative_path_is_resolved(self):
        config, _ = self.load(tts_bin="espeak-ng")
        self.assertEqual("espeak-ng", config.tts_bin)
        config, folder = self.load(tts_bin="bin/espeak-ng", quiet_hours=None, tts_args=["-v", "en-gb"])
        self.assertEqual(os.path.join(folder, "bin/espeak-ng"), config.tts_bin)
        self.assertIsNone(config.quiet_hours)

    def test_invalid_values_are_rejected(self):
        for values in ({"tts_bin": ""}, {"tts_bin": 3}, {"tts_args": "-v en"}, {"tts_args": [1]},
                       {"quiet_hours": "22:00-07:00"}, {"quiet_hours": ["22:00"]},
                       {"quiet_hours": ["22:00", "22:00"]}, {"quiet_hours": ["22:00", "7"]}):
            with self.subTest(values=values), self.assertRaises(ConfigError):
                self.load(**values)


class StatusLineTest(unittest.TestCase):
    def test_speaking_line(self):
        cases = [(dict(muted=False, quiet_hours=False, speaker_ready=True), "Speaking: on"),
                 (dict(muted=True, quiet_hours=True, speaker_ready=False), "Speaking: muted"),
                 (dict(muted=False, quiet_hours=True, speaker_ready=True), "Speaking: on, quiet hours now"),
                 (dict(muted=False, quiet_hours=False, speaker_ready=False), "Speaking: on, but no text-to-speech")]
        for fields, expected in cases:
            with self.subTest(fields=fields):
                self.assertIn("\n" + expected, format_status(Status(**fields)))

    def test_no_line_without_speech_status(self):
        self.assertNotIn("Speaking", format_status(Status()))


if __name__ == "__main__":
    unittest.main()
