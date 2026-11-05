"""The backend answers its own commands, without lazarus-ai, against a fake Telegram API."""

import json
import os
import shutil
import tempfile
import time
import unittest
import urllib.request

from lazarus_backend.app import Backend
from lazarus_backend.commands import (ALREADY_MUTED_REPLY, ALREADY_UNMUTED_REPLY, HELP_TEXT, MUTE_REPLY,
                                      NOT_AVAILABLE, ROLLBACK_NOT_CONFIGURED, UNMUTE_NO_SPEAKER, UNMUTE_QUIET_HOURS,
                                      UNMUTE_REPLY)
from lazarus_backend.config import Config
from lazarus_backend.local_api import LazarusClient
from lazarus_backend.telegram_client import TelegramClient
from lazarus_backend.update_router import COMMANDS, UNKNOWN_COMMAND_REPLY
from tests.fakes import NOON, FakeSpeaker, FakeTelegram, RecordingServer, local_time, quiet_logs, text_update

OWNER = 1111


def wait_until(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class BackendCommandsTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)  # "text-to-speech tool not found" where the real Speaker looks for espeak-ng
        self.telegram = FakeTelegram()
        self.addCleanup(self.telegram.close)
        self.inbox = RecordingServer(lambda path, body: (200, {"ok": True}))
        self.addCleanup(self.inbox.close)

    def start_backend(self, lazarus_url, outbox_db=":memory:", **kwargs):
        config = Config(telegram_user_id=OWNER, api_port=0, lazarus_url=lazarus_url,
                        telegram_api_url=self.telegram.url, poll_timeout=1, outbox_db=outbox_db,
                        inbound_db=":memory:")
        backend = Backend(config, TelegramClient("123:token", api_url=self.telegram.url),
                          LazarusClient(lazarus_url, timeout=2), **kwargs)
        backend.start()
        self.addCleanup(backend.stop)
        return backend

    def answer_to(self, backend, text):
        backend.handle_update(text_update(1, OWNER, text))
        return "\n".join(body["text"] for body in self.telegram.wait_for("/sendMessage"))

    def test_help_and_start_list_every_command(self):
        backend = self.start_backend(self.inbox.url)
        for text in ("/help", "/start@lazarus_bot"):
            with self.subTest(text=text):
                self.telegram.requests.clear()
                self.assertEqual(HELP_TEXT, self.answer_to(backend, text))
        for name in set(COMMANDS) - {"start"}:  # /start is the same as /help
            self.assertIn("/" + name, HELP_TEXT)
        self.assertEqual([], self.inbox.bodies("/message"))

    def test_status_while_lazarus_is_up(self):
        backend = self.start_backend(self.inbox.url)
        answer = self.answer_to(backend, "/status@lazarus_bot")
        self.assertIn("Lazarus: up", answer)
        self.assertIn("Backend up: 0m", answer)
        self.assertEqual([], self.inbox.bodies("/message"))

    def test_help_and_status_work_while_lazarus_isnt_running(self):
        self.inbox.close()
        backend = self.start_backend(self.inbox.url)
        self.assertEqual(HELP_TEXT, self.answer_to(backend, "/help"))
        self.telegram.requests.clear()
        self.assertIn("Lazarus: down", self.answer_to(backend, "/status"))

    def test_every_command_is_implemented(self):
        backend = self.start_backend(self.inbox.url, speaker=FakeSpeaker())
        for name in COMMANDS:
            with self.subTest(name=name):
                self.telegram.requests.clear()
                self.assertNotEqual(NOT_AVAILABLE.format(name), self.answer_to(backend, "/" + name))

    # --- /mute, /unmute and speech items

    def mute_backend(self, now=NOON, speaker=None, outbox_db=":memory:"):
        self.speaker = speaker or FakeSpeaker()
        return self.start_backend(self.inbox.url, outbox_db=outbox_db, speaker=self.speaker, wall_clock=lambda: now)

    def say(self, backend, text):
        """Post a speech item to the LocalAPI's `send`, as lazarus-ai would, and wait until it's handled."""
        url = "http://{}:{}/send".format(*backend.api.address)
        request = urllib.request.Request(url, data=json.dumps({"text": text, "channel": "speech"}).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(202, response.status)
        self.assertTrue(wait_until(lambda: not backend.outbox.pending()))

    def test_speech_item_sent_to_the_local_api_is_spoken_not_written(self):
        backend = self.mute_backend()
        self.say(backend, "Good morning.")
        self.assertEqual(["Good morning."], self.speaker.said)
        self.assertEqual([], self.telegram.bodies("/sendMessage"))

    def test_mute_writes_speech_to_telegram_until_unmute(self):
        backend = self.mute_backend()
        self.assertEqual(MUTE_REPLY, self.answer_to(backend, "/mute"))
        self.telegram.requests.clear()
        self.assertEqual(ALREADY_MUTED_REPLY, self.answer_to(backend, "/mute@lazarus_bot"))
        self.telegram.requests.clear()
        self.say(backend, "Psst.")
        self.assertEqual([], self.speaker.said)
        self.assertEqual(["Psst."], [body["text"] for body in self.telegram.wait_for("/sendMessage")])

        self.telegram.requests.clear()
        self.assertEqual(UNMUTE_REPLY, self.answer_to(backend, "/unmute"))
        self.telegram.requests.clear()
        self.say(backend, "Hello again.")
        self.assertEqual(["Hello again."], self.speaker.said)
        self.assertEqual(ALREADY_UNMUTED_REPLY, self.answer_to(backend, "/unmute"))

    def test_mute_survives_a_backend_restart(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        path = os.path.join(folder, "data", "outbox.db")
        backend = self.mute_backend(outbox_db=path)
        self.answer_to(backend, "/mute")
        backend.stop()

        self.telegram.requests.clear()
        backend = self.mute_backend(outbox_db=path)
        self.assertTrue(backend.outbox.muted())
        self.say(backend, "Still quiet?")
        self.assertEqual([], self.speaker.said)
        self.assertEqual(["Still quiet?"], [body["text"] for body in self.telegram.wait_for("/sendMessage")])

    def test_speech_at_23_00_is_written_and_unmute_says_why(self):
        backend = self.mute_backend(now=local_time(23, 0))
        self.say(backend, "Late news.")
        self.assertEqual([], self.speaker.said)
        self.assertEqual(["Late news."], [body["text"] for body in self.telegram.wait_for("/sendMessage")])
        self.telegram.requests.clear()
        backend.outbox.set_muted(True)
        self.assertEqual(UNMUTE_REPLY + " " + UNMUTE_QUIET_HOURS.format("22:00–07:00"),
                         self.answer_to(backend, "/unmute"))

    def test_unmute_without_a_text_to_speech_tool_says_so(self):
        backend = self.mute_backend(speaker=FakeSpeaker(ready=False))
        backend.outbox.set_muted(True)
        self.assertEqual(UNMUTE_REPLY + " " + UNMUTE_NO_SPEAKER, self.answer_to(backend, "/unmute"))
        self.telegram.requests.clear()
        self.say(backend, "Nobody hears this.")
        self.assertEqual(["Nobody hears this."], [body["text"] for body in self.telegram.wait_for("/sendMessage")])

    def test_status_shows_whether_lazarus_is_muted(self):
        backend = self.mute_backend()
        self.assertIn("Speaking: on", self.answer_to(backend, "/status"))
        backend.outbox.set_muted(True)
        self.telegram.requests.clear()
        self.assertIn("Speaking: muted", self.answer_to(backend, "/status"))

    def test_rollback_without_a_repo_says_snapshots_are_off(self):
        backend = self.start_backend(self.inbox.url)
        for text in ("/rollback", "/rollback abc123", "/rollback abc123 memory"):
            with self.subTest(text=text):
                self.telegram.requests.clear()
                self.assertEqual(ROLLBACK_NOT_CONFIGURED, self.answer_to(backend, text))

    def test_unknown_command_is_answered_and_not_forwarded(self):
        backend = self.start_backend(self.inbox.url)
        self.assertEqual(UNKNOWN_COMMAND_REPLY.format("/foo"), self.answer_to(backend, "/foo"))
        self.assertEqual([], self.inbox.bodies("/message"))


if __name__ == "__main__":
    unittest.main()
