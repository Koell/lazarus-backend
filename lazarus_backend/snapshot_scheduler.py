"""SnapshotScheduler: RepoManager's snapshot every night at a set local time, and pushes that wait.

A snapshot runs once the wall clock passes the snapshot time (default 03:00); if the phone was
asleep or the backend down at that moment, it runs at the next check instead, once. Commits that
couldn't be pushed are pushed at the first check after a start, every `retry_seconds` after that,
and right away when `retry_now()` says the network is back. `now` is injectable for tests.
"""

import datetime
import logging
import threading

RETRY_SECONDS = 600
CHECK_SECONDS = 30

log = logging.getLogger(__name__)


def parse_time(text):
    """"HH:MM" -> datetime.time; ValueError for anything else."""
    try:
        hours, minutes = text.split(":")
        return datetime.time(int(hours), int(minutes))
    except (AttributeError, TypeError, ValueError):
        raise ValueError("expected a time like 03:00, got {!r}".format(text))


def next_run_after(now, at):
    """The first moment strictly after `now` whose time of day is `at`."""
    candidate = now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += datetime.timedelta(days=1)
    return candidate


class SnapshotScheduler:
    """Drives `repo` (a RepoManager): `tick()` does what is due; `start()` ticks on a thread.

    `on_failure(error)` is called when a snapshot couldn't be taken at all (not for a failed push).
    """

    def __init__(self, repo, at="03:00", now=datetime.datetime.now, retry_seconds=RETRY_SECONDS,
                 check_seconds=CHECK_SECONDS, on_failure=lambda error: None):
        self.repo = repo
        self.at = parse_time(at)
        self._now = now
        self._retry_seconds = retry_seconds
        self._check_seconds = check_seconds
        self._on_failure = on_failure
        self.next_run = None
        self._next_push = None  # None: at the next check
        self._retry_requested = False
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread = None

    def tick(self):
        now = self._now()
        if self.next_run is None:
            self.next_run = next_run_after(now, self.at)
        if now >= self.next_run:
            self.next_run = next_run_after(now, self.at)
            self._retry_requested = False
            self._snapshot(now)
            return
        retry, self._retry_requested = self._retry_requested, False
        if (retry or self._next_push is None or now >= self._next_push) and self.repo.pending_count():
            self._push(now)

    def retry_now(self):
        """The network is back: push waiting commits at once instead of after the retry interval."""
        self._retry_requested = True
        self._wake.set()

    def _snapshot(self, now):
        log.info("taking the nightly snapshot")
        try:
            result = self.repo.snapshot()
        except Exception as e:
            log.exception("nightly snapshot failed")
            self._on_failure(e)
            return
        self._next_push = None if result.pushed else now + datetime.timedelta(seconds=self._retry_seconds)

    def _push(self, now):
        try:
            done = self.repo.push_pending()
        except Exception:
            log.exception("pushing waiting snapshots failed")
            done = False
        self._next_push = None if done else now + datetime.timedelta(seconds=self._retry_seconds)

    # --- thread

    def start(self):
        self._thread = threading.Thread(target=self._run, name="snapshots", daemon=True)
        self._thread.start()

    def stop(self, timeout=5):
        self._stopping.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)

    def _run(self):
        while not self._stopping.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("snapshot scheduler failed")
            self._wake.wait(self._check_seconds)
            self._wake.clear()
