"""AutoRollback: decides after every crash whether it makes an incident and what follows from it.

An **incident** is `crash_limit` (3) crashes within `window_seconds` (10 min). Crashes up to the last
incident, or up to the owner's last `/restart` or `/rollback`, don't count again, so the version that
runs afterwards starts from zero. An incident is recorded in the StabilityLedger with the running
commit, which keeps that commit from becoming stable, and leads to one of two decisions:

- ROLL_BACK to the latest stable commit other than the running one (`ledger.latest_stable`): the
  Supervisor holds Lazarus stopped while the backend restores that commit (code only) and resumes it.
- STOP: the Supervisor stops Lazarus with the persisted stop flag and the owner gets an alert. That
  happens when the automatically restored version produces an incident too (REPEATED), when no
  stable commit exists (NO_STABLE), or when there's no repo to roll back from (NO_REPO).

**One automatic rollback per incident:** the time of the last automatic rollback is persisted, and an
incident within `stable_seconds` (24 h) after it stops Lazarus instead of rolling back again. The
budget comes back when the owner takes over (`reset`, called by the Supervisor on `/restart`, and so
also after a `/rollback`, which restarts Lazarus that way), or when the restored version has run
`stable_seconds` without an incident, which makes it stable in its own right.

The decision is made from SupervisorState (the crash record with the mark of the crashes already
settled, and the time of the last automatic rollback) and the StabilityLedger only, with times taken from the crashes, so tests
drive it with a fake clock. It does nothing itself; the Supervisor acts on the Decision.
"""

from dataclasses import dataclass

from .stability_ledger import STABLE_SECONDS

ROLL_BACK = "roll back"
STOP = "stop"

# Why an incident stops Lazarus instead of rolling it back.
REPEATED = "repeated"     # the version an automatic rollback restored produced an incident too
NO_STABLE = "no stable"   # no stable commit other than the running one
NO_REPO = "no repo"       # snapshots and rollback are off (repo_remote_url is null)

CRASH_LIMIT = 3
WINDOW_SECONDS = 600


@dataclass(frozen=True)
class Decision:
    action: str             # ROLL_BACK or STOP
    bad: object             # the commit that ran during the incident (full sha), None if unknown
    at: float               # the incident's time: the wall-clock time of its last crash
    crashes: tuple          # the Crashes that made the incident, oldest first
    window_seconds: float   # the incident window they fell into
    stable_seconds: float   # how long a commit must run without an incident to be stable
    target: object = None   # ROLL_BACK: the stable Deployment to restore
    reason: object = None   # STOP: REPEATED, NO_STABLE or NO_REPO


class AutoRollback:
    """`state` is the SupervisorState, `ledger` the StabilityLedger; `can_roll_back` False when there
    is no repo (every incident then stops Lazarus)."""

    def __init__(self, state, ledger, can_roll_back=True, crash_limit=CRASH_LIMIT, window_seconds=WINDOW_SECONDS,
                 stable_seconds=STABLE_SECONDS):
        if crash_limit < 1 or window_seconds <= 0 or stable_seconds <= 0:
            raise ValueError("crash_limit, window_seconds and stable_seconds must be positive")
        self._state = state
        self._ledger = ledger
        self._can_roll_back = can_roll_back
        self.crash_limit = crash_limit
        self.window_seconds = window_seconds
        self.stable_seconds = stable_seconds

    def on_crash(self, crash):
        """Called after `crash` was recorded: None if it makes no incident, else the Decision."""
        crashes = [c for c in self._state.crashes(since=crash.at - self.window_seconds, after_mark=True)
                   if c.at <= crash.at]
        if len(crashes) < self.crash_limit:
            return None
        self._state.mark_crashes()
        current = self._ledger.current_deployment()
        bad = current.sha if current is not None else None
        self._ledger.record_incident(bad, crash.at)

        def decision(action, **kwargs):
            return Decision(action=action, bad=bad, at=crash.at, crashes=tuple(crashes),
                            window_seconds=self.window_seconds, stable_seconds=self.stable_seconds, **kwargs)

        rolled_back_at = self._state.auto_rollback_at()
        if rolled_back_at is not None and crash.at - rolled_back_at < self.stable_seconds:
            return decision(STOP, reason=REPEATED)
        if not self._can_roll_back:
            return decision(STOP, reason=NO_REPO)
        target = self._ledger.latest_stable(other_than=bad, now=crash.at, stable_seconds=self.stable_seconds)
        if target is None:
            return decision(STOP, reason=NO_STABLE)
        # Spent now, before the restore runs: a backend restart in between can't roll back twice.
        self._state.set_auto_rollback_at(crash.at)
        return decision(ROLL_BACK, target=target)

    def reset(self):
        """The owner took over: the crashes so far count no more, and the next incident may roll back."""
        self._state.mark_crashes()
        self._state.set_auto_rollback_at(None)
