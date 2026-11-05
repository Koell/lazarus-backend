import os
import shutil
import tempfile
import time
import unittest

from lazarus_backend.repo_manager import RepoStatus
from lazarus_backend.status_probe import Status, StatusProbe, format_status

MEMINFO = "MemTotal:        3891200 kB\nMemFree:          204800 kB\nMemAvailable:    1258291 kB\n"


class FakeLinux:
    """A temporary folder shaped like /proc and /sys/class."""

    def __init__(self, test):
        self.root = tempfile.mkdtemp()
        test.addCleanup(shutil.rmtree, self.root)
        self.proc = os.path.join(self.root, "proc")
        self.sys_class = os.path.join(self.root, "sys", "class")
        os.makedirs(self.proc)
        os.makedirs(self.sys_class)

    def write(self, relative, text):
        path = os.path.join(self.root, *relative.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)


class StatusProbeTest(unittest.TestCase):
    def setUp(self):
        self.linux = FakeLinux(self)
        self.now = 1000.0

    def probe(self, health=lambda: True):
        return StatusProbe(health, started=400.0, clock=lambda: self.now, proc_dir=self.linux.proc,
                           sys_class_dir=self.linux.sys_class)

    def test_reads_every_value_from_a_phone_like_system(self):
        self.linux.write("proc/uptime", "93784.52 180000.10\n")
        self.linux.write("proc/meminfo", MEMINFO)
        self.linux.write("sys/class/thermal/thermal_zone0/temp", "38500\n")
        self.linux.write("sys/class/thermal/thermal_zone1/temp", "41200\n")
        self.linux.write("sys/class/power_supply/usb/type", "USB\n")
        self.linux.write("sys/class/power_supply/battery/type", "Battery\n")
        self.linux.write("sys/class/power_supply/battery/capacity", "87\n")
        self.linux.write("sys/class/power_supply/battery/status", "Charging\n")

        self.assertEqual(Status(backend_uptime=600, device_uptime=93784, ram_available=1258291 * 1024,
                                ram_total=3891200 * 1024, temperature=41.2, battery=87, charging=True,
                                lazarus_up=True),
                         self.probe().snapshot())

    def test_pc_without_linux_files_gives_unknown_values_not_an_error(self):
        status = StatusProbe(lambda: False, started=400.0, clock=lambda: self.now,
                             proc_dir=os.path.join(self.linux.root, "missing"),
                             sys_class_dir=os.path.join(self.linux.root, "missing")).snapshot()
        self.assertEqual(Status(backend_uptime=600, lazarus_up=False), status)
        text = format_status(status)
        self.assertIn("Temperature: ?", text)
        self.assertIn("Battery: ?", text)
        self.assertIn("RAM free: ? of ?", text)
        self.assertIn("Lazarus: down", text)

    def test_garbage_and_placeholder_values_are_unknown(self):
        self.linux.write("proc/uptime", "not a number")
        self.linux.write("proc/meminfo", "MemTotal: lots\n")
        self.linux.write("sys/class/thermal/thermal_zone0/temp", "-273000\n")
        self.linux.write("sys/class/thermal/thermal_zone1/temp", "hot\n")
        self.linux.write("sys/class/power_supply/battery/type", "Battery\n")
        self.linux.write("sys/class/power_supply/battery/capacity", "\n")
        self.assertEqual(Status(backend_uptime=600, lazarus_up=True), self.probe().snapshot())

    def test_failing_health_check_shows_unknown(self):
        def broken():
            raise RuntimeError("boom")
        self.assertIsNone(self.probe(broken).snapshot().lazarus_up)
        self.assertIn("Lazarus: ?", format_status(self.probe(broken).snapshot()))


class FormatStatusTest(unittest.TestCase):
    def test_formats_every_value(self):
        text = format_status(Status(backend_uptime=3 * 3600 + 5 * 60, device_uptime=2 * 86400 + 4 * 3600,
                                    ram_available=int(1.5 * 1024 ** 3), ram_total=4 * 1024 ** 3,
                                    temperature=41.2, battery=87, charging=False, lazarus_up=True))
        self.assertEqual("Lazarus: up\nBackend up: 3h 5m\nPhone up: 2d 4h\nRAM free: 1.5 GB of 4.0 GB\n"
                         "Temperature: 41.2 °C\nBattery: 87% (on battery)\nCommit: ?\nLast push: ?", text)

    def test_shows_the_supervisors_states(self):
        shown = {state: format_status(Status(lazarus_state=state)).splitlines()[0]
                 for state in ("up", "starting", "down", "stopped")}
        self.assertEqual({"up": "Lazarus: up", "starting": "Lazarus: starting", "down": "Lazarus: down (restarting)",
                          "stopped": "Lazarus: stopped by owner"}, shown)

    def test_everything_unknown(self):
        self.assertEqual("Lazarus: ?\nBackend up: ?\nPhone up: ?\nRAM free: ? of ?\nTemperature: ?\nBattery: ?\n"
                         "Commit: ?\nLast push: ?", format_status(Status()))

    def test_running_commit_and_last_push(self):
        pushed = time.mktime((2026, 11, 5, 3, 0, 12, 0, 0, -1))
        text = format_status(Status(running_commit="1a2b3c4", running_branch="live", last_push=pushed,
                                    pushes_pending=0))
        self.assertTrue(text.endswith("\nCommit: 1a2b3c4 on live\nLast push: 2026-11-05 03:00"), text)

    def test_snapshots_waiting_for_a_push_are_counted(self):
        self.assertIn("Last push: ?, 1 commit waiting", format_status(Status(pushes_pending=1)))
        pushed = time.mktime((2026, 11, 4, 3, 0, 0, 0, 0, -1))
        self.assertIn("Last push: 2026-11-04 03:00, 2 commits waiting",
                      format_status(Status(last_push=pushed, pushes_pending=2)))


class RepoStatusTest(unittest.TestCase):
    def test_probe_takes_commit_and_push_from_the_repo(self):
        repo = RepoStatus(commit="1a2b3c4", branch="live", last_push=1700000000.0, pending=1)
        status = StatusProbe(lambda: True, started=0.0, clock=lambda: 60.0, repo_status=lambda: repo).snapshot()
        self.assertEqual(("1a2b3c4", "live", 1700000000.0, 1),
                         (status.running_commit, status.running_branch, status.last_push, status.pushes_pending))

    def test_failing_repo_status_shows_unknown(self):
        def broken():
            raise RuntimeError("git is gone")
        status = StatusProbe(lambda: True, started=0.0, clock=lambda: 60.0, repo_status=broken).snapshot()
        self.assertIsNone(status.running_commit)
        self.assertIn("Commit: ?", format_status(status))


if __name__ == "__main__":
    unittest.main()
