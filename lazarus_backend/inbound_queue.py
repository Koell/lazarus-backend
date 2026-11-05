"""InboundQueue: the owner's messages for Lazarus, held while it's down and delivered in order later.

`submit(message)` hands a message straight to `deliver` while Lazarus is ready and nothing is waiting.
Otherwise, or if that fails, the message is stored in SQLite (`data/inbound.db` by default) behind the
others. One worker thread delivers the stored messages oldest first: right away when `retry_now()` is
called (the Supervisor saw Lazarus come up), else with a backoff of 5 s doubling up to 1 min.

An outage, from the owner's point of view, starts with the first message that has to wait and ends
when the queue is empty again. `submit` returns QUEUED_NEW_OUTAGE for that first message, so the
owner is told "down, message queued" once per outage, also across a restart of the backend.
"""

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass

DELIVERED = "delivered"
QUEUED = "queued"
QUEUED_NEW_OUTAGE = "queued, new outage"

INITIAL_BACKOFF = 5
MAX_BACKOFF = 60

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbound (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id,  -- no type on purpose: keeps a number a number and a string a string
    chat_id,
    text TEXT NOT NULL,
    sent_at,
    queued_at REAL NOT NULL
)
"""


@dataclass(frozen=True)
class InboundMessage:
    """One owner text message for Lazarus."""
    message_id: object
    chat_id: object
    text: str
    sent_at: object = None


class InboundQueue:
    """`deliver(message)` hands one message to Lazarus and raises if it can't; `ready()` says whether
    Lazarus is worth trying at all (False while it's stopped or restarting). `path` is the SQLite file."""

    def __init__(self, deliver, path=":memory:", ready=lambda: True, clock=time.time,
                 initial_backoff=INITIAL_BACKOFF, max_backoff=MAX_BACKOFF):
        self._deliver = deliver
        self._ready = ready
        self._clock = clock
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._failures = 0
        self._not_before = 0.0  # time.monotonic() before which the worker doesn't try again
        self._lock = threading.Lock()   # the database connection
        self._order = threading.Lock()  # one delivery at a time, so the order holds
        self._wake = threading.Event()   # a message was queued, or retry_now/stop
        self._retry = threading.Event()  # retry_now/stop: ends a backoff wait early
        self._stopping = threading.Event()
        self._thread = None
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        with self._lock, self._db:
            self._db.execute(_SCHEMA)

    # --- queue

    def submit(self, message):
        """Deliver `message` now if possible, else queue it: DELIVERED, QUEUED or QUEUED_NEW_OUTAGE."""
        with self._order:
            waiting = bool(self._rows(limit=1))
            if not waiting and self._ready():
                try:
                    self._deliver(message)
                    return DELIVERED
                except Exception as e:
                    log.error("couldn't deliver message %s to lazarus-ai: %s", message.message_id, e)
                    # It just failed: the worker waits out the first backoff before trying again.
                    self._failures = 1
                    self._not_before = time.monotonic() + self._initial_backoff
            with self._lock, self._db:
                self._db.execute(
                    "INSERT INTO inbound (message_id, chat_id, text, sent_at, queued_at) VALUES (?, ?, ?, ?, ?)",
                    (message.message_id, message.chat_id, message.text, message.sent_at, self._clock()))
        self._wake.set()
        return QUEUED if waiting else QUEUED_NEW_OUTAGE

    def pending(self):
        """The waiting messages, oldest first."""
        return [message for _, message in self._rows()]

    def _rows(self, limit=-1):
        with self._lock:
            rows = self._db.execute(
                "SELECT id, message_id, chat_id, text, sent_at FROM inbound ORDER BY id LIMIT ?",
                (limit,)).fetchall()
        return [(row[0], InboundMessage(*row[1:])) for row in rows]

    # --- delivery

    def deliver_next(self):
        """Try to deliver the oldest waiting message once.

        Returns None if nothing waits, 0 if it was delivered, else the seconds until the next try.
        """
        with self._order:
            rows = self._rows(limit=1)
            if not rows:
                return None
            row_id, message = rows[0]
            try:
                self._deliver(message)
            except Exception as e:
                self._failures += 1
                delay = min(self._initial_backoff * 2 ** min(self._failures - 1, 16), self._max_backoff)
                log.info("couldn't deliver queued message %s (%s), retrying in %s s",
                         message.message_id, e, delay)
                return delay
            with self._lock, self._db:
                self._db.execute("DELETE FROM inbound WHERE id = ?", (row_id,))
        self._failures = 0
        log.info("delivered queued message %s to lazarus-ai", message.message_id)
        return 0

    def retry_now(self):
        """Lazarus is up again: deliver what waits without waiting out the backoff."""
        self._failures = 0
        self._not_before = 0.0
        self._retry.set()
        self._wake.set()

    def start(self):
        self._thread = threading.Thread(target=self._run, name="inbound", daemon=True)
        self._thread.start()

    def stop(self, timeout=5):
        """Stop the worker; waiting messages stay in the database for the next start."""
        self._stopping.set()
        self._retry.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)
            if self._thread.is_alive():
                return  # still inside deliver(); the daemon thread ends with the process
        with self._lock:
            self._db.close()

    def _run(self):
        while not self._stopping.is_set():
            self._wake.clear()
            hold_off = self._not_before - time.monotonic()
            if not self._ready():
                delay = self._max_backoff  # retry_now() wakes it as soon as Lazarus is up
            elif hold_off > 0:
                delay = hold_off
            else:
                try:
                    delay = self.deliver_next()
                except sqlite3.Error:
                    log.exception("inbound queue database error")
                    delay = self._max_backoff
            if delay is None:
                self._wake.wait()
            elif delay > 0:
                # New messages don't shorten the wait: they queue behind the one that failed.
                self._retry.wait(delay)
                self._retry.clear()
