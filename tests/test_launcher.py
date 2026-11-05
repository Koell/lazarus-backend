"""ProcessLauncher and scripts/run_lazarus.py with real (tiny Python) processes: output captured into the
log, the group ended on stop, and a killed Lazarus started again by the Supervisor."""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from lazarus_backend.launcher import BACKEND_DIR, PYTHON, ProcessLauncher, tail_log
from lazarus_backend.supervisor import Supervisor, SupervisorState
from tests.fakes import FakeClock, RecordingServer, quiet_logs

SLEEPER = [PYTHON, "-c", "import time; print('hello from lazarus'); time.sleep(60)"]
CRASHER = [PYTHON, "-c", "import sys; sys.stderr.write('SyntaxError: boom\\n'); sys.exit(3)"]
RUN_LAZARUS = os.path.join(BACKEND_DIR, "scripts", "run_lazarus.py")


def wait_until(condition, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def alive(pid):
    """POSIX only: whether `pid` runs (a zombie waiting to be reaped counts as ended)."""
    try:
        with open("/proc/{}/stat".format(pid)) as f:
            if f.read().rsplit(")", 1)[-1].split()[0] == "Z":
                return False
    except (OSError, IndexError):
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class ProcessLauncherTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.log = os.path.join(self.dir, "logs", "lazarus.log")

    def launch(self, command):
        process = ProcessLauncher(command, self.log, cwd=self.dir, stop_timeout=5).start()
        self.addCleanup(process.stop)
        return process

    def test_output_goes_to_the_log_and_stop_ends_the_process(self):
        process = self.launch(SLEEPER)
        self.assertTrue(wait_until(lambda: "hello from lazarus" in tail_log(self.log)))
        self.assertIsNone(process.poll())
        process.stop()
        self.assertIsNotNone(process.poll())

    def test_log_shows_the_error_of_a_crashed_process(self):
        process = self.launch(CRASHER)
        self.assertTrue(wait_until(lambda: process.poll() is not None))
        self.assertEqual(3, process.poll())
        self.assertEqual("SyntaxError: boom", tail_log(self.log)[-1])
        self.assertIn("[backend] starting: ", tail_log(self.log)[0])

    @unittest.skipIf(os.name == "nt", "process groups are POSIX; Windows uses taskkill /T")
    def test_stop_ends_the_whole_group_also_after_the_leader_died(self):
        # The leader starts a grandchild (think llama-server), prints its pid and exits.
        script = ("import subprocess, sys; "
                  "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                  "print('grandchild', child.pid, flush=True)")
        process = self.launch([PYTHON, "-c", script])
        self.assertTrue(wait_until(lambda: process.poll() is not None))
        line = [l for l in tail_log(self.log) if l.startswith("grandchild ")][0]
        grandchild = int(line.split()[1])
        self.assertTrue(alive(grandchild))
        process.stop()
        self.assertTrue(wait_until(lambda: not alive(grandchild)))

    def test_note_and_rotation(self):
        quiet = [PYTHON, "-c", "import time; time.sleep(60)"]
        launcher = ProcessLauncher(quiet, self.log, cwd=self.dir, max_log_bytes=10)
        os.makedirs(os.path.dirname(self.log))
        with open(self.log, "w", encoding="utf-8") as f:
            f.write("".join("old {}\n".format(i) for i in range(5)))
        launcher.note("crash: exited with code 1")
        process = launcher.start()
        self.addCleanup(process.stop)
        self.assertTrue(os.path.exists(self.log + ".1"))
        lines = tail_log(self.log, lines=3)  # continued from the rotated file
        self.assertEqual("old 4", lines[0])
        self.assertTrue(lines[1].endswith("[backend] crash: exited with code 1"))
        self.assertIn("[backend] starting: ", lines[2])

    def test_a_killed_lazarus_is_started_again_by_the_supervisor(self):
        clock = FakeClock()
        state = SupervisorState(":memory:")
        self.addCleanup(state.close)
        launcher = ProcessLauncher(SLEEPER, self.log, cwd=self.dir, stop_timeout=5)
        supervisor = Supervisor(launcher, lambda: True, state, clock=clock, wall_clock=clock.wall)
        self.addCleanup(supervisor.shutdown)
        supervisor.tick()
        supervisor.tick()
        first = supervisor.pid()

        os.kill(first, signal.SIGTERM)  # on Windows: TerminateProcess
        self.assertTrue(wait_until(lambda: (supervisor.tick() or True) and supervisor.state() == "down"))
        self.assertEqual(1, len(state.crashes()))
        clock.advance(1)
        supervisor.tick()
        supervisor.tick()
        self.assertEqual("up", supervisor.state())
        self.assertNotEqual(first, supervisor.pid())
        self.assertIsNotNone(supervisor.pid())


class RunLazarusScriptTest(unittest.TestCase):
    """scripts/run_lazarus.py ends llama-server when lazarus-ai dies, and exits with lazarus-ai's code."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        package = os.path.join(self.dir, "lazarus_ai")
        os.makedirs(package)
        open(os.path.join(package, "__init__.py"), "w").close()
        with open(os.path.join(package, "__main__.py"), "w", encoding="utf-8") as f:
            f.write("import sys\nprint('lazarus-ai stub', sys.argv[1:])\nsys.exit(3)\n")
        self.llama_health = RecordingServer(lambda path, body: (200, {"status": "ok"}))
        self.addCleanup(self.llama_health.close)

    def run_script(self, *args):
        return subprocess.run([sys.executable, RUN_LAZARUS, "--lazarus-dir", self.dir] + list(args),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)

    def test_exits_with_lazarus_ais_code(self):
        result = self.run_script()
        self.assertEqual(3, result.returncode)
        self.assertIn(b"lazarus-ai stub []", result.stdout)

    def test_lazarus_config_is_passed_to_lazarus_ai(self):
        result = self.run_script("--lazarus-config", "config.local.json")
        self.assertEqual(3, result.returncode)
        self.assertIn(b"lazarus-ai stub ['--config', 'config.local.json']", result.stdout)

    def test_waits_for_llama_server_and_ends_it_when_lazarus_ai_dies(self):
        started = time.monotonic()
        result = self.run_script("--llama-health", self.llama_health.url + "/health", "--",
                                 sys.executable, "-c", "import time; time.sleep(60)")
        self.assertEqual(3, result.returncode)
        self.assertLess(time.monotonic() - started, 20, "llama-server was ended, not waited for")
        self.assertIn(b"llama-server is ready", result.stdout)

    def test_llama_log_goes_into_its_own_file_whose_folder_is_created(self):
        loading = RecordingServer(lambda path, body: (503, {"error": "Loading model"}))
        self.addCleanup(loading.close)
        llama_log = os.path.join(self.dir, "logs", "llama-server.log")
        result = self.run_script("--llama-health", loading.url + "/health", "--llama-log", llama_log,
                                 "--", sys.executable, "-c", "print('llama ' + 'chatter')")
        self.assertEqual(1, result.returncode)
        self.assertNotIn(b"llama chatter", result.stdout)
        with open(llama_log, "rb") as f:
            self.assertIn(b"llama chatter", f.read())

    def test_exits_when_llama_server_dies_before_it_is_ready(self):
        loading = RecordingServer(lambda path, body: (503, {"error": "Loading model"}))
        self.addCleanup(loading.close)
        result = self.run_script("--llama-health", loading.url + "/health", "--",
                                 sys.executable, "-c", "import sys; sys.exit(7)")
        self.assertEqual(1, result.returncode)
        self.assertIn(b"exited with code 7 before it was ready", result.stdout)
        self.assertNotIn(b"lazarus-ai stub", result.stdout)


if __name__ == "__main__":
    unittest.main()
