"""StatusProbe: what `/status` shows — uptime, RAM, temperature, battery, whether Lazarus answers.

It reads Linux files (`/proc`, `/sys`); their locations are injectable so tests run anywhere. A value
that can't be read is None in the Status and shows as `?`, so `/status` works on a PC as well.
"""

import glob
import os
import time
from dataclasses import dataclass

UNKNOWN = "?"
# How /status shows the Supervisor's states.
LAZARUS_STATES = {"up": "up", "starting": "starting", "down": "down (restarting)",
                  "stopped": "stopped by owner", "halted": "stopped after repeated crashes"}


@dataclass(frozen=True)
class Status:
    backend_uptime: object = None    # seconds since the backend started
    device_uptime: object = None     # seconds since the phone booted
    ram_available: object = None     # bytes
    ram_total: object = None         # bytes
    temperature: object = None       # degrees Celsius, the hottest thermal zone
    battery: object = None           # percent
    charging: object = None          # True / False
    lazarus_up: object = None        # True if lazarus-ai answers `health`
    lazarus_state: object = None     # the Supervisor's "up" / "starting" / "down" / "stopped", if it runs Lazarus
    running_commit: object = None    # short sha of the commit Lazarus runs (the latest deployment)
    running_branch: object = None
    last_push: object = None         # time.time() of the last successful push of a snapshot
    pushes_pending: object = None    # snapshot commits waiting for a push
    muted: object = None             # True after /mute
    quiet_hours: object = None       # True during quiet hours
    speaker_ready: object = None     # False if there's no text-to-speech tool


class StatusProbe:
    """`lazarus_health()` returns True if lazarus-ai answers; `started` is the backend's start on `clock`.

    With `lazarus_state()` (the Supervisor's state) given, that decides whether Lazarus is up instead of
    a health check of its own.
    """

    def __init__(self, lazarus_health, started, clock=time.monotonic, proc_dir="/proc",
                 sys_class_dir="/sys/class", lazarus_state=None, repo_status=None, speech=None):
        self._lazarus_health = lazarus_health
        self._speech = speech  # () -> dict with muted, quiet_hours, speaker_ready
        self._repo_status = repo_status  # RepoManager.status, if snapshots are on
        self._lazarus_state = lazarus_state
        self._started = started
        self._clock = clock
        self._proc = proc_dir
        self._sys_class = sys_class_dir

    def snapshot(self):
        ram_available, ram_total = self._ram()
        battery, charging = self._battery()
        return Status(backend_uptime=max(0, int(self._clock() - self._started)),
                      device_uptime=self._device_uptime(), ram_available=ram_available, ram_total=ram_total,
                      temperature=self._temperature(), battery=battery, charging=charging,
                      lazarus_up=self._lazarus_up(), lazarus_state=self._state(), **self._repo(),
                      **self._speech_status())

    def _device_uptime(self):
        text = _read(os.path.join(self._proc, "uptime"))
        try:
            return int(float(text.split()[0]))
        except (AttributeError, IndexError, ValueError):
            return None

    def _ram(self):
        text = _read(os.path.join(self._proc, "meminfo")) or ""
        values = {}
        for line in text.splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts and parts[0].isdigit():
                values[key.strip()] = int(parts[0]) * 1024  # meminfo counts in kB
        return values.get("MemAvailable"), values.get("MemTotal")

    def _temperature(self):
        temps = []
        for zone in glob.glob(os.path.join(self._sys_class, "thermal", "thermal_zone*")):
            value = _read_int(os.path.join(zone, "temp"))
            if value is None:
                continue
            celsius = value / 1000 if abs(value) >= 1000 else value  # most report millidegrees
            if -40 <= celsius <= 150:  # some zones report placeholders such as -273 or 0xFFFF
                temps.append(celsius)
        return round(max(temps), 1) if temps else None

    def _battery(self):
        supplies = sorted(glob.glob(os.path.join(self._sys_class, "power_supply", "*")))
        for supply in supplies:
            if (_read(os.path.join(supply, "type")) or "").strip().lower() != "battery":
                continue
            capacity = _read_int(os.path.join(supply, "capacity"))
            if capacity is None:
                continue
            state = (_read(os.path.join(supply, "status")) or "").strip().lower()
            charging = {"charging": True, "full": True, "discharging": False,
                        "not charging": False}.get(state)
            return capacity, charging
        return None, None

    def _repo(self):
        try:
            repo = self._repo_status() if self._repo_status is not None else None
        except Exception:
            repo = None
        if repo is None:
            return {}
        return dict(running_commit=repo.commit, running_branch=repo.branch, last_push=repo.last_push,
                    pushes_pending=repo.pending)

    def _speech_status(self):
        try:
            return dict(self._speech()) if self._speech is not None else {}
        except Exception:
            return {}

    def _state(self):
        return None if self._lazarus_state is None else self._lazarus_state()

    def _lazarus_up(self):
        if self._lazarus_state is not None:
            return self._lazarus_state() == "up"
        try:
            return bool(self._lazarus_health())
        except Exception:
            return None


def format_status(status):
    """The `/status` reply for `status`; unknown values show as `?`."""
    if status.battery is None:
        battery = UNKNOWN
    else:
        battery = "{}%".format(status.battery)
        if status.charging is not None:
            battery += " (charging)" if status.charging else " (on battery)"
    lazarus = {True: "up", False: "down (doesn't answer health)"}.get(status.lazarus_up, UNKNOWN)
    if status.lazarus_state is not None:
        lazarus = LAZARUS_STATES.get(status.lazarus_state, status.lazarus_state)
    lines = [
        "Lazarus: " + lazarus,
        "Backend up: " + _duration(status.backend_uptime),
        "Phone up: " + _duration(status.device_uptime),
        "RAM free: {} of {}".format(_size(status.ram_available), _size(status.ram_total)),
        "Temperature: " + (UNKNOWN if status.temperature is None else "{:g} °C".format(status.temperature)),
        "Battery: " + battery,
    ]
    lines += _repo_lines(status)
    if status.muted is not None:
        lines.append("Speaking: " + _speaking(status))
    return "\n".join(lines)


def _speaking(status):
    """Whether Lazarus speaks aloud now, and why not: muted first, as that's the owner's own switch."""
    if status.muted:
        return "muted (/unmute switches it back on)"
    if status.speaker_ready is False:
        return "on, but no text-to-speech tool: written here instead"
    if status.quiet_hours:
        return "on, quiet hours now: written here instead"
    return "on"


def _repo_lines(status):
    commit = UNKNOWN
    if status.running_commit is not None:
        commit = status.running_commit + (" on " + status.running_branch if status.running_branch else "")
    push = UNKNOWN if status.last_push is None else time.strftime("%Y-%m-%d %H:%M", time.localtime(status.last_push))
    if status.pushes_pending:
        push += ", {} commit{} waiting".format(status.pushes_pending, "" if status.pushes_pending == 1 else "s")
    return ["Commit: " + commit, "Last push: " + push]


def _duration(seconds):
    if seconds is None:
        return UNKNOWN
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return "{}d {}h".format(days, hours)
    if hours:
        return "{}h {}m".format(hours, minutes)
    return "{}m".format(minutes)


def _size(value):
    if value is None:
        return UNKNOWN
    return "{:.1f} GB".format(value / 1024 ** 3)


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _read_int(path):
    text = _read(path)
    try:
        return int(text.strip())
    except (AttributeError, ValueError):
        return None
