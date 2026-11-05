"""Smoke test of the wired backend against a fake Telegram API and a fake lazarus-ai Inbox."""

import json
import threading
import unittest
import urllib.error
import urllib.request

from lazarus_backend.app import LAZARUS_DOWN_QUEUED, Backend
from lazarus_backend.config import Config
from lazarus_backend.local_api import LazarusClient
from lazarus_backend.telegram_client import TelegramClient
from tests.fakes import FakeTelegram, RecordingServer, media_update, text_update

OWNER = 1111
STRANGER = 2222


def post_json(url, body):
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        with e:
            return e.code, json.loads(e.read().decode("utf-8"))


class BackendSmokeTest(unittest.TestCase):
    def setUp(self):
        self.telegram = FakeTelegram()
        self.addCleanup(self.telegram.close)
        self.inbox = RecordingServer(lambda path, body: (202, {"ok": True}))
        self.addCleanup(self.inbox.close)
        self.backend = self._backend(self.inbox.url)

    def _backend(self, lazarus_url):
        config = Config(telegram_user_id=OWNER, api_port=0, lazarus_url=lazarus_url,
                        telegram_api_url=self.telegram.url, poll_timeout=1, outbox_db=":memory:",
                        inbound_db=":memory:")
        backend = Backend(config, TelegramClient("123:token", api_url=self.telegram.url),
                          LazarusClient(lazarus_url, timeout=2))
        backend.start()
        self.addCleanup(backend.stop)
        host, port = backend.api.address
        self.api_url = "http://{}:{}".format(host, port)
        return backend

    def _poll(self):
        thread = threading.Thread(target=self.backend.poll_forever, daemon=True)
        thread.start()

    def test_owner_text_shows_typing_is_forwarded_and_the_reply_arrives(self):
        self._poll()
        self.telegram.updates.put(text_update(1, OWNER, "Why is the sky blue?", message_id=55))

        forwarded = self.inbox.wait_for("/message")
        self.assertEqual([{"id": "55", "text": "Why is the sky blue?", "sent_at": 1700000000}], forwarded)
        self.assertEqual({"chat_id": OWNER, "action": "typing"},
                         self.telegram.wait_for("/sendChatAction")[0])
        self.assertEqual(["55"], self.backend.typing.pending())

        status, body = post_json(self.api_url + "/send", {"text": "Rayleigh scattering.", "in_reply_to": "55"})
        self.assertEqual((202, {"ok": True}), (status, body))
        self.assertEqual([{"chat_id": OWNER, "text": "Rayleigh scattering."}],
                         self.telegram.wait_for("/sendMessage"))
        self.assertEqual([], self.backend.typing.pending())

    def test_stranger_gets_no_reply_and_their_id_is_logged(self):
        with self.assertLogs("lazarus_backend", level="WARNING") as logs:
            self.backend.handle_update(text_update(1, STRANGER, "hi"))
        self.assertIn(str(STRANGER), "\n".join(logs.output))
        self.assertEqual([], self.inbox.requests)
        self.assertEqual([], self.telegram.requests)

    def test_owner_voice_sticker_or_photo_gets_no_reply_and_isnt_forwarded(self):
        for kind in ("voice", "sticker", "photo"):
            with self.subTest(kind=kind), self.assertLogs("lazarus_backend", level="INFO") as logs:
                self.backend.handle_update(media_update(1, OWNER, kind))
            self.assertIn("not a text message", "\n".join(logs.output))
        self.assertEqual([], self.backend.outbox.pending())
        self.assertEqual([], self.backend.inbound.pending())
        self.assertEqual([], self.backend.typing.pending())
        self.assertEqual([], self.inbox.requests)
        self.assertEqual([], self.telegram.requests)

    def test_long_answer_arrives_as_several_messages(self):
        text = "\n".join("line {} ".format(i) + "z" * 90 for i in range(100))
        post_json(self.api_url + "/send", {"text": text})
        sent = self.telegram.wait_for("/sendMessage", count=3)
        self.assertEqual(text, "\n".join(body["text"] for body in sent))

    def test_unreachable_lazarus_queues_messages_and_tells_the_owner_once(self):
        self.inbox.close()
        with self.assertLogs("lazarus_backend", level="ERROR"):
            self.backend.handle_update(text_update(1, OWNER, "hello"))
            self.backend.handle_update(text_update(2, OWNER, "anyone there?"))
        self.assertEqual([{"chat_id": OWNER, "text": LAZARUS_DOWN_QUEUED}],
                         self.telegram.wait_for("/sendMessage"))
        self.assertEqual(["hello", "anyone there?"], [m.text for m in self.backend.inbound.pending()])
        self.assertEqual([], self.backend.typing.pending())

    def test_send_rejects_invalid_items(self):
        for body in ({}, {"text": ""}, {"text": "x", "channel": "pigeon"}, {"text": "x", "priority": "max"}):
            status, answer = post_json(self.api_url + "/send", body)
            self.assertEqual(400, status, body)
            self.assertFalse(answer["ok"])

    def test_health_answers(self):
        with urllib.request.urlopen(self.api_url + "/health", timeout=5) as response:
            self.assertTrue(json.loads(response.read().decode("utf-8"))["ok"])


if __name__ == "__main__":
    unittest.main()
