"""SnapshotScheduler with a fake clock and a fake RepoManager: nightly at 03:00, pushes retried."""

import datetime
import unittest

from lazarus_backend.repo_manager import RepoError, SnapshotResult
from lazarus_backend.snapshot_scheduler import SnapshotScheduler, next_run_after, parse_time


class FakeRepo:
    def __init__(self):
        self.online = True
        self.pending = 0
        self.snapshots = 0
        self.pushes = 0
        self.fail_snapshot = False

    def snapshot(self):
        if self.fail_snapshot:
            raise RepoError("Lazarus's folder doesn't exist")
        self.snapshots += 1
        self.pending += 1
        if self.online:
            self.pending = 0
        return SnapshotResult(commit="abc", created=True, pushed=self.online)

    def push_pending(self):
        self.pushes += 1
        if self.online:
            self.pending = 0
        return self.online

    def pending_count(self):
        return self.pending


class Clock:
    def __init__(self, *start):
        self.now = datetime.datetime(*start)

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += datetime.timedelta(**delta)


class SnapshotSchedulerTest(unittest.TestCase):
    def setUp(self):
        self.repo = FakeRepo()
        self.clock = Clock(2026, 11, 5, 18, 0)
        self.failures = []
        self.scheduler = SnapshotScheduler(self.repo, at="03:00", now=self.clock, retry_seconds=600,
                                           on_failure=self.failures.append)

    def test_snapshot_runs_once_a_night_at_three(self):
        self.scheduler.tick()
        self.clock.advance(hours=8, minutes=59)  # 02:59
        self.scheduler.tick()
        self.assertEqual(0, self.repo.snapshots)

        self.clock.advance(minutes=1)  # 03:00
        self.scheduler.tick()
        self.clock.advance(seconds=30)
        self.scheduler.tick()
        self.assertEqual(1, self.repo.snapshots)
        self.assertEqual(datetime.datetime(2026, 11, 7, 3, 0), self.scheduler.next_run)

        self.clock.advance(days=1)
        self.scheduler.tick()
        self.assertEqual(2, self.repo.snapshots)

    def test_a_missed_night_runs_once_at_the_next_check(self):
        self.scheduler.tick()
        self.clock.advance(hours=14)  # 08:00 the next day: asleep at 03:00
        self.scheduler.tick()
        self.scheduler.tick()
        self.assertEqual(1, self.repo.snapshots)
        self.assertEqual(datetime.datetime(2026, 11, 7, 3, 0), self.scheduler.next_run)

    def test_offline_snapshot_is_pushed_after_the_retry_interval(self):
        self.repo.online = False
        self.clock = Clock(2026, 11, 5, 2, 59)
        self.scheduler._now = self.clock
        self.scheduler.tick()
        self.clock.advance(minutes=1)
        self.scheduler.tick()
        self.assertEqual(1, self.repo.pending)

        self.clock.advance(minutes=9)
        self.scheduler.tick()
        self.assertEqual(0, self.repo.pushes)

        self.clock.advance(minutes=1)
        self.scheduler.tick()  # still offline: tried, next try in 10 min
        self.assertEqual(1, self.repo.pushes)
        self.repo.online = True
        self.clock.advance(minutes=5)
        self.scheduler.tick()
        self.assertEqual(1, self.repo.pushes)
        self.clock.advance(minutes=5)
        self.scheduler.tick()
        self.assertEqual(2, self.repo.pushes)
        self.assertEqual(0, self.repo.pending)

        self.clock.advance(hours=1)
        self.scheduler.tick()
        self.assertEqual(2, self.repo.pushes)  # nothing waiting, nothing to do

    def test_reconnect_pushes_right_away(self):
        self.repo.online = False
        self.scheduler.tick()
        self.clock.advance(hours=9)
        self.scheduler.tick()  # 03:00 snapshot, offline
        self.repo.online = True

        self.scheduler.retry_now()
        self.scheduler.tick()

        self.assertEqual(1, self.repo.pushes)
        self.assertEqual(0, self.repo.pending)

    def test_commits_left_from_before_a_restart_are_pushed_at_the_first_check(self):
        self.repo.pending = 2
        self.scheduler.tick()
        self.assertEqual(1, self.repo.pushes)
        self.assertEqual(0, self.repo.pending)

    def test_failed_snapshot_is_reported_and_tried_again_the_next_night(self):
        self.repo.fail_snapshot = True
        self.scheduler.tick()
        self.clock.advance(hours=9)
        with self.assertLogs("lazarus_backend.snapshot_scheduler", "ERROR"):
            self.scheduler.tick()
        self.assertEqual(1, len(self.failures))
        self.assertIsInstance(self.failures[0], RepoError)

        self.repo.fail_snapshot = False
        self.clock.advance(days=1)
        self.scheduler.tick()
        self.assertEqual(1, self.repo.snapshots)


class TimeTest(unittest.TestCase):
    def test_parse_time(self):
        self.assertEqual(datetime.time(3, 0), parse_time("03:00"))
        self.assertEqual(datetime.time(23, 45), parse_time("23:45"))
        for bad in ("3", "25:00", "03:00:00", "noon", None, 3):
            with self.assertRaises(ValueError):
                parse_time(bad)

    def test_next_run_after(self):
        at = datetime.time(3, 0)
        self.assertEqual(datetime.datetime(2026, 11, 6, 3, 0),
                         next_run_after(datetime.datetime(2026, 11, 5, 3, 0), at))
        self.assertEqual(datetime.datetime(2026, 11, 5, 3, 0),
                         next_run_after(datetime.datetime(2026, 11, 5, 2, 59, 59), at))
        self.assertEqual(datetime.datetime(2026, 12, 1, 3, 0),
                         next_run_after(datetime.datetime(2026, 11, 30, 4, 0), at))


if __name__ == "__main__":
    unittest.main()
