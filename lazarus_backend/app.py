"""Wiring: connects Telegram polling, UpdateRouter, the Outbox, the inbound queue, the typing indicator,
the LocalAPI and the Supervisor that keeps lazarus-ai running.

Run with `python -m lazarus_backend [--config path]`.
"""

import argparse
import http.client
import logging
import sys
import threading
import time
import urllib.error

from . import supervisor as supervision
from . import update_router
from .auto_rollback import STOP, AutoRollback
from .commands import (ALREADY_MUTED_REPLY, ALREADY_UNMUTED_REPLY, MUTE_REPLY, UNMUTE_NO_SPEAKER,
                       UNMUTE_QUIET_HOURS, UNMUTE_REPLY)
from .commands import incident_rolled_back, incident_stopped
from .commands import (ALREADY_STOPPED_REPLY, HELP_TEXT, LOG_EMPTY, LOG_HEADER, NOT_AVAILABLE, NOT_SUPERVISED,
                       RESTART_REPLY, ROLLBACK_BUSY, ROLLBACK_CANCELLED, ROLLBACK_CONFIRM, ROLLBACK_FAILED,
                       ROLLBACK_LIST_FAILED, ROLLBACK_LIST_SIZE, ROLLBACK_NO, ROLLBACK_NOT_CONFIGURED,
                       ROLLBACK_PICK_TEXT, ROLLBACK_REFUSED, ROLLBACK_STALE, ROLLBACK_STARTED, ROLLBACK_USAGE,
                       ROLLBACK_YES, START_REPLY, STOP_REPLY, describe_commit, rollback_done, rollback_label,
                       what_is_restored)
from .config import ConfigError, build_lazarus_command, load_config, load_token
from .inbound_queue import DELIVERED, QUEUED_NEW_OUTAGE, InboundMessage, InboundQueue
from .launcher import ProcessLauncher, tail_log
from .local_api import LazarusClient, LocalAPI
from .outbox import Outbox, make_item
from .repo_manager import RepoManager, RestoreRefused
from .snapshot_scheduler import SnapshotScheduler
from .speaker import QuietHours, Speaker
from .stability_ledger import StabilityLedger
from .telegram_client import TelegramClient, TelegramError
from .status_probe import StatusProbe, format_status
from .typing_indicator import TypingIndicator

# The reply to the first message of an outage; it and later ones wait in the inbound queue.
LAZARUS_DOWN_QUEUED = "Lazarus is down, message queued. It gets your messages once it's back."
LAZARUS_STOPPED_QUEUED = "Lazarus is down (stopped by you), message queued. /restart brings it back."
# /log cuts longer lines (llama-server's can be long) so the answer stays readable.
MAX_LOG_LINE = 300
# The incident note quotes this many of the last log lines.
INCIDENT_LOG_LINES = 10
SNAPSHOT_FAILED = "The nightly snapshot of Lazarus failed, nothing was committed: {}"

log = logging.getLogger("lazarus_backend")


def _refused_by_telegram(error):
    """For the Outbox: True if Telegram refused the item itself, so retrying it can't help."""
    return isinstance(error, TelegramError) and error.permanent


class Backend:
    # Waits between getUpdates attempts while offline: doubling from the first to the second.
    poll_backoff = 5
    poll_max_backoff = 60

    def __init__(self, config, telegram, lazarus, clock=time.monotonic, supervisor=None, repo=None, ledger=None,
                 speaker=None, wall_clock=time.time):
        """`supervisor` starts and watches lazarus-ai; None if it's started by hand (and in most tests).

        `repo` (a RepoManager) gets the nightly snapshot and `/rollback`; None: no snapshots and no
        rollback (most tests). `ledger` is the StabilityLedger, there also without a repo.

        `speaker` says speech items aloud (default: a Speaker for `tts_bin`, none if it's null);
        `wall_clock` is the Outbox's time, which decides the quiet hours.
        """
        self.config = config
        self.telegram = telegram
        self.lazarus = lazarus
        self.supervisor = supervisor
        self.repo = repo
        self.ledger = ledger
        self._offered = {}  # full sha -> CommitInfo of the commits /rollback showed as buttons
        self._repo_busy = threading.Lock()  # one /rollback (list or restore) at a time
        self.snapshots = None
        if repo is not None:
            self.snapshots = SnapshotScheduler(repo, at=config.snapshot_time, on_failure=self._snapshot_failed)
        self.typing = TypingIndicator(telegram.send_typing, clock=clock, max_seconds=config.typing_max_seconds)
        self.speaker = speaker if speaker is not None else Speaker.from_config(config)
        quiet_hours = QuietHours(*config.quiet_hours) if config.quiet_hours else None
        self.outbox = Outbox(self._deliver, path=config.outbox_db, clock=wall_clock, is_permanent=_refused_by_telegram,
                             speak=self.speaker.speak if self.speaker is not None else None,
                             quiet_hours=quiet_hours)
        self.inbound = InboundQueue(self._deliver_to_lazarus, path=config.inbound_db, ready=self._lazarus_ready)
        if supervisor is not None:
            supervisor.on_up = self.inbound.retry_now
            supervisor.on_incident = self._incident
        self.api =LocalAPI(config.api_host, config.api_port, on_send=self.on_send, health=self._health)
        self._clock = clock
        self._started = clock()
        self._stopped = threading.Event()
        self.status_probe = StatusProbe(lazarus.health, started=self._started, clock=clock,
                                        lazarus_state=supervisor.state if supervisor is not None else None,
                                        repo_status=repo.status if repo is not None else None,
                                        speech=self._speech_status)

    # --- lifecycle

    def start(self):
        """Start the background parts (outbox, inbound queue, typing indicator, LocalAPI, Supervisor);
        polling is separate."""
        self.outbox.start()
        self.inbound.start()
        self.typing.run_in_background()
        self.api.start()
        log.info("LocalAPI listening on %s:%s, owner id %s", *self.api.address,
                 self.config.telegram_user_id if self.config.telegram_user_id is not None else "not set (everyone is ignored)")
        if self.supervisor is not None:
            self.supervisor.run_in_background()
        if self.snapshots is not None:
            self.snapshots.start()

    def stop(self):
        self._stopped.set()
        if self.snapshots is not None:
            self.snapshots.stop()
        if self.supervisor is not None:
            self.supervisor.shutdown()
        self.api.stop()
        self.typing.shutdown()
        self.inbound.stop()
        self.outbox.stop()

    # --- incoming from Telegram

    def poll_forever(self):
        """Long-poll Telegram and handle each update; back off while offline.

        Telegram keeps the owner's messages for 24 h, so once a poll succeeds again they arrive and
        are handled like any other. The first successful poll after an outage also tells the Outbox
        to retry right away instead of waiting out its backoff.
        """
        offset = None
        backoff = self.poll_backoff
        offline = False
        while not self._stopped.is_set():
            try:
                updates = self.telegram.get_updates(offset=offset, timeout=self.config.poll_timeout)
            except Exception as e:
                # Offline or Telegram unreachable (URLError, OSError, http.client errors, a refusal).
                if not isinstance(e, (urllib.error.URLError, OSError, http.client.HTTPException,
                                      TelegramError, ValueError)):
                    log.exception("unexpected getUpdates failure")
                log.info("getUpdates failed (%s), retrying in %s s", e, backoff)
                offline = True
                self._stopped.wait(backoff)
                backoff = min(backoff * 2, self.poll_max_backoff)
                continue
            backoff = self.poll_backoff
            if offline:
                offline = False
                log.info("Telegram reachable again")
                self.outbox.retry_now()
                if self.snapshots is not None:
                    self.snapshots.retry_now()  # snapshot commits waiting for a push
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    self.handle_update(update)
                except Exception:
                    log.exception("failed to handle update %s", update.get("update_id"))

    def handle_update(self, update):
        action = update_router.route(update, self.config.telegram_user_id)
        if isinstance(action, update_router.Ignore):
            if action.reason == update_router.UNKNOWN_USER:
                log.warning("ignored message from unknown user id %s (set telegram_user_id in the config "
                            "if that's you)", action.user_id)
            else:
                log.info("ignored update %s: %s", update.get("update_id"), action.reason)
        elif isinstance(action, update_router.Reply):
            self.outbox.put(make_item(action.text))
        elif isinstance(action, update_router.ForwardText):
            self._forward_text(action)
        elif isinstance(action, update_router.Command):
            self._run_command(action)
        elif isinstance(action, update_router.RollbackButton):
            self._rollback_button(action)
        else:
            raise TypeError("unhandled action {!r}".format(action))

    def _forward_text(self, action):
        self._forward(InboundMessage(message_id=action.message_id, chat_id=action.chat_id, text=action.text,
                                     sent_at=action.sent_at))

    def _forward(self, message):
        """Hand an owner message to lazarus-ai, or queue it while Lazarus is down.

        The first message of an outage is answered with "down, message queued"; later ones queue silently.
        """
        self.typing.stop(message.message_id)  # delivery starts it again
        result = self.inbound.submit(message)
        if result == DELIVERED:
            log.info("forwarded message %s to lazarus-ai", message.message_id)
            return
        log.warning("Lazarus is down, message %s queued", message.message_id)
        if result == QUEUED_NEW_OUTAGE:
            stopped = self.supervisor is not None and self.supervisor.state() == supervision.STOPPED
            self.outbox.put(make_item(LAZARUS_STOPPED_QUEUED if stopped else LAZARUS_DOWN_QUEUED,
                                      in_reply_to=str(message.message_id)))

    def _deliver_to_lazarus(self, message):
        """Called by the inbound queue, oldest message first; raises if lazarus-ai didn't take it."""
        self.typing.start(message.message_id, message.chat_id)
        try:
            self.lazarus.post_message(message.message_id, message.text, message.sent_at)
        except Exception:
            self.typing.stop(message.message_id)
            raise

    def _lazarus_ready(self):
        """Whether to try lazarus-ai at all: not while the Supervisor has it stopped or restarting."""
        return self.supervisor is None or self.supervisor.is_up()

    def _run_command(self, action):
        """Answer a backend command; it never involves lazarus-ai except for the `/status` health check."""
        log.info("command /%s %s", action.name, " ".join(action.args))
        if action.name in ("help", "start"):
            text = HELP_TEXT
        elif action.name == "status":
            text = format_status(self.status_probe.snapshot())
        elif action.name in ("stop", "restart", "log"):
            text = self._lifecycle_command(action.name)
        elif action.name == "rollback":
            text = self._rollback_command(action)
        elif action.name in ("mute", "unmute"):
            text = self._mute_command(action.name == "mute")
        else:
            text = NOT_AVAILABLE.format(action.name)
        if text is not None:
            self.outbox.put(make_item(text))

    def _lifecycle_command(self, name):
        """/stop, /restart and /log, which need the Supervisor."""
        if self.supervisor is None:
            return NOT_SUPERVISED.format(name)
        if name == "stop":
            return STOP_REPLY if self.supervisor.stop_by_owner() else ALREADY_STOPPED_REPLY
        if name == "restart":
            return RESTART_REPLY if self.supervisor.restart_by_owner() else START_REPLY
        lines = self._log_tail()
        if not lines:
            return LOG_EMPTY
        return "\n".join([LOG_HEADER.format(len(lines))] + lines)

    def _log_tail(self, count=None):
        """The last lines of Lazarus's log (30 by default), each cut to MAX_LOG_LINE."""
        lines = tail_log(self.config.lazarus_log) if count is None else tail_log(self.config.lazarus_log, count)
        return [line if len(line) <= MAX_LOG_LINE else line[:MAX_LOG_LINE] + "…" for line in lines]

    # --- /mute, /unmute

    def _mute_command(self, mute):
        """Set the Outbox's mute flag (it survives restarts); the reply says if speech still goes here."""
        if self.outbox.muted() == mute:
            return ALREADY_MUTED_REPLY if mute else ALREADY_UNMUTED_REPLY
        self.outbox.set_muted(mute)
        log.info("speech %s by the owner", "muted" if mute else "unmuted")
        if mute:
            return MUTE_REPLY
        lines = [UNMUTE_REPLY]
        if self.speaker is None or not self.speaker.ready():
            lines.append(UNMUTE_NO_SPEAKER)
        elif self.outbox.in_quiet_hours():
            lines.append(UNMUTE_QUIET_HOURS.format(self.outbox.quiet_hours.describe()))
        return " ".join(lines)

    def _speech_status(self):
        """For /status: muted, quiet hours now, and whether a text-to-speech tool is there."""
        return dict(muted=self.outbox.muted(), quiet_hours=self.outbox.in_quiet_hours(),
                    speaker_ready=self.speaker is not None and self.speaker.ready())

    # --- /rollback

    def _rollback_command(self, action):
        """`/rollback` lists commits as buttons, `/rollback <sha> [memory]` restores; the reply, or None."""
        if self.repo is None:
            return ROLLBACK_NOT_CONFIGURED
        args = action.args
        if not args:
            return None if self._start_repo_task(lambda: self._offer_rollbacks(action.chat_id)) else ROLLBACK_BUSY
        if len(args) > 2 or (len(args) == 2 and args[1].lower() != "memory"):
            return ROLLBACK_USAGE
        sha, with_memory = args[0], len(args) == 2
        started = ROLLBACK_STARTED.format(sha, what_is_restored(with_memory))
        if not self._start_repo_task(lambda: self._rollback(sha, with_memory),
                                     announce=lambda: self.outbox.put(make_item(started))):
            return ROLLBACK_BUSY
        return None

    def _offer_rollbacks(self, chat_id):
        try:
            commits = self.repo.list_commits(ROLLBACK_LIST_SIZE)
        except Exception as e:
            log.warning("can't list commits for /rollback: %s", e)
            self.outbox.put(make_item(ROLLBACK_LIST_FAILED.format(e)))
            return
        for commit in commits:
            self._offered[commit.sha] = commit
        buttons = [[(rollback_label(c), update_router.rollback_button_data(update_router.ROLLBACK_PICK, c.sha))]
                   for c in commits]
        try:
            self.telegram.send_message(chat_id, ROLLBACK_PICK_TEXT, buttons=buttons)
        except Exception as e:
            # Buttons can't wait in the Outbox; the same list as text can, with the command to type.
            log.warning("can't send the /rollback buttons: %s", e)
            self.outbox.put(make_item("\n".join([ROLLBACK_PICK_TEXT] + [describe_commit(c) for c in commits]
                                                + [ROLLBACK_USAGE])))

    def _rollback_button(self, action):
        """A press on a /rollback button: pick shows the confirmation, confirm restores (code only)."""
        if action.step == update_router.ROLLBACK_CANCEL:
            self._answer_button(action)
            self._edit_buttons(action, ROLLBACK_CANCELLED)
            return
        commit = self._offered.get(action.sha) if self.repo is not None else None
        if commit is None:
            log.info("ignored a /rollback button for %s: not offered by the latest list", action.sha)
            self._answer_button(action, ROLLBACK_STALE if self.repo is not None else ROLLBACK_NOT_CONFIGURED)
            return
        if action.step == update_router.ROLLBACK_PICK:
            self._answer_button(action)
            self._edit_buttons(action, ROLLBACK_CONFIRM.format(describe_commit(commit)), [[
                (ROLLBACK_YES, update_router.rollback_button_data(update_router.ROLLBACK_CONFIRM, commit.sha)),
                (ROLLBACK_NO, update_router.rollback_button_data(update_router.ROLLBACK_CANCEL))]])
            return
        def announce():
            self._offered.clear()  # a confirmed list is used up
            self._answer_button(action)
            self._edit_buttons(action, ROLLBACK_STARTED.format(describe_commit(commit), what_is_restored(False)))
        if not self._start_repo_task(lambda: self._rollback(commit.sha, False), announce=announce):
            self._answer_button(action, ROLLBACK_BUSY)

    def _rollback(self, sha, with_memory):
        """Restore `sha` with Lazarus stopped and restarted by the Supervisor; tell the owner how it went."""
        stop = start = None
        if self.supervisor is not None:
            stop, start = self.supervisor.stop_by_owner, self.supervisor.restart_by_owner
        try:
            result = self.repo.restore(sha, with_memory=with_memory, stop_lazarus=stop, start_lazarus=start)
        except RestoreRefused as e:
            log.warning("rollback to %s refused: %s", sha, e)
            text = ROLLBACK_REFUSED.format(sha, e)
        except Exception as e:
            log.exception("rollback to %s failed", sha)
            text = ROLLBACK_FAILED.format(sha, e)
        else:
            text = rollback_done(result, restarted=self.supervisor is not None)
        self.outbox.put(make_item(text))

    # --- incidents (3 crashes within 10 min)

    def _incident(self, decision):
        """The Supervisor's `on_incident`: it already stopped Lazarus (STOP) or holds it for the rollback."""
        log_lines = self._log_tail(INCIDENT_LOG_LINES)  # now, before a restart adds to the log
        if decision.action == STOP:
            self.outbox.put(make_item(incident_stopped(decision, log_lines), priority="urgent"))
            return
        self.run_repo_task(lambda: self._auto_rollback(decision, log_lines))

    def _auto_rollback(self, decision, log_lines):
        """Restore the stable commit (code only; restore snapshots the broken state first), resume Lazarus
        and tell the owner; if that fails, stop Lazarus and alert. Waits for a running /rollback first."""
        with self._repo_busy:
            if not self.supervisor.held():
                log.info("automatic rollback to %s skipped: the owner took over", decision.target.sha[:7])
                return
            try:
                if self.repo is None:
                    raise RestoreRefused("snapshots are off (repo_remote_url is null)")
                self.repo.push_pending()  # the stable commit may still wait for its push
                result = self.repo.restore(decision.target.sha)
            except Exception as e:
                log.exception("automatic rollback to %s failed", decision.target.sha[:7])
                self.supervisor.stop_after_incident("stopped: the automatic rollback failed")
                text = incident_stopped(decision, log_lines, error=e)
            else:
                self.supervisor.resume()
                text = incident_rolled_back(decision, result, log_lines)
        self.outbox.put(make_item(text, priority="urgent"))

    def _start_repo_task(self, task, announce=None):
        """Run `task` with `run_repo_task` unless another /rollback runs; False if one does.

        `announce()` runs first, on the calling thread, so its message comes before the task's.
        """
        if not self._repo_busy.acquire(blocking=False):
            return False
        if announce is not None:
            try:
                announce()
            except Exception:
                self._repo_busy.release()
                raise

        def run():
            try:
                task()
            except Exception:
                log.exception("/rollback failed")
            finally:
                self._repo_busy.release()
        self.run_repo_task(run)
        return True

    def run_repo_task(self, task):
        """Run a slow repo operation (fetch, push) off the polling thread. Tests run it in place."""
        threading.Thread(target=task, name="rollback", daemon=True).start()

    def _answer_button(self, action, text=None):
        try:
            self.telegram.answer_callback(action.callback_id, text)
        except Exception as e:
            log.warning("can't answer a button press: %s", e)

    def _edit_buttons(self, action, text, buttons=None):
        try:
            self.telegram.edit_message(action.chat_id, action.message_id, text, buttons=buttons)
        except Exception as e:
            log.warning("can't update the /rollback message: %s", e)
            self.outbox.put(make_item(text + ("\n" + ROLLBACK_USAGE if buttons else "")))

    # --- outgoing from lazarus-ai

    def on_send(self, item):
        if item.in_reply_to is not None:
            self.typing.stop(item.in_reply_to)
        self.outbox.put(item)

    def _deliver(self, item):
        """Called by the Outbox worker, oldest item first; raising makes the Outbox retry it later.

        Speech items arrive here as text only when they aren't spoken (muted, quiet hours, no tool):
        the Outbox hands the others to the Speaker.
        """
        if self.config.telegram_user_id is None:
            log.warning("no telegram_user_id configured, dropping outgoing message")
            return
        self.telegram.send_message(self.config.telegram_user_id, item.text)

    def _snapshot_failed(self, error):
        self.outbox.put(make_item(SNAPSHOT_FAILED.format(error)))

    def _health(self):
        return {"uptime": int(self._clock() - self._started)}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m lazarus_backend", description=__doc__)
    parser.add_argument("--config", help="config file (default: config.json in the repo root)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        config = load_config(args.config)
        token = load_token(config)
        lazarus_command = build_lazarus_command(config)
    except ConfigError as e:
        log.error("%s", e)
        return 2

    telegram = TelegramClient(token, api_url=config.telegram_api_url)
    # The ledger exists also without snapshots: the Supervisor's incidents go there.
    ledger = StabilityLedger(config.ledger_db)
    repo = None
    if config.repo_remote_url:
        try:
            repo = RepoManager.from_config(config, ledger)
        except ValueError as e:
            log.error("%s", e)
            return 2
    else:
        log.info("repo_remote_url is null: no nightly snapshots, no /rollback and no automatic rollback")
    supervisor = None
    if lazarus_command is not None:
        launcher = ProcessLauncher(lazarus_command, config.lazarus_log)
        state = supervision.SupervisorState(config.supervisor_db)
        incidents = AutoRollback(state, ledger, can_roll_back=repo is not None, crash_limit=config.incident_crashes,
                                 window_seconds=config.incident_window_seconds, stable_seconds=config.stable_seconds)
        supervisor = supervision.Supervisor(
            launcher, LazarusClient(config.lazarus_url, timeout=5).health, state,
            hang_seconds=config.lazarus_hang_seconds, start_seconds=config.lazarus_start_seconds, incidents=incidents)
    else:
        log.info("lazarus_command is null: start lazarus-ai by hand")
    backend =Backend(config, telegram, LazarusClient(config.lazarus_url), supervisor=supervisor, repo=repo,
                      ledger=ledger)
    backend.start()
    poller = threading.Thread(target=backend.poll_forever, name="telegram-poll", daemon=True)
    poller.start()
    try:
        # Waiting here instead of polling in the main thread keeps Ctrl+C responsive on Windows.
        while poller.is_alive():
            poller.join(1)
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        backend.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
