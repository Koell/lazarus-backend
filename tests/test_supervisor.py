"""Supervisor: restart on exit, hang detection, the owner's stop flag and the crash record, with a fake
clock and a fake launcher."""

import os
import shutil
import tempfile
import unittest

from lazarus_backend.supervisor import DOWN, STARTING, STOPPED, UP, Crash, Supervisor, SupervisorState
from tests.fakes import FakeClock, FakeLauncher, quiet_logs


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)
        self.clock = FakeClock()
        self.launcher = FakeLauncher()
        self.healthy = True
        self.ups = 0
        self.crashes = []
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db = os.path.join(self.dir, "data", "supervisor.db")

    def supervisor(self, path=None):
        state = SupervisorState(path or self.db)
        self.addCleanup(state.close)
        supervisor = Supervisor(self.launcher, lambda: self.healthy, state, clock=self.clock,
                                wall_clock=self.clock.wall, hang_seconds=60, start_seconds=180,
                                on_up=self._on_up, on_crash=self.crashes.append)
        return supervisor

    def _on_up(self):
        self.ups += 1

    def up_supervisor(self):
        supervisor = self.supervisor()
        supervisor.tick()  # starts
        supervisor.tick()  # healthy
        self.assertEqual(UP, supervisor.state())
        return supervisor

    def test_starts_lazarus_and_reports_up_once_it_answers_health(self):
        supervisor = self.supervisor()
        self.healthy = False
        self.assertEqual(STARTING, supervisor.state())
        supervisor.tick()
        self.assertEqual(1, len(self.launcher.running))
        supervisor.tick()
        self.assertEqual(STARTING, supervisor.state())
        self.healthy = True
        supervisor.tick()
        supervisor.tick()
        self.assertEqual(UP, supervisor.state())
        self.assertEqual(1, self.ups)
        self.assertEqual(1, len(self.launcher.processes))

    def test_restarts_after_the_process_exits(self):
        supervisor = self.up_supervisor()
        first = self.launcher.processes[0]
        first.exit(1)

        supervisor.tick()
        self.assertEqual(DOWN, supervisor.state())
        self.assertTrue(first.stopped, "what's left of the process group is ended")
        self.assertEqual([], self.launcher.running)
        self.assertEqual(["exited with code 1"], [c.reason for c in self.crashes])

        self.clock.advance(0.5)
        supervisor.tick()
        self.assertEqual([], self.launcher.running, "still in the backoff")
        self.clock.advance(0.5)
        supervisor.tick()
        self.assertEqual(2, len(self.launcher.processes))
        self.assertEqual(1, len(self.launcher.running))
        supervisor.tick()
        self.assertEqual(UP, supervisor.state())
        self.assertEqual(2, self.ups)

    def test_starting_lasts_from_the_start_until_the_first_healthy_answer(self):
        supervisor = self.supervisor()
        self.healthy = False
        self.assertEqual(STARTING, supervisor.state(), "about to start at the backend's start")
        supervisor.tick()
        for _ in range(5):
            self.clock.advance(30)
            supervisor.tick()
            self.assertEqual(STARTING, supervisor.state())
        self.assertFalse(supervisor.is_up())
        self.healthy = True
        supervisor.tick()
        self.assertEqual(UP, supervisor.state())

    def test_a_crash_shows_down_during_the_backoff_and_starting_once_relaunched(self):
        supervisor = self.up_supervisor()
        self.healthy = False
        self.launcher.running[0].exit(1)
        supervisor.tick()
        self.assertEqual(DOWN, supervisor.state())
        self.clock.advance(0.5)
        supervisor.tick()
        self.assertEqual(DOWN, supervisor.state(), "still in the backoff")
        self.clock.advance(0.5)
        supervisor.tick()
        self.assertEqual(1, len(self.launcher.running))
        self.assertEqual(STARTING, supervisor.state())
        self.healthy = True
        supervisor.tick()
        self.assertEqual(UP, supervisor.state())

    def test_a_failing_launch_shows_down_and_restart_by_owner_shows_starting(self):
        supervisor = self.supervisor()
        self.launcher.fail = "no such file"
        supervisor.tick()
        self.assertEqual(DOWN, supervisor.state())
        self.launcher.fail = None
        self.healthy = False
        supervisor.restart_by_owner()
        self.assertEqual(STARTING, supervisor.state())
        supervisor.stop_by_owner()
        supervisor.restart_by_owner()
        self.assertEqual(STARTING, supervisor.state())

    def test_backoff_doubles_with_recent_crashes_and_is_capped(self):
        supervisor = self.up_supervisor()
        waits = []
        for _ in range(7):
            self.launcher.running[0].exit(1)
            supervisor.tick()
            started = len(self.launcher.processes)
            waited = 0
            while len(self.launcher.processes) == started:
                self.clock.advance(1)
                waited += 1
                supervisor.tick()
            waits.append(waited)
        self.assertEqual([1, 2, 4, 8, 16, 30, 30], waits)

    def test_crashes_older_than_ten_minutes_dont_lengthen_the_backoff(self):
        supervisor = self.up_supervisor()
        self.launcher.running[0].exit(1)
        supervisor.tick()
        self.clock.advance(601)
        supervisor.tick()
        self.launcher.running[0].exit(1)
        supervisor.tick()
        self.clock.advance(1)
        supervisor.tick()
        self.assertEqual(1, len(self.launcher.running))

    def test_a_process_that_stops_answering_health_for_60_s_is_restarted(self):
        supervisor = self.up_supervisor()
        hung = self.launcher.processes[0]
        self.healthy = False
        self.clock.advance(59)
        supervisor.tick()
        self.assertEqual(UP, supervisor.state())
        self.assertFalse(hung.stopped)

        self.clock.advance(1)
        supervisor.tick()
        self.assertTrue(hung.stopped)
        self.assertEqual(["no health answer for 60 s"], [c.reason for c in self.crashes])
        self.assertEqual(DOWN, supervisor.state())

        self.healthy = True
        self.clock.advance(1)
        supervisor.tick()
        supervisor.tick()
        self.assertEqual(UP, supervisor.state())
        self.assertEqual(2, len(self.launcher.processes))

    def test_one_healthy_answer_resets_the_hang_timer(self):
        supervisor = self.up_supervisor()
        self.healthy = False
        self.clock.advance(50)
        supervisor.tick()
        self.healthy = True
        supervisor.tick()
        self.healthy = False
        self.clock.advance(50)
        supervisor.tick()
        self.assertEqual([], self.crashes)

    def test_a_start_that_never_answers_health_is_a_crash_after_start_seconds(self):
        supervisor = self.supervisor()
        self.healthy = False
        supervisor.tick()
        self.clock.advance(179)
        supervisor.tick()
        self.assertEqual([], self.crashes)
        self.clock.advance(1)
        supervisor.tick()
        self.assertEqual(["no health answer within 180 s of starting"], [c.reason for c in self.crashes])
        self.assertTrue(self.launcher.processes[0].stopped)

    def test_a_failing_launch_is_a_crash_and_is_retried(self):
        supervisor = self.supervisor()
        self.launcher.fail = "no such file"
        supervisor.tick()
        self.assertEqual(["couldn't start: no such file"], [c.reason for c in self.crashes])
        self.launcher.fail = None
        self.clock.advance(1)
        supervisor.tick()
        self.assertEqual(1, len(self.launcher.running))

    def test_crashes_are_recorded_with_their_time_and_survive_a_new_supervisor(self):
        supervisor = self.up_supervisor()
        first_crash_at = self.clock.wall()
        self.launcher.running[0].exit(1)
        supervisor.tick()
        self.clock.advance(30)
        supervisor.tick()
        supervisor.tick()
        self.healthy = False
        self.clock.advance(60)
        supervisor.tick()
        second_crash_at = self.clock.wall()

        expected = [Crash(at=first_crash_at, reason="exited with code 1"),
                    Crash(at=second_crash_at, reason="no health answer for 60 s")]
        self.assertEqual(expected, self.crashes)
        reopened = SupervisorState(self.db)
        self.addCleanup(reopened.close)
        self.assertEqual(expected, reopened.crashes())
        self.assertEqual(expected[1:], reopened.crashes(since=second_crash_at))
        self.assertIn("crash: exited with code 1; restarting in 1 s", self.launcher.notes)

    def test_stop_keeps_lazarus_stopped_across_a_new_supervisor_until_restart(self):
        supervisor = self.up_supervisor()
        self.assertTrue(supervisor.stop_by_owner())
        self.assertTrue(self.launcher.processes[0].stopped)
        self.assertEqual(STOPPED, supervisor.state())
        self.clock.advance(3600)
        supervisor.tick()
        self.assertEqual([], self.launcher.running)
        self.assertFalse(supervisor.stop_by_owner(), "already stopped")
        self.assertEqual([], self.crashes, "a stop is no crash")

        # The backend restarts (e.g. a phone reboot): the new Supervisor honours the flag.
        supervisor.shutdown()
        after_reboot = self.supervisor()
        self.assertEqual(STOPPED, after_reboot.state())
        after_reboot.tick()
        self.assertEqual([], self.launcher.running)

        self.assertFalse(after_reboot.restart_by_owner(), "started from stopped")
        self.assertEqual(1, len(self.launcher.running))
        after_reboot.tick()
        self.assertEqual(UP, after_reboot.state())

        # Cleared persistently too.
        after_reboot.shutdown()
        self.assertEqual(STARTING, self.supervisor().state())

    def test_restart_of_a_running_lazarus_starts_a_new_process_without_a_crash(self):
        supervisor = self.up_supervisor()
        self.assertTrue(supervisor.restart_by_owner())
        self.assertTrue(self.launcher.processes[0].stopped)
        self.assertEqual(2, len(self.launcher.processes))
        self.assertEqual(1, len(self.launcher.running))
        self.assertEqual([], self.crashes)

    def test_restart_skips_the_backoff(self):
        supervisor = self.up_supervisor()
        for _ in range(5):
            self.launcher.running[0].exit(1)
            supervisor.tick()
            self.clock.advance(30)
            supervisor.tick()
        self.launcher.running[0].exit(1)
        supervisor.tick()
        self.assertEqual([], self.launcher.running)
        supervisor.restart_by_owner()
        self.assertEqual(1, len(self.launcher.running))

    def test_shutdown_ends_lazarus_but_doesnt_set_the_stop_flag(self):
        supervisor = self.up_supervisor()
        supervisor.shutdown()
        self.assertTrue(self.launcher.processes[0].stopped)
        supervisor.tick()
        self.assertEqual([], self.launcher.running)
        self.assertEqual(STARTING, self.supervisor().state())
        self.assertEqual([], self.crashes)

    def test_memory_state_works_for_tests(self):
        supervisor = self.supervisor(path=":memory:")
        supervisor.tick()
        self.assertEqual(1, len(self.launcher.running))


if __name__ == "__main__":
    unittest.main()
