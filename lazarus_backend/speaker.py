"""Speaker: says speech items aloud with a root-owned text-to-speech tool, and the quiet hours.

The tool (default `espeak-ng -v en --stdin`) is a name on the backend's PATH or a path, never a program
from Lazarus's folder: root never runs code that Lazarus can write. The text goes in on stdin, so it
can't be taken for an option. If the tool can't be found (usual on a PC) or fails, `speak` returns
False and the Outbox sends the item to Telegram as text instead.

Whether speaking is allowed at all (`/mute`, quiet hours) is the Outbox's decision; QuietHours is the
pure part of it.
"""

import datetime
import logging
import os
import shutil
import subprocess

from .snapshot_scheduler import parse_time

DEFAULT_TTS_BIN = "espeak-ng"
DEFAULT_TTS_ARGS = ("-v", "en", "--stdin")
# A tool that hangs longer than this is ended, and the item goes to Telegram.
SPEAK_TIMEOUT = 120

log = logging.getLogger(__name__)


class QuietHours:
    """A daily window in local time, `start` included and `end` excluded; it may cross midnight.

    `QuietHours("22:00", "07:00")` holds 22:00 to 06:59. `start` and `end` are "HH:MM" strings.
    """

    def __init__(self, start, end):
        self.start = parse_time(start)
        self.end = parse_time(end)
        if self.start == self.end:
            raise ValueError("quiet hours start and end at the same time ({}); use null for none".format(start))

    def contains(self, time_of_day):
        """True if `time_of_day` (a datetime.time) is within the quiet hours."""
        if self.start < self.end:
            return self.start <= time_of_day < self.end
        return time_of_day >= self.start or time_of_day < self.end

    def contains_timestamp(self, timestamp):
        """True if wall-clock `timestamp` (time.time()) is within the quiet hours, in local time."""
        return self.contains(datetime.datetime.fromtimestamp(timestamp).time())

    def describe(self):
        return "{:%H:%M}–{:%H:%M}".format(self.start, self.end)


class Speaker:
    """Runs `binary args…` with the text on stdin; `speak(text)` is True once it's been said.

    `forbidden_dir` is Lazarus's folder: a tool resolved to a file inside it is refused. `run`, `which`
    and `isfile` are injectable for tests, which never run a real tool.
    """

    def __init__(self, binary=DEFAULT_TTS_BIN, args=DEFAULT_TTS_ARGS, forbidden_dir=None, run=subprocess.run,
                 which=shutil.which, isfile=os.path.isfile, timeout=SPEAK_TIMEOUT):
        self.binary = binary
        self.args = list(args)
        self._forbidden = os.path.realpath(forbidden_dir) if forbidden_dir else None
        self._run = run
        self._which = which
        self._isfile = isfile
        self._timeout = timeout
        self._warned = False  # the "can't find the tool" warning, once until it's found again

    @classmethod
    def from_config(cls, config):
        """The Speaker for `config`, or None if `tts_bin` is null (speech always goes to Telegram)."""
        if config.tts_bin is None:
            return None
        return cls(config.tts_bin, config.tts_args, forbidden_dir=config.lazarus_folder)

    def ready(self):
        """True if the tool can be found (and isn't in Lazarus's folder)."""
        return self._resolve() is not None

    def speak(self, text):
        """Say `text`; False if the tool is missing or failed (logged), so the caller writes it instead."""
        program = self._resolve()
        if program is None:
            return False
        try:
            result = self._run([program] + self.args, input=text.encode("utf-8"), stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, timeout=self._timeout)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("text-to-speech failed (%s), sending the speech item to Telegram", e)
            return False
        if result.returncode != 0:
            error = (result.stderr or b"").decode("utf-8", "replace").strip()
            log.warning("text-to-speech exited with %s (%s), sending the speech item to Telegram",
                        result.returncode, error or "no output")
            return False
        return True

    def _resolve(self):
        """The tool's full path, or None (warned once) if it can't be found or is in Lazarus's folder."""
        if os.path.dirname(self.binary):
            program = self.binary if self._isfile(self.binary) else None
        else:
            program = self._which(self.binary)
        if program is None:
            self._warn("text-to-speech tool %s not found: speech items go to Telegram as text "
                       "(set tts_bin to its path, or to null)", self.binary)
            return None
        if self._forbidden is not None and _inside(os.path.realpath(program), self._forbidden):
            self._warn("text-to-speech tool %s is inside Lazarus's folder and is never run: speech items go "
                       "to Telegram as text", program)
            return None
        if self._warned:
            log.info("text-to-speech tool found again: %s", program)
            self._warned = False
        return program

    def _warn(self, message, *args):
        if not self._warned:
            log.warning(message, *args)
            self._warned = True


def _inside(path, folder):
    path, folder = os.path.normcase(path), os.path.normcase(folder)
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)
