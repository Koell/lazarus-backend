"""TypingIndicator: keeps "typing…" visible while Lazarus works on a message.

Each forwarded message is tracked by its id from `start` until `stop` (a reply with a matching
`in_reply_to` arrived) or until `max_seconds` have passed. Telegram shows the indicator for about
5 s, so it's re-sent every `interval` seconds, once per chat however many messages are pending.
"""

import logging
import threading
import time

log = logging.getLogger(__name__)


class TypingIndicator:
    def __init__(self, send_typing, clock=time.monotonic, interval=4.0, max_seconds=600):
        self._send_typing = send_typing
        self._clock = clock
        self._interval = interval
        self._max_seconds = max_seconds
        self._pending = {}  # message id -> (chat id, started at)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self._thread = None

    def start(self, message_id, chat_id):
        with self._lock:
            self._pending[_key(message_id)] = (chat_id, self._clock())
        self._wake.set()

    def stop(self, message_id):
        with self._lock:
            self._pending.pop(_key(message_id), None)

    def pending(self):
        with self._lock:
            return sorted(self._pending)

    def tick(self):
        """Drop expired entries and send "typing…" once to every chat that still waits."""
        now = self._clock()
        with self._lock:
            for key, (_, started) in list(self._pending.items()):
                if now - started >= self._max_seconds:
                    log.warning("no reply to message %s after %d s, typing indicator stopped",
                                key, self._max_seconds)
                    del self._pending[key]
            chats = sorted({chat_id for chat_id, _ in self._pending.values()})
        for chat_id in chats:
            try:
                self._send_typing(chat_id)
            except Exception as e:
                log.debug("typing indicator for chat %s failed: %s", chat_id, e)

    def run_in_background(self):
        self._thread = threading.Thread(target=self._run, name="typing", daemon=True)
        self._thread.start()

    def shutdown(self):
        self._stopped.set()
        self._wake.set()

    def _run(self):
        while not self._stopped.is_set():
            self._wake.clear()
            self.tick()
            self._wake.wait(self._interval)


def _key(message_id):
    # lazarus-ai echoes the id back as it got it; compare as strings so 42 and "42" match.
    return str(message_id)
