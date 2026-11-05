"""StabilityLedger: which commit of Lazarus was deployed when, its incidents, and the last push.

A deployment is a commit whose code Lazarus runs from a given time on: the first snapshot of a
changed code state, and later every rollback. The latest deployment is the running commit that
`/status` shows. An incident (3 crashes within 10 min, see auto_rollback.py) is recorded with its
time and the commit that ran.

A deployment is **stable** once it has run `stable_seconds` (24 h) without an incident: the next
deployment (or now, for the running one) came at least that long after it, and no incident falls
into its first `stable_seconds`. An incident after that doesn't take the stability back.
`latest_stable(other_than)` is the automatic rollback's target.

SQLite (`data/ledger.db` by default, git-ignored; ":memory:" for tests), one connection shared by
the threads, serialised by a lock, like the Outbox.
"""

import os
import sqlite3
import threading
import time
from dataclasses import dataclass

_SCHEMA = """
CREATE TABLE IF NOT EXISTS deployments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha TEXT NOT NULL,
    branch TEXT NOT NULL,
    deployed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pushes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha TEXT NOT NULL,
    pushed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha TEXT,                -- the running commit, NULL if none was recorded yet
    at REAL NOT NULL
);
"""
# A commit is stable once it has run this long without an incident.
STABLE_SECONDS = 24 * 3600


@dataclass(frozen=True)
class Deployment:
    sha: str
    branch: str
    deployed_at: float


@dataclass(frozen=True)
class Push:
    sha: str
    pushed_at: float


@dataclass(frozen=True)
class Incident:
    sha: object   # the commit that ran, or None
    at: float


class StabilityLedger:
    def __init__(self, path=":memory:", clock=time.time):
        self.clock = clock
        self._lock = threading.Lock()
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)

    def record_deployment(self, sha, branch, deployed_at=None):
        """Lazarus runs the code of `sha` (on `branch`) from `deployed_at` (default: now) on."""
        deployed_at = self.clock() if deployed_at is None else deployed_at
        with self._lock, self._db:
            self._db.execute("INSERT INTO deployments (sha, branch, deployed_at) VALUES (?, ?, ?)",
                             (sha, branch, deployed_at))
        return Deployment(sha, branch, deployed_at)

    def current_deployment(self):
        """The latest deployment, i.e. the running commit, or None before the first one."""
        deployments = self.deployments(limit=1)
        return deployments[0] if deployments else None

    def deployments(self, limit=-1):
        """Deployments, newest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT sha, branch, deployed_at FROM deployments ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [Deployment(*row) for row in rows]

    def record_push(self, sha, pushed_at=None):
        """The branch reached the remote at `sha`."""
        pushed_at = self.clock() if pushed_at is None else pushed_at
        with self._lock, self._db:
            self._db.execute("INSERT INTO pushes (sha, pushed_at) VALUES (?, ?)", (sha, pushed_at))
        return Push(sha, pushed_at)

    def last_push(self):
        """The latest successful push, or None."""
        with self._lock:
            row = self._db.execute("SELECT sha, pushed_at FROM pushes ORDER BY id DESC LIMIT 1").fetchone()
        return Push(*row) if row else None

    def record_incident(self, sha, at=None):
        """An incident happened at `at` (default: now) while `sha` (None if unknown) ran."""
        at = self.clock() if at is None else at
        with self._lock, self._db:
            self._db.execute("INSERT INTO incidents (sha, at) VALUES (?, ?)", (sha, at))
        return Incident(sha, at)

    def incidents(self):
        """Every incident, oldest first."""
        with self._lock:
            rows = self._db.execute("SELECT sha, at FROM incidents ORDER BY at, id").fetchall()
        return [Incident(*row) for row in rows]

    def latest_stable(self, other_than=None, now=None, stable_seconds=STABLE_SECONDS):
        """The latest stable deployment whose commit isn't `other_than`, or None.

        A deployment is stable if it ran `stable_seconds` (until the next deployment, or until `now`,
        default the clock, for the running one) and no incident falls into its first `stable_seconds`.
        """
        now = self.clock() if now is None else now
        oldest_first = list(reversed(self.deployments()))
        incidents = [incident.at for incident in self.incidents()]
        ends = [d.deployed_at for d in oldest_first[1:]] + [now]
        for deployment, end in reversed(list(zip(oldest_first, ends))):
            if deployment.sha == other_than:
                continue
            start = deployment.deployed_at
            if end - start < stable_seconds:
                continue
            if any(start <= at < start + stable_seconds for at in incidents):
                continue
            return deployment
        return None

    def close(self):
        with self._lock:
            self._db.close()
