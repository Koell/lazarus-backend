"""ProcessLauncher: starts lazarus-ai as its own process group and captures its output into a log.

The command comes from the config (`lazarus_command`) and runs in the backend's folder. Its first
element may be PYTHON ("{python}"), which stands for the Python running the backend. The default
("auto", built by `config.build_lazarus_command`) runs `scripts/run_lazarus.py`, which starts
llama-server and lazarus-ai and ends both when one of them dies; on the phone the command wraps that
in the Libertine container (see the README).

stdout and stderr of the whole group go into one log file (`lazarus_log`). The backend owns the file,
so Lazarus can't rewrite what `/log` shows. Before each start a log over `max_log_bytes` is moved to
`<log>.1`. `note(text)` adds the backend's own lines (start, crash, stop) to the same file.

Stopping ends the whole group: on Linux SIGTERM to the process group, SIGKILL after `stop_timeout`;
on Windows `taskkill /T /F`, which reaches only children whose parent is still alive.
"""

import os
import signal
import subprocess
import sys
import time

PYTHON = "{python}"
DEFAULT_LOG_LINES = 30
MAX_LOG_BYTES = 5 * 1024 * 1024

# The backend's own folder: the launched command runs here, so relative paths in it start from here.
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ProcessLauncher:
    def __init__(self, command, log_path, cwd=BACKEND_DIR, stop_timeout=10, max_log_bytes=MAX_LOG_BYTES):
        if not command or not all(isinstance(part, str) and part for part in command):
            raise ValueError("the command must be a non-empty list of non-empty strings")
        self._command = [sys.executable if part == PYTHON else part for part in command]
        self.log_path = log_path
        self._cwd = cwd
        self._stop_timeout = stop_timeout
        self._max_log_bytes = max_log_bytes

    def start(self):
        """Start the command in a new process group; OSError if it can't be started."""
        os.makedirs(os.path.dirname(os.path.abspath(self.log_path)), exist_ok=True)
        self._rotate()
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        with open(self.log_path, "ab") as log_file:
            log_file.write(_line("starting: " + " ".join(self._command)))
            log_file.flush()
            popen = subprocess.Popen(self._command, cwd=self._cwd, env=env, stdin=subprocess.DEVNULL,
                                     stdout=log_file, stderr=subprocess.STDOUT, **_new_group())
        return LaunchedProcess(popen, self._stop_timeout)

    def note(self, text):
        """Append one of the backend's own lines to the log."""
        with open(self.log_path, "ab") as log_file:
            log_file.write(_line(text))

    def _rotate(self):
        try:
            if os.path.getsize(self.log_path) > self._max_log_bytes:
                os.replace(self.log_path, self.log_path + ".1")
        except OSError:
            pass  # no log yet, or still held open on Windows: keep appending


class LaunchedProcess:
    def __init__(self, popen, stop_timeout):
        self._popen = popen
        self._stop_timeout = stop_timeout
        self.pid = popen.pid

    def poll(self):
        """None while the group's leader runs, else its exit code."""
        return self._popen.poll()

    def stop(self):
        """End the whole process group, also what's left of it after the leader exited; the exit code."""
        if os.name == "nt":
            self._stop_windows()
        else:
            self._stop_posix()
        try:
            return self._popen.wait(self._stop_timeout)
        except subprocess.TimeoutExpired:
            return None

    def _stop_posix(self):
        group = self.pid  # start_new_session made the leader's pid the group id
        _signal_group(group, signal.SIGTERM)
        deadline = time.monotonic() + self._stop_timeout
        while time.monotonic() < deadline:
            self._popen.poll()  # reap the leader, so a zombie doesn't count as alive
            if not _signal_group(group, 0):
                return
            time.sleep(0.05)
        _signal_group(group, signal.SIGKILL)

    def _stop_windows(self):
        if self._popen.poll() is not None:
            return
        taskkill = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
        try:
            subprocess.run([taskkill, "/T", "/F", "/PID", str(self.pid)], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=self._stop_timeout)
        except (OSError, subprocess.SubprocessError):
            pass
        if self._popen.poll() is None:
            self._popen.kill()


def tail_log(path, lines=DEFAULT_LOG_LINES):
    """The last `lines` lines of the log at `path`, continued from `<path>.1` after a rotation."""
    result = _tail(path, lines)
    if len(result) < lines:
        result = _tail(path + ".1", lines - len(result)) + result
    return result


def _tail(path, lines, chunk=64 * 1024):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - chunk))
            data = f.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    result = text.splitlines()
    if size > chunk and result:
        result = result[1:]  # the first line is probably cut off
    return result[-lines:] if lines > 0 else []


def _line(text):
    return "{} [backend] {}\n".format(time.strftime("%Y-%m-%d %H:%M:%S"), text).encode("utf-8")


def _new_group():
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _signal_group(group, sig):
    """Send `sig` to the process group; False if the group no longer exists."""
    try:
        os.killpg(group, sig)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists, but isn't ours to signal
    return True
