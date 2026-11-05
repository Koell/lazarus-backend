"""Config: the backend's settings, read from a JSON file over built-in defaults.

The file is optional; every key has a default. Relative paths in it are resolved against the folder
that holds the file (the repo root for the default `config.json`), and a leading `~` is the user's
home. The Telegram token never lives in the config file: it comes from the `LAZARUS_TELEGRAM_TOKEN`
environment variable or from `telegram_token_file` (default `secrets/telegram.token`).
"""

import json
import os
import shutil
from dataclasses import dataclass, fields, replace

from .snapshot_scheduler import parse_time
from .speaker import QuietHours

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_PATH = os.path.join(ROOT_DIR, "config.json")
TOKEN_ENV = "LAZARUS_TELEGRAM_TOKEN"

# Keys whose values are file system paths and get resolved against the config file's folder.
_PATH_KEYS = ("telegram_token_file", "outbox_db", "inbound_db", "supervisor_db", "lazarus_log", "lazarus_folder",
              "repo_clone_dir", "repo_deploy_key_file", "repo_known_hosts_file", "ledger_db",
              "llama_model", "llama_log")
# Program keys: resolved like paths only when they name a file (`bin/git`), not a bare `git` on PATH.
_PROGRAM_KEYS = ("git_bin", "ssh_bin", "llama_server_bin", "tts_bin")
# `lazarus_command` value that builds the command from the llama_* keys (see `build_lazarus_command`).
AUTO_COMMAND = "auto"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    # Telegram user id of the owner. None answers nobody: every sender is ignored and logged,
    # which is how the owner finds their own id during setup.
    telegram_user_id: object = None
    # The backend's LocalAPI (send, health) for lazarus-ai.
    api_host: str = "127.0.0.1"
    api_port: int = 8700
    # lazarus-ai's Inbox (message, health).
    lazarus_url: str = "http://127.0.0.1:8701"
    telegram_api_url: str = "https://api.telegram.org"
    telegram_token_file: str = "secrets/telegram.token"
    # Long-polling wait in seconds that Telegram holds a getUpdates request open.
    poll_timeout: int = 50
    # The typing indicator stops after this many seconds even without a reply.
    typing_max_seconds: int = 600
    # SQLite file of the outgoing queue (git-ignored); ":memory:" keeps it in memory only.
    outbox_db: str = "data/outbox.db"
    # SQLite file of the owner's messages that wait while Lazarus is down (git-ignored).
    inbound_db: str = "data/inbound.db"
    # The command that runs Lazarus (lazarus-ai, and llama-server if given) as one process group, as a
    # list; it runs in the backend's folder, and "{python}" stands for the backend's own Python.
    # "auto" (the default, for the PC) runs scripts/run_lazarus.py with lazarus-ai from `lazarus_folder`
    # and llama-server built from the llama_* keys below; the README has the phone's explicit command.
    # null: the backend doesn't start Lazarus, which then has to be started by hand.
    lazarus_command: object = AUTO_COMMAND
    # llama-server for the "auto" command: the binary (a name on PATH or a path) and the GGUF model.
    # Either one null: the backend starts only lazarus-ai, and llama-server is started by hand.
    llama_server_bin: object = "llama-server"
    llama_model: object = "~/models/qwen2.5-1.5b-instruct-q4_k_m.gguf"
    # It listens on 127.0.0.1:`llama_port` (lazarus-ai's `llm_url`); context size in tokens and CPU
    # threads (null: llama-server's own default); `llama_args`: further arguments as a list.
    llama_port: int = 8080
    llama_context: object = 4096
    llama_threads: object = 4
    llama_args: object = ()
    # llama-server's (chatty) output, kept out of /log (git-ignored); null: into `lazarus_log`.
    llama_log: object = "logs/llama-server.log"
    # lazarus-ai's config file for the "auto" command, relative to `lazarus_folder` (passed as --config).
    # null: lazarus-ai's tracked config.json. Set it (e.g. "config.local.json") on the PC only: on the
    # phone Lazarus must not run from a config file that snapshots and rollbacks don't track.
    lazarus_config: object = None
    # Lazarus's captured stdout and stderr, shown by /log (git-ignored).
    lazarus_log: str = "logs/lazarus.log"
    # The Supervisor's state: the owner's /stop flag and the crash record (git-ignored).
    supervisor_db: str = "data/supervisor.db"
    # A running Lazarus that doesn't answer `health` for this long counts as crashed; after a start the
    # first answer may take `lazarus_start_seconds` (llama-server loads the model first).
    lazarus_hang_seconds: int = 60
    lazarus_start_seconds: int = 180
    # An incident is `incident_crashes` crashes within `incident_window_seconds`; it rolls Lazarus back
    # (code only) to the latest commit that ran `stable_seconds` without an incident, once, or stops it.
    incident_crashes: int = 3
    incident_window_seconds: int = 600
    stable_seconds: int = 24 * 3600
    # Nightly snapshot (RepoManager): Lazarus's folder is copied into the backend's own clone of the
    # Lazarus repo, with the memory database (`lazarus_memory_db`, inside Lazarus's folder) as a SQL
    # dump, committed to `repo_branch` and pushed with the deploy key at `snapshot_time` (local time).
    lazarus_folder: str = "../lazarus-ai"
    lazarus_memory_db: str = "data/memory.db"
    repo_clone_dir: str = "data/repo"
    # null: no snapshots (a PC without the deploy key).
    repo_remote_url: object = "git@github.com:Koell/lazarus-ai.git"
    repo_branch: str = "live"
    repo_deploy_key_file: str = "secrets/deploy_key"
    # GitHub's host keys for ssh (StrictHostKeyChecking); the README says how to create it.
    repo_known_hosts_file: str = "secrets/known_hosts"
    # Root-owned git and ssh: names on the backend's PATH or paths of binaries.
    git_bin: str = "git"
    ssh_bin: str = "ssh"
    snapshot_time: str = "03:00"
    # Files bigger than this (bytes) are left out of a snapshot and listed in its commit message.
    snapshot_size_limit: int = 5 * 1024 * 1024
    # Left out of snapshots and never written by a /rollback, gitignore-like: with a slash matched from
    # the folder's root, without against every name, a trailing slash for folders only. `.git` is always
    # left out. Lazarus's config.json is tracked; config.local.json is the PC's git-ignored override.
    snapshot_excludes: object = ("/config.local.json", "*.db", "*.db-journal", "*.db-wal", "*.db-shm", "*.log",
                                 "/logs/", "__pycache__/", "*.py[cod]", ".cache/", ".pytest_cache/")
    # Deployments (which commit Lazarus runs since when) and pushes (git-ignored).
    ledger_db: str = "data/ledger.db"
    # The Speaker's root-owned text-to-speech tool (a name on PATH or a path, never in Lazarus's folder)
    # and its arguments; the text goes in on stdin. null, or a tool that can't be found: speech items go
    # to Telegram as text.
    tts_bin: object = "espeak-ng"
    tts_args: object = ("-v", "en", "--stdin")
    # Nothing is spoken from the first to the second time (local time, may cross midnight); null: never quiet.
    quiet_hours: object = ("22:00", "07:00")


def load_config(path=None):
    """Read the config file at `path` (default `config.json` next to the package), or defaults."""
    explicit = path is not None
    path = os.path.abspath(path or DEFAULT_CONFIG_PATH)
    values = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                values = json.load(f)
        except (OSError, ValueError) as e:
            raise ConfigError("can't read config {}: {}".format(path, e))
        if not isinstance(values, dict):
            raise ConfigError("config {} must hold a JSON object".format(path))
    elif explicit:
        raise ConfigError("config file {} doesn't exist".format(path))

    known = {f.name for f in fields(Config)}
    unknown = sorted(set(values) - known)
    if unknown:
        raise ConfigError("unknown config keys in {}: {}".format(path, ", ".join(unknown)))

    config = replace(Config(), **values)
    if config.telegram_user_id is not None and not isinstance(config.telegram_user_id, int):
        raise ConfigError("telegram_user_id must be a number (the owner's Telegram user id) or null")
    command = config.lazarus_command
    if command is not None and command != AUTO_COMMAND and (
            not isinstance(command, (list, tuple)) or not command
            or not all(isinstance(part, str) and part for part in command)):
        raise ConfigError('lazarus_command must be "auto", a list of strings (program and arguments) or null')
    _check_llama(config)
    _check_speech(config)
    for key in ("incident_crashes", "incident_window_seconds", "stable_seconds"):
        value = getattr(config, key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise ConfigError("{} must be a positive number".format(key))
    try:
        parse_time(config.snapshot_time)
    except ValueError as e:
        raise ConfigError("snapshot_time: {}".format(e))
    excludes = config.snapshot_excludes
    if not isinstance(excludes, (list, tuple)) or not all(isinstance(p, str) and p.strip("/") for p in excludes):
        raise ConfigError("snapshot_excludes must be a list of patterns")
    base = os.path.dirname(path)
    resolved = {}
    for key in _PATH_KEYS + _PROGRAM_KEYS:
        value = getattr(config, key)
        if value is None or value == ":memory:":
            continue
        value = os.path.expanduser(value)
        if key in _PATH_KEYS or os.path.dirname(value):
            resolved[key] = os.path.join(base, value)
    return replace(config, **resolved)


def build_lazarus_command(config, which=shutil.which, isfile=os.path.isfile):
    """The command the Supervisor runs: `lazarus_command`, with "auto" built from the llama_* keys.

    None if Lazarus is started by hand. With "auto" and both llama-server's binary and model set, a
    ConfigError if either can't be found, so a PC without them fails at the start with a clear message.
    """
    if config.lazarus_command != AUTO_COMMAND:
        return None if config.lazarus_command is None else list(config.lazarus_command)
    command = ["{python}", "scripts/run_lazarus.py", "--lazarus-dir", config.lazarus_folder]
    if config.lazarus_config is not None:
        command += ["--lazarus-config", config.lazarus_config]
    if config.llama_server_bin is None or config.llama_model is None:
        return command
    binary = config.llama_server_bin
    if not (isfile(binary) if os.path.dirname(binary) else which(binary)):
        raise ConfigError("llama-server not found at {}: set llama_server_bin to its path, or set it to null "
                          "and start llama-server by hand".format(binary))
    if not isfile(config.llama_model):
        raise ConfigError("model file {} doesn't exist: set llama_model to the path of a GGUF model, or set it "
                          "to null and start llama-server by hand".format(config.llama_model))
    command += ["--llama-health", "http://127.0.0.1:{}/health".format(config.llama_port)]
    if config.llama_log is not None:
        command += ["--llama-log", config.llama_log]
    command += ["--", binary, "-m", config.llama_model, "--host", "127.0.0.1", "--port", str(config.llama_port)]
    if config.llama_context is not None:
        command += ["-c", str(config.llama_context)]
    if config.llama_threads is not None:
        command += ["-t", str(config.llama_threads)]
    return command + list(config.llama_args)


def _check_llama(config):
    for key in ("llama_server_bin", "llama_model", "llama_log", "lazarus_config"):
        value = getattr(config, key)
        if value is not None and not (isinstance(value, str) and value):
            raise ConfigError("{} must be a path or null".format(key))
    if not isinstance(config.llama_port, int) or isinstance(config.llama_port, bool):
        raise ConfigError("llama_port must be a number")
    for key in ("llama_context", "llama_threads"):
        value = getattr(config, key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value <= 0):
            raise ConfigError("{} must be a positive number or null".format(key))
    if not isinstance(config.llama_args, (list, tuple)) or not all(isinstance(a, str) for a in config.llama_args):
        raise ConfigError("llama_args must be a list of strings")


def _check_speech(config):
    if config.tts_bin is not None and not (isinstance(config.tts_bin, str) and config.tts_bin):
        raise ConfigError("tts_bin must be a program name, a path or null")
    if not isinstance(config.tts_args, (list, tuple)) or not all(isinstance(a, str) for a in config.tts_args):
        raise ConfigError("tts_args must be a list of strings")
    quiet = config.quiet_hours
    if quiet is None:
        return
    if not isinstance(quiet, (list, tuple)) or len(quiet) != 2:
        raise ConfigError('quiet_hours must be ["HH:MM", "HH:MM"] (start, end) or null')
    try:
        QuietHours(*quiet)
    except ValueError as e:
        raise ConfigError("quiet_hours: {}".format(e))


def load_token(config, environ=None):
    """The Telegram bot token from the environment, else from the token file."""
    environ = os.environ if environ is None else environ
    token = environ.get(TOKEN_ENV, "").strip()
    if token:
        return token
    try:
        with open(config.telegram_token_file, encoding="utf-8") as f:
            token = f.read().strip()
    except OSError as e:
        raise ConfigError("no Telegram token: set {} or create {} ({})".format(
            TOKEN_ENV, config.telegram_token_file, e))
    if not token:
        raise ConfigError("Telegram token file {} is empty".format(config.telegram_token_file))
    return token
