"""Texts for the commands the backend answers itself. UpdateRouter recognises them; app.py runs them."""

import time

from . import auto_rollback

HELP_TEXT = "\n".join([
    "Commands (they work even when Lazarus is down):",
    "/status – uptime, free RAM, temperature, battery, whether Lazarus is up",
    "/log – the last lines of Lazarus's error output",
    "/stop – stop Lazarus and keep it stopped, also across reboots",
    "/restart – start or restart Lazarus",
    "/mute – send what Lazarus would say aloud to Telegram instead",
    "/unmute – let Lazarus speak again",
    "/rollback – pick a version of main or live to run (code only)",
    "/rollback <sha> [memory] – run that commit's code, with memory also its memory",
    "/help – this list",
    "",
    "Everything else goes to Lazarus.",
])

NOT_AVAILABLE = "/{} isn't available yet."

# /stop, /restart, /log (the Supervisor).
STOP_REPLY = "Lazarus stopped. It stays stopped, also across reboots, until /restart."
ALREADY_STOPPED_REPLY = "Lazarus is already stopped. /restart brings it back."
RESTART_REPLY = "Restarting Lazarus."
START_REPLY = "Starting Lazarus."
LOG_HEADER = "The last {} lines of Lazarus's output:"
LOG_EMPTY = "Lazarus's log is empty."
NOT_SUPERVISED = "/{} isn't available: this backend doesn't start Lazarus (lazarus_command is null)."

# /mute, /unmute (the Outbox keeps the flag; the Speaker speaks).
MUTE_REPLY = "Muted. What Lazarus would say aloud comes here as text until /unmute, also across restarts."
ALREADY_MUTED_REPLY = "Already muted. /unmute lets Lazarus speak again."
UNMUTE_REPLY = "Unmuted. Lazarus speaks aloud again."
ALREADY_UNMUTED_REPLY = "Lazarus isn't muted."
UNMUTE_QUIET_HOURS = "It's quiet hours ({}), so until they end it still writes here."
UNMUTE_NO_SPEAKER = "There's no text-to-speech tool on this machine, so it still writes here (tts_bin)."

# /rollback (RepoManager.list_commits and restore).
ROLLBACK_LIST_SIZE = 5  # commits per branch
ROLLBACK_NOT_CONFIGURED = ("/rollback isn't available: snapshots are off (repo_remote_url is null), so there's no "
                           "repo to roll back from.")
ROLLBACK_USAGE = ("/rollback shows versions to pick from, /rollback <sha> runs that commit's code, "
                  "/rollback <sha> memory also brings back its memory.")
ROLLBACK_PICK_TEXT = "Which version should Lazarus run? Code only: the memory stays as it is."
ROLLBACK_LIST_FAILED = "Can't list the versions: {}"
ROLLBACK_CONFIRM = "Roll back to {}?\nCode only: the memory stays as it is."
ROLLBACK_YES = "Yes, roll back"
ROLLBACK_NO = "Cancel"
ROLLBACK_CANCELLED = "Rollback cancelled, nothing changed."
ROLLBACK_STALE = "That list is out of date. Send /rollback for a new one."
ROLLBACK_BUSY = "A rollback is already running. Its result comes in a moment."
ROLLBACK_STARTED = "Rolling back to {} ({})…"
ROLLBACK_REFUSED = "Can't roll back to {}: {}. Nothing was changed."
ROLLBACK_FAILED = "The rollback to {} failed: {}"
ROLLBACK_DONE = "Lazarus runs {target} now ({branch}: {subject}), {memory}. Deployed as {commit} on live{pushed}."
ROLLBACK_UNDO = "The state before is saved as {0}; /rollback {0} undoes this."
ROLLBACK_SKIPPED = "Not restored: {}."
ROLLBACK_NOT_RESTARTED = "Restart Lazarus by hand: this backend doesn't start it."
_BUTTON_LENGTH = 60

# Incidents (3 crashes within 10 min; the Supervisor and AutoRollback): urgent notes to the owner.
INCIDENT_HEAD = "Lazarus crashed {count} times within {minutes} min running {bad}."
INCIDENT_ROLLED_BACK = ("Rolled back automatically to {target} ({branch}: {subject}), the latest commit that ran "
                        "{hours} h without an incident; memory kept as it was. Deployed as {commit} on live{pushed}.")
INCIDENT_BROKEN_SAVED = "The broken state is saved as {0}; /rollback {0} brings it back."
INCIDENT_NEXT_TIME = "If this version crashes like that too, Lazarus is stopped instead of rolled back again."
INCIDENT_STOPPED = "Lazarus is stopped now and stays stopped, also across reboots."
INCIDENT_REPEATED = ("It runs the version an automatic rollback restored, so there's no second automatic rollback.")
INCIDENT_NO_STABLE = ("There's no stable commit to roll back to: no other commit ran {hours} h without an "
                      "incident.")
INCIDENT_NO_REPO = "There's no automatic rollback: snapshots are off (repo_remote_url is null)."
INCIDENT_ROLLBACK_FAILED = "The automatic rollback to {target} failed: {error}"
INCIDENT_WHAT_NOW = "/log shows more, /rollback picks a version to run, /restart tries this one again."
INCIDENT_LOG = "The last lines of its log:"
INCIDENT_UNKNOWN_COMMIT = "an unrecorded commit"
_INCIDENT_REASONS = {auto_rollback.REPEATED: INCIDENT_REPEATED, auto_rollback.NO_STABLE: INCIDENT_NO_STABLE,
                     auto_rollback.NO_REPO: INCIDENT_NO_REPO}


def what_is_restored(with_memory):
    return "with memory" if with_memory else "code only"


def describe_commit(commit):
    """`abc1234 (live, 2026-11-04 03:00: Snapshot …)` for a CommitInfo."""
    return "{} ({}, {}: {})".format(commit.short, commit.branch, _date(commit.date), commit.subject)


def rollback_label(commit):
    """The button text for a CommitInfo: short sha, branch, date, message, cut to fit a button."""
    label = "{} {} {} {}".format(commit.short, commit.branch, _date(commit.date), commit.subject)
    return label if len(label) <= _BUTTON_LENGTH else label[:_BUTTON_LENGTH - 1] + "…"


def rollback_done(result, restarted):
    """The owner's confirmation for a RestoreResult; `restarted` False if nobody restarts Lazarus."""
    memory = ("memory restored from {}".format(result.target[:7]) if result.with_memory
              else "memory kept as it was")
    lines = [ROLLBACK_DONE.format(target=result.target[:7], branch=result.target_branch, subject=result.subject,
                                  memory=memory, commit=result.commit[:7],
                                  pushed="" if result.pushed else " (waiting for a push)"),
             ROLLBACK_UNDO.format(result.snapshot[:7])]
    if result.skipped:
        lines.append(ROLLBACK_SKIPPED.format(", ".join(result.skipped)))
    if not restarted:
        lines.append(ROLLBACK_NOT_RESTARTED)
    return "\n".join(lines)


def incident_rolled_back(decision, result, log_lines):
    """The owner's note after an automatic rollback (a RestoreResult) for an incident (a Decision)."""
    lines = [_incident_head(decision),
             INCIDENT_ROLLED_BACK.format(target=result.target[:7], branch=result.target_branch, subject=result.subject,
                                         hours=_hours(decision.stable_seconds), commit=result.commit[:7],
                                         pushed="" if result.pushed else " (waiting for a push)"),
             INCIDENT_BROKEN_SAVED.format(result.snapshot[:7])]
    if result.skipped:
        lines.append(ROLLBACK_SKIPPED.format(", ".join(result.skipped)))
    lines.append(INCIDENT_NEXT_TIME)
    return "\n".join(lines + _log_block(log_lines))


def incident_stopped(decision, log_lines, error=None):
    """The owner's alert when an incident stopped Lazarus: for the decision's reason, or, with `error`,
    because the automatic rollback to its target failed."""
    if error is not None:
        why = INCIDENT_ROLLBACK_FAILED.format(target=decision.target.sha[:7], error=error)
    else:
        why = _INCIDENT_REASONS.get(decision.reason, decision.reason).format(hours=_hours(decision.stable_seconds))
    lines = [_incident_head(decision), why, INCIDENT_STOPPED, INCIDENT_WHAT_NOW]
    return "\n".join(lines + _log_block(log_lines))


def _incident_head(decision):
    return INCIDENT_HEAD.format(count=len(decision.crashes), minutes=_number(decision.window_seconds / 60),
                                bad=decision.bad[:7] if decision.bad else INCIDENT_UNKNOWN_COMMIT)


def _log_block(log_lines):
    return ["", INCIDENT_LOG] + list(log_lines) if log_lines else []


def _hours(seconds):
    return _number(seconds / 3600)


def _number(value):
    return "{:g}".format(round(value, 1))


def _date(seconds):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(seconds))
