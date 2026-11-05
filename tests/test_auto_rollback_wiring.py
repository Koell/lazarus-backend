"""Automatic rollback through the wired Backend: a real temporary Lazarus repo (RepoFixture), a Supervisor
with an AutoRollback and a launcher whose Lazarus crashes as long as its code doesn't compile."""

import os
import unittest
from unittest import mock

from lazarus_backend import supervisor as supervision
from lazarus_backend.app import Backend
from lazarus_backend.auto_rollback import AutoRollback
from lazarus_backend.commands import INCIDENT_NO_STABLE, INCIDENT_STOPPED
from lazarus_backend.config import Config
from tests.fakes import FakeClock, FakeLauncher, FakeProcess, quiet_logs, text_update
from tests.test_repo_manager import LATER, OWNER, RepoFixture, SentMessages

DAY = 24 * 3600
BROKEN = "def answer(:\n    return 42\n"


class CompilingLauncher(FakeLauncher):
    """Starts a fake Lazarus that exits with code 1 right away, with a traceback in its log, while
    `lazarus_ai/app.py` in Lazarus's folder has a syntax error; else it keeps running."""

    def __init__(self, lazarus_dir, log_path):
        super().__init__()
        self.app = os.path.join(lazarus_dir, "lazarus_ai", "app.py")
        self.log_path = log_path

    def start(self):
        process = FakeProcess(pid=100 + len(self.processes))
        self.processes.append(process)
        with open(self.app, encoding="utf-8") as f:
            source = f.read()
        try:
            compile(source, self.app, "exec")
        except SyntaxError as e:
            self._log("Traceback (most recent call last):", "  File \"lazarus_ai/app.py\", line {}".format(e.lineno),
                      "SyntaxError: {}".format(e.msg))
            process.exit(1)
        return process

    def note(self, text):
        super().note(text)
        self._log("[backend] " + text)

    def _log(self, *lines):
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write("".join(line + "\n" for line in lines))


class AutoRollbackWiringTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)
        self.repo = RepoFixture(self)
        self.manager = self.repo.manager()
        self.clock = FakeClock()
        self.log_path = os.path.join(self.repo.root, "backend", "logs", "lazarus.log")
        self.launcher = CompilingLauncher(self.repo.lazarus, self.log_path)

    def backend(self):
        state = supervision.SupervisorState(":memory:")
        self.addCleanup(state.close)
        incidents = AutoRollback(state, self.repo.ledger, can_roll_back=True)
        self.supervisor = supervision.Supervisor(self.launcher, lambda: bool(self.launcher.running), state,
                                                 clock=self.clock, wall_clock=lambda: self.repo.now,
                                                 incidents=incidents)
        config = Config(telegram_user_id=OWNER, api_port=0, outbox_db=":memory:", inbound_db=":memory:",
                        supervisor_db=":memory:", lazarus_log=self.log_path)
        backend = Backend(config, SentMessages(), lazarus=mock.Mock(), supervisor=self.supervisor, repo=self.manager,
                          ledger=self.repo.ledger)
        backend.run_repo_task = lambda task: task()  # in place instead of on a thread
        self.addCleanup(backend.outbox.stop)
        self.addCleanup(backend.inbound.stop)
        return backend

    def advance(self, seconds):
        self.clock.advance(seconds)
        self.repo.now += seconds

    def run_until_incident(self):
        """Tick the Supervisor (crashing Lazarus is restarted after its backoff) until it stops restarting."""
        for _ in range(20):
            self.supervisor.tick()
            if self.supervisor.state() == supervision.HALTED or len(self.repo.ledger.incidents()) > 0:
                return
            self.advance(1)
        self.fail("no incident")

    def test_a_broken_lazarus_is_rolled_back_to_the_stable_commit_after_its_third_crash(self):
        stable = self.manager.snapshot().commit  # runs for two days
        self.advance(2 * DAY)
        self.repo.write(self.repo.lazarus, "lazarus_ai/app.py", BROKEN)
        bad = self.manager.snapshot().commit  # the nightly snapshot of the broken code: a deployment
        self.repo.write_memory(LATER)  # what changed since: the pre-rollback snapshot saves it
        backend = self.backend()

        self.run_until_incident()

        # Three crashes, then the rollback, then Lazarus runs the stable code again.
        self.assertEqual(3, len(self.supervisor.state_store.crashes()))
        self.assertEqual("print('hello')\n", self.repo.read("lazarus_ai/app.py"))
        self.assertEqual(LATER, self.repo.memory_rows(), "code only: the memory stays")
        self.assertEqual(4, len(self.launcher.processes))
        self.assertEqual(1, len(self.launcher.running))
        self.supervisor.tick()
        self.assertEqual(supervision.UP, self.supervisor.state())

        # The broken state is a commit before the rollback commit, both pushed.
        rollback = self.repo.rev("live")
        broken, = self.repo.parents(rollback)
        self.assertNotEqual(bad, broken, "the pre-rollback snapshot")
        self.assertEqual([bad], self.repo.parents(broken))
        self.assertEqual(BROKEN, self.repo.show("lazarus_ai/app.py", broken))
        self.assertEqual("print('hello')\n", self.repo.show("lazarus_ai/app.py", rollback))
        self.assertEqual(rollback, self.repo.ledger.current_deployment().sha)
        self.assertEqual([bad], [incident.sha for incident in self.repo.ledger.incidents()])

        # The owner's urgent note names the bad, the restored and the saved commit and the error.
        note, = backend.outbox.pending()
        self.assertEqual("urgent", note.priority)
        self.assertTrue(note.text.startswith("Lazarus crashed 3 times within 10 min running {}.".format(bad[:7])),
                        note.text)
        self.assertIn("Rolled back automatically to {} (live: Snapshot ".format(stable[:7]), note.text)
        self.assertIn("Deployed as {} on live.".format(rollback[:7]), note.text)
        self.assertIn("The broken state is saved as {0}; /rollback {0} brings it back.".format(broken[:7]), note.text)
        self.assertIn("SyntaxError: ", note.text)

    def test_with_no_stable_commit_lazarus_is_stopped_with_an_alert_not_restarted_in_a_loop(self):
        self.repo.write(self.repo.lazarus, "lazarus_ai/app.py", BROKEN)
        bad = self.manager.snapshot().commit  # the only commit, and it never ran 24 h
        live = self.repo.rev("live")
        backend = self.backend()

        self.run_until_incident()

        self.assertEqual(supervision.HALTED, self.supervisor.state())
        self.assertTrue(self.supervisor.state_store.stopped_by_owner(), "the persisted stop flag")
        started = len(self.launcher.processes)
        for _ in range(10):
            self.advance(3600)
            self.supervisor.tick()
        self.assertEqual(started, len(self.launcher.processes))
        self.assertEqual(live, self.repo.rev("live"), "nothing rolled back")
        self.assertEqual(BROKEN, self.repo.read("lazarus_ai/app.py"))

        alert, = backend.outbox.pending()
        self.assertEqual("urgent", alert.priority)
        self.assertIn("running {}.".format(bad[:7]), alert.text)
        self.assertIn(INCIDENT_NO_STABLE.format(hours="24"), alert.text)
        self.assertIn(INCIDENT_STOPPED, alert.text)
        self.assertIn("SyntaxError: ", alert.text)

        backend.handle_update(text_update(1, OWNER, "/status"))
        self.assertIn("Lazarus: stopped after repeated crashes", backend.outbox.pending()[-1].text)

    def test_a_failing_restore_stops_lazarus_with_an_alert(self):
        self.manager.snapshot()
        self.advance(2 * DAY)
        self.repo.write(self.repo.lazarus, "lazarus_ai/app.py", BROKEN)
        self.manager.snapshot()
        backend = self.backend()
        self.repo.go_offline()
        self.addCleanup(self.repo.go_online)

        self.run_until_incident()

        self.assertEqual(supervision.HALTED, self.supervisor.state())
        alert, = backend.outbox.pending()
        self.assertEqual("urgent", alert.priority)
        self.assertIn("The automatic rollback to ", alert.text)
        self.assertIn("failed: the Lazarus repo can't be reached to check it", alert.text)
        self.assertIn(INCIDENT_STOPPED, alert.text)


if __name__ == "__main__":
    unittest.main()
