"""Incidents and automatic rollback: the Supervisor with an AutoRollback, a fake clock and a fake launcher.

The backend's part (the restore, the owner's note) is in test_auto_rollback_wiring.py.
"""

import os
import shutil
import tempfile
import unittest

from lazarus_backend.auto_rollback import NO_REPO, NO_STABLE, REPEATED, ROLL_BACK, STOP, AutoRollback
from lazarus_backend.stability_ledger import Incident, StabilityLedger
from lazarus_backend.supervisor import DOWN, HALTED, STARTING, STOPPED, UP, Supervisor, SupervisorState
from tests.fakes import FakeClock, FakeLauncher, quiet_logs

HOUR = 3600
DAY = 24 * HOUR


class AutoRollbackTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)
        self.clock = FakeClock()
        self.launcher = FakeLauncher()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.state_path = os.path.join(self.dir, "supervisor.db")
        self.ledger = StabilityLedger(":memory:", clock=self.clock.wall)
        self.addCleanup(self.ledger.close)
        self.decisions = []

    def supervisor(self, can_roll_back=True, start=True):
        state = SupervisorState(self.state_path)
        self.addCleanup(state.close)
        incidents = AutoRollback(state, self.ledger, can_roll_back=can_roll_back)
        supervisor = Supervisor(self.launcher, lambda: True, state, clock=self.clock, wall_clock=self.clock.wall,
                                incidents=incidents, on_incident=self.decisions.append)
        if start:
            supervisor.tick()  # started
            supervisor.tick()  # healthy
            self.assertEqual(UP, supervisor.state())
        return supervisor

    def deploy(self, sha, ago=0):
        self.ledger.record_deployment(sha, "live", self.clock.wall() - ago)

    def stable_then_bad(self):
        """`stable` ran two days, then `bad` was deployed an hour ago."""
        self.deploy("stable", ago=2 * DAY + HOUR)
        self.deploy("bad", ago=HOUR)

    def crash(self, supervisor, after=0):
        """`after` seconds pass, then the running Lazarus exits and the Supervisor sees it."""
        self.clock.advance(after)
        self.launcher.running[0].exit(1)
        supervisor.tick()

    def restart(self, supervisor):
        """Wait out the backoff: the Supervisor starts Lazarus again."""
        started = len(self.launcher.processes)
        self.clock.advance(30)
        supervisor.tick()
        self.assertEqual(started + 1, len(self.launcher.processes), "restarted")

    def incident(self, supervisor, after=60):
        """Three crashes, `after` seconds apart; returns the Decision."""
        for _ in range(2):
            self.crash(supervisor, after)
            self.restart(supervisor)
        self.assertEqual([], self.decisions, "no incident before the third crash")
        self.crash(supervisor, after)
        self.assertEqual(1, len(self.decisions))
        return self.decisions.pop()

    def assert_never_restarted(self, supervisor):
        count = len(self.launcher.processes)
        for _ in range(3):
            self.clock.advance(HOUR)
            supervisor.tick()
        self.assertEqual(count, len(self.launcher.processes))
        self.assertEqual([], self.launcher.running)

    # --- incidents

    def test_the_third_crash_within_10_min_is_an_incident_and_holds_lazarus_for_the_rollback(self):
        self.stable_then_bad()
        supervisor = self.supervisor()
        self.crash(supervisor)
        self.restart(supervisor)
        self.crash(supervisor, after=300)
        self.restart(supervisor)
        self.crash(supervisor, after=210)  # 9 min 30 s after the first

        decision, = self.decisions
        self.assertEqual(ROLL_BACK, decision.action)
        self.assertEqual("stable", decision.target.sha)
        self.assertEqual("bad", decision.bad)
        self.assertEqual(3, len(decision.crashes))
        self.assertEqual([Incident("bad", decision.at)], self.ledger.incidents())
        self.assertTrue(supervisor.held())
        self.assertEqual(DOWN, supervisor.state())
        self.assert_never_restarted(supervisor)  # until the backend resumes it
        self.assertIn("incident (3 crashes within 10 min): rolling back to stable", self.launcher.notes[-1])

        self.assertTrue(supervisor.resume())
        self.assertEqual(1, len(self.launcher.running))
        self.assertFalse(supervisor.held())
        supervisor.tick()
        self.assertEqual(UP, supervisor.state())

    def test_three_crashes_spread_over_11_min_are_no_incident(self):
        self.stable_then_bad()
        supervisor = self.supervisor()
        self.crash(supervisor)
        self.restart(supervisor)
        self.crash(supervisor, after=330 - 30)
        self.restart(supervisor)
        self.crash(supervisor, after=330 - 30)  # 11 min after the first

        self.assertEqual([], self.decisions)
        self.assertEqual([], self.ledger.incidents())
        self.restart(supervisor)
        self.assertEqual(1, len(self.launcher.running))

    def test_one_rollback_per_incident_then_stop_with_the_flag_kept_across_restarts(self):
        self.stable_then_bad()
        supervisor = self.supervisor()
        self.assertEqual(ROLL_BACK, self.incident(supervisor).action)
        self.deploy("restored")  # what the backend's restore records
        supervisor.resume()
        supervisor.tick()

        # The crashes that made the incident don't count again: the restored version starts from zero.
        self.crash(supervisor)
        self.restart(supervisor)
        self.assertEqual([], self.decisions)

        self.crash(supervisor, after=60)
        self.restart(supervisor)
        self.crash(supervisor, after=60)
        decision, = self.decisions
        self.assertEqual((STOP, REPEATED, "restored"), (decision.action, decision.reason, decision.bad))
        self.assertEqual(HALTED, supervisor.state())
        self.assertFalse(supervisor.held())
        self.assert_never_restarted(supervisor)
        self.assertEqual(["bad", "restored"], [incident.sha for incident in self.ledger.incidents()])

        # The stop flag is persisted: a new backend (a reboot) keeps Lazarus stopped.
        supervisor.shutdown()
        after_reboot = self.supervisor(start=False)
        self.assertEqual(HALTED, after_reboot.state())
        self.assert_never_restarted(after_reboot)

    def test_no_stable_commit_stops_lazarus_instead_of_looping(self):
        self.deploy("only", ago=HOUR)
        supervisor = self.supervisor()
        decision = self.incident(supervisor)
        self.assertEqual((STOP, NO_STABLE, "only"), (decision.action, decision.reason, decision.bad))
        self.assertEqual(HALTED, supervisor.state())
        self.assert_never_restarted(supervisor)
        self.assertEqual(["only"], [incident.sha for incident in self.ledger.incidents()])

    def test_no_deployment_recorded_at_all_stops_lazarus(self):
        supervisor = self.supervisor()
        decision = self.incident(supervisor)
        self.assertEqual((STOP, NO_STABLE, None), (decision.action, decision.reason, decision.bad))
        self.assert_never_restarted(supervisor)

    def test_without_a_repo_an_incident_stops_lazarus(self):
        self.stable_then_bad()
        supervisor = self.supervisor(can_roll_back=False)
        decision = self.incident(supervisor)
        self.assertEqual((STOP, NO_REPO), (decision.action, decision.reason))
        self.assertEqual(HALTED, supervisor.state())
        self.assert_never_restarted(supervisor)

    def test_the_restore_failing_stops_lazarus(self):
        self.stable_then_bad()
        supervisor = self.supervisor()
        self.incident(supervisor)
        supervisor.stop_after_incident("stopped: the automatic rollback failed")
        self.assertEqual(HALTED, supervisor.state())
        self.assertFalse(supervisor.resume())
        self.assert_never_restarted(supervisor)

    # --- the owner takes over

    def test_restart_by_owner_after_the_stop_gives_a_new_automatic_rollback(self):
        self.stable_then_bad()
        supervisor = self.supervisor()
        self.incident(supervisor)
        self.deploy("restored")
        supervisor.resume()
        supervisor.tick()
        self.assertEqual(REPEATED, self.incident(supervisor).reason)

        self.assertFalse(supervisor.restart_by_owner())
        self.assertEqual(STARTING, supervisor.state())
        supervisor.tick()
        decision = self.incident(supervisor)
        self.assertEqual(ROLL_BACK, decision.action)
        self.assertEqual("stable", decision.target.sha, "restored and bad had incidents within 24 h")

    def test_restart_by_owner_starts_the_crash_count_from_zero(self):
        self.stable_then_bad()
        supervisor = self.supervisor()
        self.crash(supervisor)
        self.restart(supervisor)
        self.crash(supervisor, after=60)
        self.restart(supervisor)
        self.clock.advance(1)
        supervisor.restart_by_owner()
        supervisor.tick()
        self.crash(supervisor, after=60)
        self.restart(supervisor)
        self.crash(supervisor, after=60)
        self.assertEqual([], self.decisions)
        self.restart(supervisor)
        self.crash(supervisor, after=60)
        self.assertEqual(ROLL_BACK, self.decisions[0].action)

    def test_stop_by_owner_during_the_hold_wins_over_the_resume(self):
        self.stable_then_bad()
        supervisor = self.supervisor()
        self.incident(supervisor)
        supervisor.stop_by_owner()
        self.assertFalse(supervisor.resume())
        self.assertEqual(STOPPED, supervisor.state())
        self.assert_never_restarted(supervisor)

    def test_stop_by_owner_turns_the_incident_stop_into_the_owners(self):
        self.deploy("only", ago=HOUR)
        supervisor = self.supervisor()
        self.incident(supervisor)
        self.assertFalse(supervisor.stop_by_owner(), "already stopped")
        self.assertEqual(STOPPED, supervisor.state())

    # --- the budget

    def test_the_budget_comes_back_once_the_restored_version_ran_24_h(self):
        self.stable_then_bad()
        supervisor = self.supervisor()
        self.incident(supervisor)
        self.deploy("restored")
        supervisor.resume()
        supervisor.tick()
        self.clock.advance(DAY)
        supervisor.tick()

        decision = self.incident(supervisor)
        self.assertEqual(ROLL_BACK, decision.action)
        self.assertEqual(("stable", "restored"), (decision.target.sha, decision.bad))

    def test_the_spent_budget_survives_a_restart_of_the_backend_during_the_rollback(self):
        self.stable_then_bad()
        supervisor = self.supervisor()
        self.incident(supervisor)
        supervisor.shutdown()  # the phone reboots before the restore finished

        after_reboot = self.supervisor()  # runs the broken version again
        decision = self.incident(after_reboot)
        self.assertEqual((STOP, REPEATED), (decision.action, decision.reason))
        self.assert_never_restarted(after_reboot)


class AutoRollbackConfigTest(unittest.TestCase):
    def test_thresholds_are_configurable(self):
        state = SupervisorState(":memory:")
        self.addCleanup(state.close)
        ledger = StabilityLedger(":memory:")
        self.addCleanup(ledger.close)
        ledger.record_deployment("stable", "live", 0.0)
        ledger.record_deployment("bad", "live", 100.0)
        incidents = AutoRollback(state, ledger, crash_limit=2, window_seconds=60, stable_seconds=100)

        self.assertIsNone(incidents.on_crash(state.record_crash(200.0, "exited")))
        self.assertIsNone(incidents.on_crash(state.record_crash(261.0, "exited")), "61 s apart")
        decision = incidents.on_crash(state.record_crash(300.0, "exited"))
        self.assertEqual((ROLL_BACK, "stable", 60), (decision.action, decision.target.sha, decision.window_seconds))

    def test_thresholds_must_be_positive(self):
        for kwargs in ({"crash_limit": 0}, {"window_seconds": 0}, {"stable_seconds": -1}):
            with self.assertRaises(ValueError):
                AutoRollback(None, None, **kwargs)


if __name__ == "__main__":
    unittest.main()
