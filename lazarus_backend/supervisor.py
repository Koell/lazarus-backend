"""Supervisor: keeps lazarus-ai running.

It starts lazarus-ai through a launcher (see launcher.py: one process group with llama-server, output
captured into a log) and checks it every few seconds. A crash is either

- the process exiting, or
- no healthy `health` answer for `hang_seconds` (60 s) while it runs; right after a start the first
  healthy answer may take up to `start_seconds`, because llama-server loads the model first.

After a crash the process group is killed and started again after a short backoff (1 s, doubling with
every crash in the last 10 min, at most 30 s).

What the owner sees (`state()`): STARTING from a start until the first healthy answer, then UP; after a
crash DOWN while the backoff runs, STARTING again once the process is launched; STOPPED after `/stop`;
HALTED after an incident that stopped Lazarus (see below).

With `incidents` (an AutoRollback, see auto_rollback.py) every crash is checked for an incident (3
crashes within 10 min). For a ROLL_BACK decision the Supervisor holds Lazarus stopped (state DOWN, no
restart) until `resume()`; the backend restores the stable commit meanwhile and then resumes, or calls
`stop_after_incident()` if the restore failed. A STOP decision stops Lazarus right away with the
persisted stop flag (state HALTED), so a broken Lazarus never restarts in a loop. `on_incident(decision)`
is called after either, outside the lock. The owner's `/restart` clears the stop flag and resets the
AutoRollback (crash count and the one-rollback budget).

What survives a restart of the backend lives in SupervisorState (SQLite): the stop flag, which `/stop`
and a stopping incident set and `/restart` clears, whether an incident set it, a record of every crash
with its time, which crashes an incident or the owner already settled, and the time of the last
automatic rollback.

The clocks and the launcher are injected, so tests drive `tick()` with a fake clock and a fake launcher.
A launcher has `start()` returning a process handle with `poll()` (None while running, else the exit
code) and `stop()` (ends the whole group, also after the leader exited), and `note(text)`, which
appends a line to the captured log.
"""

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass

from . import auto_rollback

UP = "up"
STARTING = "starting"  # launched (or about to be, at the backend's start), no healthy answer yet
DOWN = "down"          # crashed, waiting out the backoff before the restart
STOPPED = "stopped"    # stopped by the owner with /stop
HALTED = "halted"      # stopped after an incident (no automatic rollback possible or left); /restart clears it

CHECK_INTERVAL = 2
INITIAL_BACKOFF = 1
MAX_BACKOFF = 30
# Crashes within this many seconds count towards the backoff.
BACKOFF_WINDOW = 600

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS crashes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,        -- wall-clock time (time.time())
    reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS flags (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS times (
    name TEXT PRIMARY KEY,
    at REAL                  -- wall-clock time, NULL for none
);
"""
_STOPPED_BY_OWNER = "stopped_by_owner"   # the stop flag (also set by a stopping incident)
_HALTED = "halted_after_incident"        # the stop flag was set by an incident, not by /stop
_CRASH_MARK = "counted_crashes"          # the id of the last crash that counts no more towards an incident
_AUTO_ROLLBACK_AT = "auto_rollback_at"


@dataclass(frozen=True)
class Crash:
    at: float      # wall-clock time
    reason: str


@dataclass(frozen=True)
class _CrashEvent:
    crash: Crash
    decision: object = None  # the AutoRollback's Decision if the crash made an incident


class SupervisorState:
    """The Supervisor's persistent part: the stop flag, the crash record and the AutoRollback's times.

    `path` is the SQLite file (":memory:" for tests). Thread-safe.
    """

    def __init__(self, path=":memory:"):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)

    def stopped_by_owner(self):
        """The stop flag: Lazarus stays stopped, also across restarts of the backend, until /restart."""
        return self._flag(_STOPPED_BY_OWNER)

    def set_stopped_by_owner(self, stopped):
        self._set_flag(_STOPPED_BY_OWNER, stopped)

    def halted(self):
        """True if the stop flag was set by an incident rather than by the owner's /stop."""
        return self._flag(_HALTED)

    def set_halted(self, halted):
        self._set_flag(_HALTED, halted)

    def mark_crashes(self):
        """The crashes recorded so far count no more towards an incident (`crashes(after_mark=True)`)."""
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO flags (name, value) SELECT ?, COALESCE(MAX(id), 0) FROM crashes",
                             (_CRASH_MARK,))

    def auto_rollback_at(self):
        """When the last automatic rollback was decided, None if the budget is unused (or was reset)."""
        return self._time(_AUTO_ROLLBACK_AT)

    def set_auto_rollback_at(self, at):
        self._set_time(_AUTO_ROLLBACK_AT, at)

    def _flag(self, name):
        with self._lock:
            return bool(self._raw_flag(name))

    def _raw_flag(self, name):
        row = self._db.execute("SELECT value FROM flags WHERE name = ?", (name,)).fetchone()
        return row[0] if row else 0

    def _set_flag(self, name, value):
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO flags (name, value) VALUES (?, ?)", (name, int(bool(value))))

    def _time(self, name):
        with self._lock:
            row = self._db.execute("SELECT at FROM times WHERE name = ?", (name,)).fetchone()
        return row[0] if row else None

    def _set_time(self, name, at):
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO times (name, at) VALUES (?, ?)", (name, at))

    def record_crash(self, at, reason):
        with self._lock, self._db:
            self._db.execute("INSERT INTO crashes (at, reason) VALUES (?, ?)", (at, reason))
        return Crash(at=at, reason=reason)

    def crashes(self, since=None, after_mark=False):
        """The recorded crashes at or after wall-clock time `since` (all if None), oldest first;
        `after_mark`: only those recorded after the last `mark_crashes()`."""
        with self._lock:
            rows = self._db.execute(
                "SELECT at, reason FROM crashes WHERE at >= ? AND id > ? ORDER BY at, id",
                (float("-inf") if since is None else since,
                 self._raw_flag(_CRASH_MARK) if after_mark else 0)).fetchall()
        return [Crash(at=at, reason=reason) for at, reason in rows]

    def close(self):
        with self._lock:
            self._db.close()


class Supervisor:
    """Starts lazarus-ai, restarts it after crashes, and honours the owner's stop flag.

    `health()` returns True if lazarus-ai answers its health check (it must not block for long).
    `clock` measures timeouts and backoff (monotonic); `wall_clock` timestamps the crash record.
    `on_up()` is called when a started process answers `health` for the first time; `on_crash(crash)`
    after every recorded crash; `on_incident(decision)` after a crash that made an incident (with
    `incidents`, an AutoRollback). All run on the calling thread, outside the Supervisor's lock.
    """

    def __init__(self, launcher, health, state, clock=time.monotonic, wall_clock=time.time,
                 hang_seconds=60, start_seconds=180, on_up=None, on_crash=None, incidents=None, on_incident=None):
        self._launcher = launcher
        self._health = health
        self.state_store = state
        self._clock = clock
        self._wall_clock = wall_clock
        self._hang_seconds = hang_seconds
        self._start_seconds = start_seconds
        self.on_up = on_up
        self.on_crash = on_crash
        self.incidents = incidents
        self.on_incident = on_incident
        self._lock = threading.Lock()
        self._stopped = state.stopped_by_owner()
        self._halted = self._stopped and state.halted()
        self._held = False  # stopped for an automatic rollback until resume()
        self._process = None
        self._up = False
        self._crashed = False  # the last thing that happened was a crash: DOWN until the restart
        self._started_at = None
        self._last_healthy = None
        self._next_start = clock()  # start right away, unless stopped by the owner
        self._halt = threading.Event()
        self._thread = None

    # --- what the owner sees

    def state(self):
        """UP, STARTING, DOWN, STOPPED or HALTED."""
        if self._stopped:
            return HALTED if self._halted else STOPPED
        if self._up:
            return UP
        return DOWN if self._crashed and self._process is None else STARTING

    def is_up(self):
        return self.state() == UP

    def pid(self):
        """The pid of the running Lazarus (the process group's leader), or None."""
        process = self._process
        return None if process is None else getattr(process, "pid", None)

    def held(self):
        """True while Lazarus is held stopped for an automatic rollback."""
        return self._held

    def stop_by_owner(self):
        """`/stop`: persist the stop flag and end Lazarus. False if it was already stopped."""
        with self._lock:
            was_stopped = self._stopped
            self.state_store.set_stopped_by_owner(True)
            self.state_store.set_halted(False)
            self._stopped = True
            self._halted = False
            self._held = False
            self._up = False
            if self._process is not None:
                self._end_process("stopped by the owner")
        if not was_stopped:
            log.info("Lazarus stopped by the owner")
        return not was_stopped

    def restart_by_owner(self):
        """`/restart`: clear the stop flag and (re)start Lazarus now, without backoff.

        Returns True if a running Lazarus was restarted, False if it was started from stopped or down.
        """
        with self._lock:
            self.state_store.set_stopped_by_owner(False)
            self.state_store.set_halted(False)
            self._stopped = False
            self._halted = False
            self._held = False
            if self.incidents is not None:
                self.incidents.reset()
            was_running = self._process is not None
            if was_running:
                self._end_process("restarted by the owner")
            event = self._start()
        log.info("Lazarus %s by the owner", "restarted" if was_running else "started")
        self._notify_crash(event)
        return was_running

    def resume(self):
        """End the hold of an automatic rollback and start Lazarus now. False if it wasn't held
        (the owner took over meanwhile with /stop or /restart)."""
        with self._lock:
            if not self._held or self._stopped:
                return False
            self._held = False
            self._note("resumed after the automatic rollback")
            event = self._start()
        log.info("Lazarus resumed after the automatic rollback")
        self._notify_crash(event)
        return True

    def stop_after_incident(self, why):
        """Stop Lazarus with the persisted stop flag (state HALTED) after an incident; `why` goes to its log."""
        with self._lock:
            self._halt_locked(why)

    # --- the check loop

    def tick(self):
        """One check: start Lazarus when it's due, detect an exit or a hang, restart after a crash."""
        event = None
        with self._lock:
            if self._stopped or self._held or self._halt.is_set():
                return
            if self._process is None:
                if self._clock() >= self._next_start:
                    event = self._start()
                process = None
            else:
                process = self._process
                code = process.poll()
                if code is not None:
                    event = self._crash("exited with code {}".format(code))
                    process = None
        if process is None:
            self._notify_crash(event)
            return

        healthy = self._check_health()  # outside the lock: it may take a few seconds

        became_up = False
        with self._lock:
            if self._process is not process or self._stopped:
                return  # stopped or restarted meanwhile
            now = self._clock()
            if healthy:
                self._last_healthy = now
                became_up = not self._up
                self._up = True
            elif self._last_healthy is None:
                if now - self._started_at >= self._start_seconds:
                    event = self._crash("no health answer within {} s of starting".format(self._start_seconds))
            elif now - self._last_healthy >= self._hang_seconds:
                event = self._crash("no health answer for {} s".format(self._hang_seconds))
        if became_up:
            log.info("Lazarus is up")
            if self.on_up is not None:
                self.on_up()
        self._notify_crash(event)

    def run_in_background(self, interval=CHECK_INTERVAL):
        self._thread = threading.Thread(target=self._run, args=(interval,), name="supervisor", daemon=True)
        self._thread.start()

    def shutdown(self, timeout=15):
        """Stop checking and end Lazarus (the backend is stopping); the stop flag stays as it is."""
        self._halt.set()
        if self._thread is not None:
            self._thread.join(timeout)
        with self._lock:
            if self._process is not None:
                self._end_process("the backend is stopping")
            self._up = False

    def _run(self, interval):
        while not self._halt.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("supervisor check failed")
            self._halt.wait(interval)

    # --- internals, called with the lock held

    def _start(self):
        """Start the process; a _CrashEvent if the launcher fails, else None."""
        self._up = False
        self._last_healthy = None
        self._started_at = self._clock()
        try:
            self._process = self._launcher.start()
        except Exception as e:
            self._process = None
            return self._crash("couldn't start: {}".format(e))
        self._crashed = False
        log.info("Lazarus started (pid %s)", getattr(self._process, "pid", "?"))
        return None

    def _crash(self, reason):
        """Record a crash, end what's left of the process group and check for an incident; then schedule
        the restart, hold Lazarus for an automatic rollback, or stop it. A _CrashEvent."""
        if self._process is not None:
            self._end_process(None)
        self._up = False
        self._crashed = True
        crash = self.state_store.record_crash(self._wall_clock(), reason)
        decision = None
        if self.incidents is not None:
            try:
                decision = self.incidents.on_crash(crash)
            except Exception:
                log.exception("incident check failed")
        if decision is None:
            recent = len(self.state_store.crashes(since=crash.at - BACKOFF_WINDOW))
            delay = min(INITIAL_BACKOFF * 2 ** min(max(recent - 1, 0), 16), MAX_BACKOFF)
            self._next_start = self._clock() + delay
            self._note("crash: {}; restarting in {} s".format(reason, delay))
            log.warning("Lazarus crashed (%s), restarting in %s s", reason, delay)
        elif decision.action == auto_rollback.ROLL_BACK:
            self._held = True
            self._note("crash: {}; incident ({} crashes within {} min): rolling back to {}".format(
                reason, len(decision.crashes), _minutes(decision.window_seconds), decision.target.sha[:7]))
            log.warning("Lazarus crashed (%s): incident, rolling back to %s", reason, decision.target.sha[:7])
        else:
            self._note("crash: {}; incident ({} crashes within {} min)".format(
                reason, len(decision.crashes), _minutes(decision.window_seconds)))
            self._halt_locked("stopped after the incident ({})".format(decision.reason))
        return _CrashEvent(crash, decision)

    def _halt_locked(self, why):
        self.state_store.set_stopped_by_owner(True)
        self.state_store.set_halted(True)
        self._stopped = True
        self._halted = True
        self._held = False
        self._up = False
        if self._process is not None:
            self._end_process(None)
        self._note(why)
        log.warning("Lazarus %s", why)

    def _end_process(self, why):
        process, self._process = self._process, None
        self._last_healthy = None
        try:
            process.stop()
        except Exception:
            log.exception("couldn't stop Lazarus's process group")
        if why:
            self._note(why)

    def _note(self, text):
        try:
            self._launcher.note(text)
        except Exception as e:
            log.debug("couldn't write to Lazarus's log: %s", e)

    def _check_health(self):
        try:
            return bool(self._health())
        except Exception:
            return False

    def _notify_crash(self, event):
        if event is None:
            return
        if self.on_crash is not None:
            try:
                self.on_crash(event.crash)
            except Exception:
                log.exception("on_crash failed")
        if event.decision is not None and self.on_incident is not None:
            try:
                self.on_incident(event.decision)
            except Exception:
                log.exception("on_incident failed")


def _minutes(seconds):
    return "{:g}".format(seconds / 60)
