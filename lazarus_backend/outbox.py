"""Outbox: everything the backend sends to the owner goes through here, in order.

Items wait in a SQLite queue (`data/outbox.db` by default) until `deliver(item)` succeeds, so nothing
is lost while Telegram is unreachable or the phone reboots. One worker thread delivers the oldest
item first; a failed delivery is retried with exponential backoff (5 s doubling up to 5 min), and
the items behind it wait, which keeps the order.

Priority is stored as given and not enforced yet (the message budget comes with milestone 3).

Speech items go to `speak(text)` (the Speaker) unless the owner muted it (`/mute`, kept in the same
database across restarts) or it's quiet hours on `clock`; then, and whenever `speak` returns False
(no text-to-speech tool, or it failed), they go to `deliver` as text like any Telegram item.
`clock` is the wall-clock time source for both the creation time and the quiet hours.
"""

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass

CHANNELS = ("telegram", "speech")
PRIORITIES = ("urgent", "normal", "low")

INITIAL_BACKOFF = 5
MAX_BACKOFF = 300

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    channel TEXT NOT NULL,
    priority TEXT NOT NULL,
    in_reply_to,  -- no type on purpose: keeps a number a number and a string a string
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS flags (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""
_MUTED = "muted"


@dataclass(frozen=True)
class OutboxItem:
    text: str
    channel: str = "telegram"
    priority: str = "normal"
    in_reply_to: object = None
    created_at: float = 0.0


def make_item(text, channel="telegram", priority="normal", in_reply_to=None, clock=time.time):
    """A validated OutboxItem; raises ValueError for anything the Outbox can't take."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a non-empty string")
    if channel not in CHANNELS:
        raise ValueError("channel must be one of {}".format(", ".join(CHANNELS)))
    if priority not in PRIORITIES:
        raise ValueError("priority must be one of {}".format(", ".join(PRIORITIES)))
    if in_reply_to is not None and (isinstance(in_reply_to, bool) or not isinstance(in_reply_to, (str, int))):
        raise ValueError("in_reply_to must be a string, a number or null")
    return OutboxItem(text=text, channel=channel, priority=priority, in_reply_to=in_reply_to,
                      created_at=clock())


def backoff_delay(failures, initial=INITIAL_BACKOFF, maximum=MAX_BACKOFF):
    """Seconds to wait after the `failures`-th failed attempt in a row (1, 2, …): doubling, capped."""
    return min(initial * 2 ** min(failures - 1, 32), maximum)


class Outbox:
    """A persistent queue delivered in order through `deliver(item)` on a worker thread.

    `deliver` raises on failure. If `is_permanent(error)` says the failure won't go away by retrying
    (Telegram refused the item itself), the item is logged and dropped; otherwise it stays first in
    line and is retried after a backoff. `path` is the SQLite file (":memory:" for tests).

    `speak(text)` says a speech item aloud and returns True once it's been said; None: no Speaker, all
    speech goes to `deliver`. `quiet_hours` (a speaker.QuietHours, or None) is when nothing is spoken.
    """

    def __init__(self, deliver, path=":memory:", clock=time.time, is_permanent=lambda error: False,
                 initial_backoff=INITIAL_BACKOFF, max_backoff=MAX_BACKOFF, speak=None, quiet_hours=None):
        self._deliver = deliver
        self._speak = speak
        self.quiet_hours = quiet_hours
        self._is_permanent = is_permanent
        self.clock = clock
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._failures = 0
        self._lock = threading.Lock()
        self._wake = threading.Event()  # an item was added, or retry_now/stop
        self._retry = threading.Event()  # retry_now/stop: ends a backoff wait early
        self._stopping = threading.Event()
        self._thread = None
        if path != ":memory:":
            folder = os.path.dirname(os.path.abspath(path))
            os.makedirs(folder, exist_ok=True)
        # One connection shared by the threads that put and the worker; the lock serialises them.
        self._db = sqlite3.connect(path, check_same_thread=False)
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)

    # --- speech: /mute and quiet hours

    def muted(self):
        """True after `/mute` until `/unmute`, also across restarts."""
        with self._lock:
            row = self._db.execute("SELECT value FROM flags WHERE name = ?", (_MUTED,)).fetchone()
        return bool(row and row[0])

    def set_muted(self, muted):
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO flags (name, value) VALUES (?, ?)", (_MUTED, int(bool(muted))))

    def in_quiet_hours(self):
        """True if it's quiet hours now, on `clock` in local time."""
        return self.quiet_hours is not None and self.quiet_hours.contains_timestamp(self.clock())

    def may_speak(self):
        """Whether a speech item would be spoken now (a Speaker, not muted, not quiet hours)."""
        return self._speak is not None and not self.muted() and not self.in_quiet_hours()

    # --- queue

    def put(self, item):
        """Store `item` at the end of the queue; it's on disk when this returns."""
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO outbox (text, channel, priority, in_reply_to, created_at) VALUES (?, ?, ?, ?, ?)",
                (item.text, item.channel, item.priority, item.in_reply_to, item.created_at))
        self._wake.set()

    def pending(self):
        """The undelivered items, oldest first."""
        return [item for _, item in self._rows()]

    def _rows(self, limit=-1):
        with self._lock:
            rows = self._db.execute(
                "SELECT id, text, channel, priority, in_reply_to, created_at FROM outbox ORDER BY id LIMIT ?",
                (limit,)).fetchall()
        return [(row[0], OutboxItem(*row[1:])) for row in rows]

    def _remove(self, item_id):
        with self._lock, self._db:
            self._db.execute("DELETE FROM outbox WHERE id = ?", (item_id,))

    # --- delivery

    def deliver_next(self):
        """Try to deliver the oldest item once.

        Returns None if the queue is empty, 0 if the item is done (delivered or dropped), else the
        seconds to wait before the next attempt.
        """
        rows = self._rows(limit=1)
        if not rows:
            return None
        item_id, item = rows[0]
        if item.channel == "speech" and self.may_speak() and self._spoken(item_id, item):
            self._remove(item_id)
            self._failures = 0
            return 0
        try:
            self._deliver(item)
        except Exception as e:
            if self._is_permanent(e):
                log.error("outbox item %s (in_reply_to=%s) refused, dropped: %s", item_id, item.in_reply_to, e)
                self._remove(item_id)
                self._failures = 0
                return 0
            self._failures += 1
            delay = backoff_delay(self._failures, self._initial_backoff, self._max_backoff)
            log.info("couldn't deliver outbox item %s (%s), retrying in %s s, %d waiting",
                     item_id, e, delay, len(self._rows()))
            return delay
        self._remove(item_id)
        self._failures = 0
        return 0

    def _spoken(self, item_id, item):
        """Speak `item`; False (it then goes to `deliver` as text) if the Speaker couldn't."""
        try:
            return bool(self._speak(item.text))
        except Exception:
            log.exception("speaking outbox item %s failed, sending it as text", item_id)
            return False

    def retry_now(self):
        """Skip the current backoff wait, e.g. because Telegram is reachable again."""
        self._failures = 0
        self._retry.set()
        self._wake.set()

    def start(self):
        self._thread = threading.Thread(target=self._run, name="outbox", daemon=True)
        self._thread.start()

    def stop(self, timeout=5):
        """Stop the worker; undelivered items stay in the database for the next start."""
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
            # Clear before looking at the queue, so a put during delivery isn't missed.
            self._wake.clear()
            try:
                delay = self.deliver_next()
            except sqlite3.Error:
                log.exception("outbox database error")
                delay = self._max_backoff
            if delay is None:
                self._wake.wait()
            elif delay > 0:
                # New items don't shorten the wait: they queue behind the one that failed.
                self._retry.wait(delay)
                self._retry.clear()
