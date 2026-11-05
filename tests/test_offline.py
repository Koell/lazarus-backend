"""The wired backend while Telegram is unreachable: outgoing messages wait, in order, across a
restart, and owner messages held by Telegram are handled once polling gets through again."""

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from lazarus_backend.app import Backend
from lazarus_backend.config import Config
from lazarus_backend.outbox import make_item
from lazarus_backend.telegram_client import TelegramError
from tests.fakes import text_update

OWNER = 1111


class FlakyTelegram:
    """A Telegram client stand-in that fails with network errors while `online` is false."""

    def __init__(self):
        self.online = False
        self.sent = []
        self.updates = []
        self.polls = 0
        self.lock = threading.Lock()

    def _check(self):
        if not self.online:
            raise urllib.error.URLError("network is unreachable")

    def get_updates(self, offset=None, timeout=50):
        self.polls += 1
        self._check()
        with self.lock:
            updates = [u for u in self.updates if offset is None or u["update_id"] >= offset]
        time.sleep(0.01)
        return updates

    def send_message(self, chat_id, text):
        self._check()
        with self.lock:
            self.sent.append((chat_id, text))

    def send_typing(self, chat_id):
        self._check()

    def texts(self):
        with self.lock:
            return [text for _, text in self.sent]


class FakeLazarus:
    def __init__(self):
        self.messages = []

    def post_message(self, message_id, text, sent_at):
        self.messages.append((message_id, text))

    def health(self):
        return True


def wait_until(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


class OfflineTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.telegram = FlakyTelegram()
        self.lazarus = FakeLazarus()

    def backend(self):
        config = Config(telegram_user_id=OWNER, api_port=0, poll_timeout=1, tts_bin=None,
                        outbox_db=os.path.join(self.dir, "data", "outbox.db"),
                        inbound_db=os.path.join(self.dir, "data", "inbound.db"))
        backend = Backend(config, self.telegram, self.lazarus)
        backend.poll_backoff = backend.poll_max_backoff = 0.02
        backend.start()
        self.addCleanup(backend.stop)
        return backend

    def test_messages_sent_while_offline_arrive_in_order_after_a_restart(self):
        backend = self.backend()
        for text in ("one", "two", "three"):
            backend.on_send(make_item(text, in_reply_to="5"))
        self.assertTrue(wait_until(lambda: len(backend.outbox.pending()) == 3))
        backend.stop()
        self.assertEqual([], self.telegram.texts())

        self.telegram.online = True
        self.backend()
        self.assertTrue(wait_until(lambda: len(self.telegram.texts()) == 3))
        self.assertEqual(["one", "two", "three"], self.telegram.texts())

    def test_send_queues_channel_and_priority_as_given_with_defaults(self):
        backend = self.backend()
        url = "http://{}:{}/send".format(*backend.api.address)
        for body in ({"text": "plain"}, {"text": "spoken", "channel": "speech", "priority": "urgent",
                                         "in_reply_to": "9"}):
            request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                             headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(202, response.status)
        self.assertTrue(wait_until(lambda: len(backend.outbox.pending()) == 2))
        self.assertEqual([("plain", "telegram", "normal", None), ("spoken", "speech", "urgent", "9")],
                         [(i.text, i.channel, i.priority, i.in_reply_to) for i in backend.outbox.pending()])

    def test_reconnecting_polls_owner_messages_and_flushes_the_outbox_right_away(self):
        backend = self.backend()
        backend.on_send(make_item("queued while offline"))
        self.telegram.updates.append(text_update(10, OWNER, "sent while the phone was offline"))
        poller = threading.Thread(target=backend.poll_forever, daemon=True)
        poller.start()
        self.assertTrue(wait_until(lambda: self.telegram.polls >= 2))
        self.assertEqual([], self.lazarus.messages)

        # The Outbox's own backoff (5 s and more) would outlast this test: the successful poll
        # makes it retry at once.
        self.telegram.online = True
        self.assertTrue(wait_until(lambda: self.lazarus.messages and self.telegram.texts()))
        self.assertEqual([(10, "sent while the phone was offline")], self.lazarus.messages)
        self.assertEqual(["queued while offline"], self.telegram.texts())

    def test_polling_survives_unexpected_errors(self):
        backend = self.backend()
        calls = []

        def get_updates(offset=None, timeout=50):
            calls.append(offset)
            if len(calls) == 1:
                raise RuntimeError("something odd")
            if len(calls) == 2:
                raise TelegramError("getUpdates failed: HTTP 502 Bad Gateway", code=502)
            backend._stopped.set()
            return []

        self.telegram.get_updates = get_updates
        with self.assertLogs("lazarus_backend", level="INFO"):
            backend.poll_forever()
        self.assertEqual(3, len(calls))


class TelegramErrorTest(unittest.TestCase):
    def test_only_refusals_of_the_item_itself_are_permanent(self):
        self.assertTrue(TelegramError("x", code=400).permanent)
        self.assertTrue(TelegramError("x", code=403).permanent)
        for code in (None, 401, 429, 500, 502):
            self.assertFalse(TelegramError("x", code=code).permanent, code)


if __name__ == "__main__":
    unittest.main()
