"""RepoManager: Lazarus's code and memory as commits on `live`, pushed with the deploy key.

The backend owns a clone of the Lazarus repo in its own folder (`data/repo`). Lazarus's folder has no
`.git` and never gets one: a snapshot copies Lazarus's files into the clone's work tree, writes the
memory database as a text SQL dump (`memory.sql`, made with Python's own sqlite3), commits that to
`live` and pushes it. A push that fails (offline) leaves the commit in the clone; `push_pending`
pushes it later. `live` only ever moves forward: when the remote moved on, the local commits are
joined to it with a merge commit, never rewritten, so a remote that refuses non-fast-forward
updates (GitHub's rulesets) always accepts the push.

A restore (`/rollback`) deploys the files of any pushed commit of `main` or `live`: after a snapshot
of the current state it writes a new commit on `live` whose tree is the target's (code only: with the
current memory dump), writes the files into Lazarus's folder and, with memory, rebuilds the memory
database from the target's dump. `list_commits` offers the latest commits of both branches.

The backend runs as root, so nothing Lazarus can write may make it run code or leak a secret:

- git runs with hooks disabled (`core.hooksPath` is an empty folder of the backend's), with no
  system, global or user configuration (GIT_CONFIG_NOSYSTEM, HOME and GIT_CONFIG_GLOBAL point into
  the backend's folder) and with `--git-dir`/`--work-tree` given explicitly, so it never looks for a
  repository in a parent folder. Commits are written with plumbing (write-tree, commit-tree,
  update-ref), which runs no hooks at all; ssh gets the deploy key, IdentitiesOnly and the backend's
  own known_hosts through GIT_SSH_COMMAND.
- Files are read without following symbolic links (`O_NOFOLLOW`, folder by folder through file
  descriptors on Linux); symbolic links, special files and, on Linux, anything not owned by the
  owner of Lazarus's folder are skipped, and a root-owned Lazarus folder is refused, so Lazarus
  can't point the backend at the deploy key. Every entry named `.git` is skipped at any depth.
- Lazarus's `.gitignore` and `.gitattributes` are copied as plain files but have no say: files are
  added with `--force` (the backend's own excludes decide), and the clone's `info/attributes`
  switches off filters, end-of-line conversion and diff/merge drivers for every path. Filters or
  drivers could only run commands that are defined in git configuration, and the backend reads none
  that Lazarus can write; this keeps the committed bytes exactly as they are on disk as well.
- The memory database is copied first (with its journal, also without following links) and dumped
  from the copy with `trusted_schema` off; nothing of Lazarus's is ever executed.
- A restore writes into Lazarus's folder the same way: folder by folder through file descriptors
  opened with `O_NOFOLLOW`, every file written to a new temporary file and renamed over the old name,
  so a symbolic or hard link Lazarus planted is replaced, never written through. New files and
  folders get the owner of Lazarus's folder. Excluded paths (the database files, `config.local.json`,
  caches, logs), `.git` and links in the commit are never written. The memory database is rebuilt
  from the dump in the backend's own folder with Python's sqlite3, an authorizer that refuses ATTACH
  and every statement a dump doesn't need, and then copied in like a file.
"""

import fnmatch
import logging
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections import namedtuple
from dataclasses import dataclass

MEMORY_DUMP = "memory.sql"
DEFAULT_SIZE_LIMIT = 5 * 1024 * 1024
# Excluded from snapshots and never written by a restore, gitignore-like: a pattern with a slash is
# matched against the path from the folder's root, one without against every name; a trailing slash
# matches folders only. Lazarus's `config.json` is tracked; `config.local.json` is the PC's override.
DEFAULT_EXCLUDES = ("/config.local.json", "*.db", "*.db-journal", "*.db-wal", "*.db-shm", "*.log", "/logs/",
                    "__pycache__/", "*.py[cod]", ".cache/", ".pytest_cache/")
# The owner's branch; a restore takes commits from it and from the backend's branch.
MAIN_BRANCH = "main"
# A commit as the owner types it: 4 to 40 hex digits (git's shortest abbreviation is 4).
_SHA = re.compile(r"[0-9a-f]{4,40}")
_MEMORY_SUFFIXES = ("", "-journal", "-wal", "-shm")
AUTHOR_NAME = "lazarus-backend"
AUTHOR_EMAIL = "lazarus-backend@localhost"
GIT_TIMEOUT = 300

# Written to the clone's .git/info/attributes, which overrides every .gitattributes in the work tree.
_ATTRIBUTES = ("# Written by lazarus-backend: Lazarus's own .gitattributes must not filter, convert or\n"
               "# diff anything in this clone. Files are committed byte for byte.\n"
               "* -text -filter -diff -merge -ident -working-tree-encoding -export-subst\n")
_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT: Windows symbolic links and junctions
_FD_WALK = (hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY") and os.listdir in os.supports_fd
            and all(f in os.supports_dir_fd for f in (os.open, os.stat, os.mkdir, os.unlink, os.rmdir, os.rename)))

log = logging.getLogger(__name__)


class RepoError(Exception):
    """A git command or a snapshot step failed."""


class RestoreRefused(RepoError):
    """A restore was refused before anything changed: an unknown or unpushed sha, no memory dump, offline."""


class _Changed(Exception):
    """A file or folder was replaced between looking at it and opening it."""


class _Blocked(Exception):
    """Something in Lazarus's folder that a restore must not replace (another user's, a folder for a file)."""


_TreeEntry = namedtuple("_TreeEntry", "mode kind sha path")


@dataclass(frozen=True)
class CommitInfo:
    """A commit `/rollback` offers: full sha, the branch it's on, committer time, subject line."""
    sha: str
    branch: str
    date: float
    subject: str

    @property
    def short(self):
        return self.sha[:7]


@dataclass(frozen=True)
class RestoreResult:
    commit: str           # the new commit on the branch, now deployed
    target: str           # the restored commit (full sha)
    target_branch: str    # where the target was found: "main" or the branch
    subject: str          # the target's subject line
    with_memory: bool
    snapshot: str         # the branch before the restore: the pre-rollback snapshot
    pushed: bool
    skipped: tuple = ()   # "path (reason)" for files of the target that weren't written


@dataclass(frozen=True)
class SnapshotResult:
    commit: object       # the branch's commit after the snapshot (None if there is none at all)
    created: bool        # a new commit was made (False: nothing changed since the last snapshot)
    pushed: bool         # the branch is on the remote now
    skipped: tuple = ()  # "path (reason)" for every file left out besides the excludes


@dataclass(frozen=True)
class RepoStatus:
    commit: object = None     # short sha of the running commit (the latest deployment)
    branch: object = None
    last_push: object = None  # time of the last successful push (time.time() seconds)
    pending: object = None    # commits waiting for a push


def git_environment(home, ssh_command=None, base=None):
    """Environment for git that reads no configuration outside `home` (a folder of the backend's).

    Inherited GIT_* and SSH_* variables (GIT_DIR, GIT_EXEC_PATH, SSH_AUTH_SOCK, …) are dropped.
    """
    base = os.environ if base is None else base
    env = {key: value for key, value in base.items()
           if not key.upper().startswith(("GIT_", "SSH_")) and key.upper() not in ("HOME", "XDG_CONFIG_HOME")}
    env.update({
        "HOME": home,
        "XDG_CONFIG_HOME": os.path.join(home, ".config"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.path.join(home, "empty.gitconfig"),  # git >= 2.32; HOME covers older ones
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": AUTHOR_NAME,
        "GIT_AUTHOR_EMAIL": AUTHOR_EMAIL,
        "GIT_COMMITTER_NAME": AUTHOR_NAME,
        "GIT_COMMITTER_EMAIL": AUTHOR_EMAIL,
        "LC_ALL": "C",
    })
    if ssh_command:
        env["GIT_SSH_COMMAND"] = ssh_command
    return env


def prepare_git_home(home):
    """Create `home` with the empty global config git_environment points to."""
    os.makedirs(home, exist_ok=True)
    empty = os.path.join(home, "empty.gitconfig")
    if not os.path.exists(empty):
        open(empty, "w").close()


def rmtree(path):
    """shutil.rmtree that also removes read-only files (git's objects on Windows)."""
    def make_writable_and_retry(function, failed_path, *_):
        os.chmod(failed_path, stat.S_IWRITE)
        function(failed_path)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=make_writable_and_retry)
    else:
        shutil.rmtree(path, onerror=make_writable_and_retry)


class RepoManager:
    """Snapshots `lazarus_dir` into the clone at `clone_dir` and pushes `branch` to `remote_url`;
    restores pushed commits of `main` and `branch` into `lazarus_dir`.

    `memory_db` is the memory database's path inside `lazarus_dir` (with `/`). `ledger` (a
    StabilityLedger) gets the deployments and pushes. All operations hold `lock`, so a restore takes
    its snapshot inside its own critical section.
    """

    def __init__(self, lazarus_dir, clone_dir, remote_url, ledger, branch="live", memory_db="data/memory.db",
                 git_bin="git", ssh_bin="ssh", deploy_key=None, known_hosts=None, size_limit=DEFAULT_SIZE_LIMIT,
                 excludes=DEFAULT_EXCLUDES, state_dir=None, clock=time.time, sleep=time.sleep):
        self.lazarus_dir = os.path.abspath(lazarus_dir)
        self.clone_dir = os.path.abspath(clone_dir)
        self.remote_url = remote_url
        self.ledger = ledger
        self.branch = branch
        memory_db = memory_db.replace("\\", "/").strip("/")
        if not memory_db or ".." in memory_db.split("/"):
            raise ValueError("memory_db must be a path inside Lazarus's folder: {!r}".format(memory_db))
        self.memory_db = memory_db
        self.git_bin = git_bin
        self.ssh_bin = ssh_bin
        self.deploy_key = deploy_key
        self.known_hosts = known_hosts
        self.size_limit = size_limit
        if isinstance(excludes, str) or not all(isinstance(p, str) and p.strip("/") for p in excludes):
            raise ValueError("excludes must be a list of patterns")
        self.excludes = tuple(excludes)
        self.state_dir = os.path.abspath(state_dir or self.clone_dir + "-state")
        self.clock = clock
        self._sleep = sleep
        self.lock = threading.RLock()
        self._git_dir = os.path.join(self.clone_dir, ".git")
        self._home = os.path.join(self.state_dir, "home")
        self._hooks = os.path.join(self.state_dir, "no-hooks")
        self._template = os.path.join(self.state_dir, "no-template")
        self._pending = None

    @classmethod
    def from_config(cls, config, ledger):
        return cls(config.lazarus_folder, config.repo_clone_dir, config.repo_remote_url, ledger,
                   branch=config.repo_branch, memory_db=config.lazarus_memory_db, git_bin=config.git_bin,
                   ssh_bin=config.ssh_bin, deploy_key=config.repo_deploy_key_file,
                   known_hosts=config.repo_known_hosts_file, size_limit=config.snapshot_size_limit,
                   excludes=config.snapshot_excludes)

    @property
    def _local_ref(self):
        return "refs/heads/" + self.branch

    @property
    def _remote_ref(self):
        return "refs/remotes/origin/" + self.branch

    # --- public operations

    def snapshot(self):
        """Commit Lazarus's folder and memory dump to the branch and push it if the remote answers.

        Raises RepoError if no snapshot could be committed; a failed push is not an error: the
        commit stays and `pending_count()` says so.
        """
        with self.lock:
            self._ensure_clone()
            online = self.fetch()
            if online:
                self._join_remote()
            return self._snapshot(online)

    def _snapshot(self, online, damaged_memory_ok=False):
        skipped, notes = self._fill_work_tree(damaged_memory_ok)
        head, created = self._commit_work_tree(skipped, notes)
        self._record_deployment(head)
        pushed = False
        if online and self._count_pending():
            pushed = self._push()
        self._pending = self._count_pending()
        if created:
            log.info("snapshot %s on %s%s", head[:7], self.branch,
                     ", pushed" if pushed else ", waiting for a push")
        return SnapshotResult(commit=head, created=created, pushed=pushed or (online and not self._pending),
                              skipped=tuple(skipped))

    def list_commits(self, count=5):
        """The latest `count` commits of `main` and of the branch on the remote, newest first per branch.

        Fetches first; offline, the remote's state as of the last fetch. RepoError if there are none.
        """
        with self.lock:
            self._ensure_clone()
            self.fetch()
            commits, seen = [], set()
            for branch in self._branches():
                ref = "refs/remotes/origin/" + branch
                if self._resolve(ref) is None:
                    continue
                out = self._out("log", "-n", str(count), "--format=%H%x00%ct%x00%s", ref, "--")
                for line in out.splitlines():
                    sha, date, subject = line.split("\0", 2)
                    if sha not in seen:  # a commit on both branches is listed once, under main
                        seen.add(sha)
                        commits.append(CommitInfo(sha=sha, branch=branch, date=float(date), subject=subject))
            if not commits:
                raise RepoError("the remote has no {} branch (or it couldn't be fetched)".format(
                    " or ".join(self._branches())))
            return commits

    def restore(self, sha, with_memory=False, stop_lazarus=None, start_lazarus=None):
        """Deploy the files of commit `sha` (pushed, on `main` or the branch) to Lazarus.

        Fetches and checks that `sha` is a commit of `main` or the branch on the remote, snapshots
        the current state, then writes a new commit on the branch whose tree is the target's (code
        only: with the current memory dump instead of the target's), writes the files into Lazarus's
        folder and, `with_memory`, rebuilds the memory database from the target's dump. Files that
        Lazarus's folder has and the pre-rollback snapshot committed, but the target doesn't, are
        removed; untracked and excluded files stay. Records the deployment and pushes.

        `stop_lazarus()` is called before Lazarus's folder is touched and `start_lazarus()` after it,
        also when writing failed; both run with `lock` held. Without them the caller stops and starts
        Lazarus itself (the Supervisor does after an incident, issue 11).

        Raises RestoreRefused when nothing was changed (unknown or unpushed sha, no memory dump in
        the target, the remote unreachable, the snapshot failed), RepoError for a failure later on.
        """
        sha = (sha or "").strip().lower()
        if not _SHA.fullmatch(sha):
            raise RestoreRefused("it isn't a commit sha (4 to 40 hex digits)")
        with self.lock:
            self._ensure_clone()
            if not self.fetch():
                raise RestoreRefused("the Lazarus repo can't be reached to check it")
            self._join_remote()
            target, target_branch = self._find_pushed(sha)
            database = self._database_from_dump(target) if with_memory else None
            try:
                before = self._snapshot(online=True, damaged_memory_ok=with_memory).commit
            except RepoError as e:
                raise RestoreRefused("the snapshot before it failed: {}".format(e))
            tree = self._restored_tree(target, before, with_memory)
            subject = self._out("log", "-1", "--format=%s", target)

            skipped = []
            files = self._restorable_files(tree, skipped)
            if stop_lazarus is not None:
                stop_lazarus()
            try:
                self._deploy(files, self._restorable_files(before), database, skipped)
                lines = ["Roll back to {} ({})".format(target[:7], "with memory" if with_memory else "code only"),
                         "",
                         "Restores the files of {} ({}: {}).".format(target[:7], target_branch, subject),
                         "The memory dump is the one of {}.".format(target[:7]) if with_memory else
                         "The memory dump stays as in {}.".format(before[:7]),
                         "Before the rollback: {}.".format(before[:7])]
                if skipped:
                    lines += ["", "Not restored:"] + ["- " + entry for entry in skipped]
                commit = self._commit_tree(tree, [before], "\n".join(lines) + "\n")
                self._git("update-ref", self._local_ref, commit, before)
                self.ledger.record_deployment(commit, self.branch, self.clock())
            finally:
                if start_lazarus is not None:
                    start_lazarus()
            pushed = self._push()
            self._pending = self._count_pending()
            log.info("rolled back to %s (%s) as %s on %s%s", target[:7], "with memory" if with_memory else
                     "code only", commit[:7], self.branch, "" if pushed else ", waiting for a push")
            return RestoreResult(commit=commit, target=target, target_branch=target_branch, subject=subject,
                                 with_memory=with_memory, snapshot=before, pushed=pushed, skipped=tuple(skipped))

    def push_pending(self):
        """Push commits that are still waiting. True if nothing is left waiting afterwards."""
        with self.lock:
            if not os.path.isdir(self._git_dir):
                self._pending = 0
                return True
            self._ensure_clone()
            if not self.fetch():
                self._pending = self._count_pending()
                return False
            self._join_remote()
            if self._count_pending():
                self._push()
            self._pending = self._count_pending()
            return not self._pending

    def fetch(self):
        """Update the remote-tracking branches; False (logged) if the remote can't be reached."""
        with self.lock:
            try:
                self._check_ssh_files()
                self._git("fetch", "--quiet", "--no-tags", "origin")
            except RepoError as e:
                log.warning("can't reach the Lazarus repo: %s", e)
                return False
            return True

    def pending_count(self):
        """Commits on the branch the remote doesn't have yet, as of the last operation."""
        if self._pending is None:
            try:
                self._pending = self._count_pending()
            except RepoError as e:
                log.warning("can't count unpushed commits: %s", e)
        return self._pending

    def status(self):
        deployment = self.ledger.current_deployment()
        push = self.ledger.last_push()
        return RepoStatus(commit=deployment.sha[:7] if deployment else None,
                          branch=deployment.branch if deployment else None,
                          last_push=push.pushed_at if push else None,
                          pending=self.pending_count())

    # --- the clone

    def _ensure_clone(self):
        for folder in (self.clone_dir, self._hooks, self._template):
            os.makedirs(folder, exist_ok=True)
        prepare_git_home(self._home)
        if os.listdir(self._hooks):
            raise RepoError("{} must stay empty: it's the clone's hooks folder".format(self._hooks))
        if not os.path.isdir(self._git_dir):
            self._run(["init", "--quiet", "--template=" + self._template, self.clone_dir], repo=False)
        self._git("symbolic-ref", "HEAD", self._local_ref)
        self._git("config", "--replace-all", "remote.origin.url", self.remote_url)
        self._git("config", "--replace-all", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
        info = os.path.join(self._git_dir, "info")
        os.makedirs(info, exist_ok=True)
        with open(os.path.join(info, "attributes"), "w", encoding="utf-8", newline="\n") as f:
            f.write(_ATTRIBUTES)

    def _join_remote(self):
        """Bring the local branch up to the remote one without rewriting either."""
        remote = self._resolve(self._remote_ref)
        if remote is None:
            return
        local = self._resolve(self._local_ref)
        if local is None or self._is_ancestor(local, remote):
            self._git("update-ref", self._local_ref, remote, local or "")
        elif not self._is_ancestor(remote, local):
            # Both moved on (the owner pushed while a snapshot waited): keep the local files on top.
            tree = self._out("rev-parse", local + "^{tree}")
            merge = self._commit_tree(tree, [local, remote], "Join {0} from the remote\n\nThe files stay as "
                                      "in the local snapshot {1}.\n".format(self.branch, local[:7]))
            self._git("update-ref", self._local_ref, merge, local)
            log.info("%s moved on the remote; joined it with merge commit %s", self.branch, merge[:7])

    def _push(self):
        head = self._resolve(self._local_ref)
        try:
            self._check_ssh_files()
            self._git("push", "--quiet", "origin", "{0}:{0}".format(self._local_ref))
        except RepoError as e:
            log.warning("push of %s failed, the commit waits for the next try: %s", self.branch, e)
            return False
        self.ledger.record_push(head, self.clock())
        log.info("pushed %s at %s", self.branch, head[:7])
        return True

    def _count_pending(self):
        if not os.path.isdir(self._git_dir):
            return 0
        local = self._resolve(self._local_ref)
        if local is None:
            return 0
        remote = self._resolve(self._remote_ref)
        if remote is None:
            return int(self._out("rev-list", "--count", local))
        return int(self._out("rev-list", "--count", "{}..{}".format(remote, local)))

    # --- the snapshot

    def _fill_work_tree(self, damaged_memory_ok=False):
        """Replace the clone's work tree with Lazarus's files and the memory dump.

        With `damaged_memory_ok` (a restore with memory replaces it anyway), a memory database that
        can't be dumped keeps the previous commit's dump instead of failing the snapshot.
        Returns (skipped, notes): files left out with the reason, and remarks for the commit message.
        """
        if not os.path.isdir(self.lazarus_dir):
            raise RepoError("Lazarus's folder {} doesn't exist".format(self.lazarus_dir))
        for name in os.listdir(self.clone_dir):
            if name != ".git":
                path = os.path.join(self.clone_dir, name)
                if os.path.isdir(path) and not os.path.islink(path):
                    rmtree(path)
                else:
                    os.remove(path)
        skipped, notes = [], []
        root = _open_root(self.lazarus_dir)
        try:
            owner = self._folder_owner(root)
            uid = owner[0] if owner else None
            self._copy_folder(root, self.clone_dir, "", uid, skipped)
            try:
                dump = self._dump_memory(root, uid)
                if dump is None:
                    notes.append("No memory database at {}.".format(self.memory_db))
            except RepoError as e:
                if not damaged_memory_ok:
                    raise
                dump = self._previous_dump()
                notes.append("{}; {}.".format(e, "memory.sql is the previous commit's" if dump is not None
                                              else "there's no memory.sql"))
        finally:
            root.close()
        if dump is not None:
            with open(os.path.join(self.clone_dir, MEMORY_DUMP), "w", encoding="utf-8", newline="\n") as f:
                f.write(dump)
        return skipped, notes

    def _folder_owner(self, root):
        """(uid, gid) of Lazarus's folder where ownership is checked (Linux), else None.

        A root-owned folder is refused when the backend runs as root: it isn't Lazarus's.
        """
        if not _FD_WALK:
            return None
        st = os.fstat(root.fd)
        if st.st_uid == 0 and _as_root():
            raise RepoError("refusing to use {}: it's owned by root, not by Lazarus's user".format(self.lazarus_dir))
        return st.st_uid, st.st_gid

    def _previous_dump(self):
        head = self._resolve(self._local_ref)
        entry = self._root_entry(head, MEMORY_DUMP) if head else None
        if entry is None:
            return None
        return self._blobs([entry.sha])[entry.sha].decode("utf-8")

    def _copy_folder(self, folder, target, prefix, owner, skipped):
        for name in folder.names():
            path = prefix + name
            try:
                st = folder.lstat(name)
            except FileNotFoundError:
                continue  # deleted while we looked
            reason = self._refuse(path, name, st, owner)
            if reason:
                skipped.append("{} ({})".format(path, reason))
                continue
            is_dir = stat.S_ISDIR(st.st_mode)
            if self._excluded(path, is_dir):
                continue
            if is_dir:
                try:
                    sub = folder.open_folder(name, st)
                except (OSError, _Changed) as e:
                    skipped.append("{}/ ({})".format(path, _why(e)))
                    continue
                try:
                    os.mkdir(os.path.join(target, name))
                    self._copy_folder(sub, os.path.join(target, name), path + "/", owner, skipped)
                finally:
                    sub.close()
            elif st.st_size > self.size_limit:
                skipped.append("{} ({}, over the {} limit)".format(path, _megabytes(st.st_size),
                                                                   _megabytes(self.size_limit)))
            else:
                try:
                    with os.fdopen(folder.open_file(name, st), "rb") as source:
                        data = source.read(self.size_limit + 1)
                except (OSError, _Changed) as e:
                    skipped.append("{} ({})".format(path, _why(e)))
                    continue
                if len(data) > self.size_limit:
                    skipped.append("{} (over the {} limit)".format(path, _megabytes(self.size_limit)))
                    continue
                destination = os.path.join(target, name)
                with open(destination, "wb") as f:
                    f.write(data)
                if os.name == "posix":
                    os.chmod(destination, 0o755 if st.st_mode & stat.S_IXUSR else 0o644)

    def _refuse(self, path, name, st, owner):
        """Why `path` must never be copied, or None."""
        if _is_git_name(name):
            return "git metadata, never copied"
        if path == MEMORY_DUMP:
            return "name reserved for the memory dump"
        if _is_link(st):
            return "symbolic link"
        if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
            return "not a regular file"
        if owner is not None and st.st_uid != owner:
            return "owned by another user"
        return None

    def _excluded(self, path, is_dir):
        name = path.rsplit("/", 1)[-1]
        for pattern in self.excludes:
            if pattern.endswith("/") and not is_dir:
                continue
            pattern = pattern.rstrip("/")
            if "/" in pattern:
                if fnmatch.fnmatchcase(path, pattern.lstrip("/")):
                    return True
            elif fnmatch.fnmatchcase(name, pattern):
                return True
        return False

    def _dump_memory(self, root, owner, attempts=3):
        """The memory database as SQL text, or None if Lazarus has none yet."""
        for attempt in range(1, attempts + 1):
            work = tempfile.mkdtemp(prefix="memory-", dir=self.state_dir)
            try:
                copy = self._copy_memory_files(root, owner, work)
                if copy is None:
                    return None
                return _sql_dump(copy)
            except (RepoError, sqlite3.Error, OSError, _Changed) as e:
                if attempt == attempts:
                    raise RepoError("can't dump the memory database {}: {}".format(self.memory_db, _why(e)))
                log.info("memory dump attempt %d failed (%s), retrying", attempt, _why(e))
                self._sleep(1)
            finally:
                rmtree(work)

    def _copy_memory_files(self, root, owner, work):
        """Copy the database and its journal or WAL into `work`; the copy's path, or None if missing."""
        parts = self.memory_db.split("/")
        folder, opened = root, []
        try:
            for part in parts[:-1]:
                try:
                    st = folder.lstat(part)
                except FileNotFoundError:
                    return None
                if _is_link(st) or not stat.S_ISDIR(st.st_mode) or (owner is not None and st.st_uid != owner):
                    raise RepoError("{} is not a plain folder of Lazarus's".format(part))
                folder = folder.open_folder(part, st)
                opened.append(folder)
            copy = os.path.join(work, "memory.db")
            for suffix in ("", "-journal", "-wal"):
                try:
                    st = folder.lstat(parts[-1] + suffix)
                except FileNotFoundError:
                    if suffix == "":
                        return None
                    continue
                if _is_link(st) or not stat.S_ISREG(st.st_mode) or (owner is not None and st.st_uid != owner):
                    raise RepoError("{} is not a plain file of Lazarus's".format(self.memory_db + suffix))
                with os.fdopen(folder.open_file(parts[-1] + suffix, st), "rb") as source, \
                        open(copy + suffix, "wb") as target:
                    shutil.copyfileobj(source, target)
            return copy
        finally:
            for folder in opened:
                folder.close()

    def _commit_work_tree(self, skipped, notes):
        """Commit the work tree to the branch if it changed; (head sha or None, created)."""
        self._git("add", "--all", "--force", "--", ".")
        tree = self._out("write-tree")
        head = self._resolve(self._local_ref)
        if head is not None and self._out("rev-parse", head + "^{tree}") == tree:
            return head, False
        lines = ["Snapshot " + time.strftime("%Y-%m-%d %H:%M", time.localtime(self.clock()))]
        if notes:
            lines += [""] + notes
        if skipped:
            lines += ["", "Skipped:"] + ["- " + entry for entry in skipped]
        commit = self._commit_tree(tree, [head] if head else [], "\n".join(lines) + "\n")
        self._git("update-ref", self._local_ref, commit, head or "")
        return commit, True

    def _record_deployment(self, head):
        """Record `head` as deployed when its code differs from the running commit's."""
        if head is None:
            return
        current = self.ledger.current_deployment()
        if current is not None and not self._code_differs(current.sha, head):
            return
        self.ledger.record_deployment(head, self.branch, self.clock())

    def _code_differs(self, old, new):
        if self._git("cat-file", "-e", old + "^{commit}", check=False).returncode != 0:
            return True
        result = self._git("diff", "--quiet", old, new, "--", ".", ":(exclude){}".format(MEMORY_DUMP), check=False)
        if result.returncode not in (0, 1):
            raise RepoError("git diff failed: {}".format(_text(result.stderr)))
        return result.returncode == 1

    # --- the restore

    def _branches(self):
        return (MAIN_BRANCH,) if self.branch == MAIN_BRANCH else (MAIN_BRANCH, self.branch)

    def _find_pushed(self, sha):
        """(full sha, branch) of `sha` if it's a commit of `main` or the branch on the remote."""
        commit = self._resolve(sha)
        if commit is not None:
            for branch in self._branches():
                remote = self._resolve("refs/remotes/origin/" + branch)
                if remote is not None and self._is_ancestor(commit, remote):
                    return commit, branch
        raise RestoreRefused("it isn't a pushed commit of {}".format(" or ".join(self._branches())))

    def _database_from_dump(self, target):
        """The memory database rebuilt from `target`'s dump, as bytes; RestoreRefused if it can't be."""
        entry = self._root_entry(target, MEMORY_DUMP)
        if entry is None or entry.kind != "blob":
            raise RestoreRefused("{} has no memory dump ({})".format(target[:7], MEMORY_DUMP))
        try:
            sql = self._blobs([entry.sha])[entry.sha].decode("utf-8")
        except UnicodeDecodeError as e:
            raise RestoreRefused("the memory dump of {} isn't text: {}".format(target[:7], e))
        work = tempfile.mkdtemp(prefix="restore-", dir=self.state_dir)
        try:
            path = os.path.join(work, "memory.db")
            try:
                _build_database(path, sql)
            except (sqlite3.Error, RepoError) as e:
                raise RestoreRefused("the memory dump of {} can't be loaded: {}".format(target[:7], e))
            with open(path, "rb") as f:
                return f.read()
        finally:
            rmtree(work)

    def _restored_tree(self, target, before, with_memory):
        """The target's tree, with `before`'s memory dump unless `with_memory` (none if it had none)."""
        entries = [e for e in self._ls_tree(target) if e.path != MEMORY_DUMP]
        dump = self._root_entry(target if with_memory else before, MEMORY_DUMP)
        if dump is not None:
            entries.append(dump)
        listing = b"".join("{} {} {}\t".format(e.mode, e.kind, e.sha).encode("ascii") + os.fsencode(e.path) + b"\0"
                           for e in entries)
        return self._out("mktree", "-z", input=listing)

    def _restorable_files(self, treeish, skipped=None):
        """{path: (mode, blob sha)} of the files in `treeish` a restore writes into Lazarus's folder.

        Left out: the memory dump and database, excluded paths (silently), `.git`, links and
        submodules (listed in `skipped`).
        """
        memory_files = {self.memory_db + suffix for suffix in _MEMORY_SUFFIXES}
        files = {}
        for entry in self._ls_tree(treeish, recursive=True):
            parts = entry.path.split("/")
            reason = None
            if any(_is_git_name(part) for part in parts):
                reason = "git metadata, never restored"
            elif entry.path == MEMORY_DUMP or entry.path in memory_files or self._excluded(entry.path, False) or \
                    any(self._excluded("/".join(parts[:i]), True) for i in range(1, len(parts))):
                continue
            elif entry.mode == "120000":
                reason = "symbolic link"
            elif entry.mode not in ("100644", "100755") or entry.kind != "blob":
                reason = "not a regular file"
            if reason is None:
                files[entry.path] = (entry.mode, entry.sha)
            elif skipped is not None:
                skipped.append("{} ({})".format(entry.path, reason))
        return files

    def _deploy(self, files, before, database, skipped):
        """Write `files` into Lazarus's folder, remove those only `before` has, install `database`.

        Files whose content and mode are as in `before` (the snapshot just taken) aren't rewritten.
        A file that can't be written is added to `skipped`; a database that can't be is a RepoError.
        """
        changed = {path: entry for path, entry in files.items() if before.get(path) != entry}
        contents = self._blobs(sorted({sha for _, sha in changed.values()}))
        root = _open_root(self.lazarus_dir)
        try:
            owner = self._folder_owner(root)
            for path in sorted(changed):
                mode, sha = changed[path]
                try:
                    _place_file(root, path, contents[sha], 0o755 if mode == "100755" else 0o644, owner)
                except (OSError, _Changed, _Blocked) as e:
                    skipped.append("{} ({})".format(path, _why(e)))
            for path in sorted(set(before) - set(files)):
                try:
                    _remove_file(root, path, owner)
                except (OSError, _Changed, _Blocked) as e:
                    log.warning("couldn't remove %s from Lazarus's folder: %s", path, _why(e))
            if database is not None:
                try:
                    _place_file(root, self.memory_db, database, 0o600, owner,
                                remove_first=[self.memory_db + suffix for suffix in _MEMORY_SUFFIXES[1:]])
                except (OSError, _Changed, _Blocked) as e:
                    raise RepoError("can't write the memory database {}: {}".format(self.memory_db, _why(e)))
        finally:
            root.close()

    def _ls_tree(self, treeish, recursive=False):
        """The entries of `treeish` (its top level, or every file with `recursive`)."""
        raw = self._git("ls-tree", "-z", "--full-tree", *(["-r"] if recursive else []), treeish).stdout
        entries = []
        for record in raw.split(b"\0"):
            if record:
                meta, _, path = record.partition(b"\t")
                mode, kind, sha = meta.decode("ascii").split()
                entries.append(_TreeEntry(mode, kind, sha, os.fsdecode(path)))
        return entries

    def _root_entry(self, treeish, name):
        for entry in self._ls_tree(treeish):
            if entry.path == name:
                return entry
        return None

    def _blobs(self, shas):
        """{sha: content} of the blobs `shas`, read in one `git cat-file --batch`."""
        if not shas:
            return {}
        out = self._git("cat-file", "--batch", input="".join(sha + "\n" for sha in shas).encode("ascii")).stdout
        blobs, pos = {}, 0
        while pos < len(out):
            end = out.index(b"\n", pos)
            header = out[pos:end].decode("ascii", "replace").split()
            if len(header) != 3:
                raise RepoError("git cat-file: {}".format(" ".join(header)))
            size = int(header[2])
            blobs[header[0]] = out[end + 1:end + 1 + size]
            pos = end + 1 + size + 1
        return blobs

    # --- git

    def _commit_tree(self, tree, parents, message):
        args = ["commit-tree", tree]
        for parent in parents:
            args += ["-p", parent]
        return self._out(*(args + ["-F", "-"]), input=message.encode("utf-8"))

    def _resolve(self, ref):
        result = self._git("rev-parse", "--verify", "--quiet", ref + "^{commit}", check=False)
        if result.returncode != 0:
            return None
        return _text(result.stdout).strip() or None

    def _is_ancestor(self, ancestor, descendant):
        result = self._git("merge-base", "--is-ancestor", ancestor, descendant, check=False)
        if result.returncode not in (0, 1):
            raise RepoError("git merge-base failed: {}".format(_text(result.stderr)))
        return result.returncode == 0

    def _out(self, *args, input=None):
        return _text(self._git(*args, input=input).stdout).strip()

    def _git(self, *args, check=True, input=None):
        return self._run(["--git-dir=" + self._git_dir, "--work-tree=" + self.clone_dir] + list(args),
                         check=check, input=input)

    def _run(self, args, check=True, input=None, repo=True):
        name = args[2] if repo else args[0]
        command = [self.git_bin, "-c", "core.hooksPath=" + self._hooks, "-c", "core.fsmonitor=",
                   "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "-c", "protocol.ext.allow=never",
                   "-c", "commit.gpgSign=false"] + args
        try:
            result = subprocess.run(command, cwd=self.clone_dir if repo else self.state_dir,
                                    env=git_environment(self._home, self._ssh_command()),
                                    input=input, stdin=None if input is not None else subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=GIT_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise RepoError("git {} failed: {}".format(name, e))
        if check and result.returncode != 0:
            raise RepoError("git {} failed: {}".format(name, _text(result.stderr).strip() or result.returncode))
        return result

    def _ssh_command(self):
        if not self.deploy_key:
            return None
        parts = [self.ssh_bin, "-i", self.deploy_key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                 "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=30",
                 "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]
        if self.known_hosts:
            parts += ["-o", "UserKnownHostsFile=" + self.known_hosts]
        return " ".join(shlex.quote(part) for part in parts)

    def _check_ssh_files(self):
        for what, path in (("deploy key", self.deploy_key), ("known_hosts file", self.known_hosts)):
            if path and not os.path.isfile(path):
                raise RepoError("{} {} doesn't exist".format(what, path))


# --- reading Lazarus's folder without following links

class _FdFolder:
    """A folder opened as a file descriptor (Linux): names are looked up relative to it, never via links."""

    def __init__(self, fd):
        self.fd = fd

    def names(self):
        return sorted(os.listdir(self.fd))

    def lstat(self, name):
        return os.stat(name, dir_fd=self.fd, follow_symlinks=False)

    def open_folder(self, name, expected):
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
        if not _same(os.fstat(fd), expected):
            os.close(fd)
            raise _Changed("replaced while copying")
        return _FdFolder(fd)

    def open_file(self, name, expected):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or not _same(st, expected):
            os.close(fd)
            raise _Changed("replaced while copying")
        return fd

    def make_folder(self, name):
        os.mkdir(name, 0o755, dir_fd=self.fd)

    def adopt(self, owner):
        """Give this (new) folder to `owner` (uid, gid) when running as root."""
        if owner is not None and _as_root():
            os.fchown(self.fd, *owner)

    def write_file(self, name, data, mode, owner):
        """Write a new file and rename it over `name`: whatever `name` was (a link too) is replaced."""
        temp = ".lazarus-restore-" + secrets.token_hex(8)
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        try:
            with os.fdopen(fd, "wb") as f:
                if owner is not None and _as_root():
                    os.fchown(f.fileno(), *owner)
                os.fchmod(f.fileno(), mode)
                f.write(data)
            os.rename(temp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        except BaseException:
            try:
                os.unlink(temp, dir_fd=self.fd)
            except OSError:
                pass
            raise

    def unlink(self, name):
        os.unlink(name, dir_fd=self.fd)  # a link itself, never what it points to

    def rmdir(self, name):
        os.rmdir(name, dir_fd=self.fd)

    def close(self):
        os.close(self.fd)


class _PathFolder:
    """The same by path, where there are no folder descriptors (Windows, for development)."""

    def __init__(self, path):
        self.path = path

    def names(self):
        return sorted(os.listdir(self.path))

    def lstat(self, name):
        return os.lstat(os.path.join(self.path, name))

    def open_folder(self, name, expected):
        path = os.path.join(self.path, name)
        if not _same(os.lstat(path), expected):
            raise _Changed("replaced while copying")
        return _PathFolder(path)

    def open_file(self, name, expected):
        fd = os.open(os.path.join(self.path, name), os.O_RDONLY | getattr(os, "O_BINARY", 0))
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or not _same(st, expected):
            os.close(fd)
            raise _Changed("replaced while copying")
        return fd

    def make_folder(self, name):
        os.mkdir(os.path.join(self.path, name), 0o755)

    def adopt(self, owner):
        pass  # no owners to set where there are no folder descriptors

    def write_file(self, name, data, mode, owner):
        path = os.path.join(self.path, name)
        temp = os.path.join(self.path, ".lazarus-restore-" + secrets.token_hex(8))
        try:
            with open(temp, "xb") as f:
                f.write(data)
            if os.name == "posix":
                os.chmod(temp, mode)
            try:
                if _is_link(os.lstat(path)):
                    self.unlink(name)
            except FileNotFoundError:
                pass
            os.replace(temp, path)
        except BaseException:
            if os.path.lexists(temp):
                os.remove(temp)
            raise

    def unlink(self, name):
        path = os.path.join(self.path, name)
        if _is_link(os.lstat(path)) and os.path.isdir(path):
            os.rmdir(path)  # a Windows folder link or junction: removes the link only
        else:
            os.remove(path)

    def rmdir(self, name):
        os.rmdir(os.path.join(self.path, name))

    def close(self):
        pass


def _open_root(path):
    if _FD_WALK:
        return _FdFolder(os.open(path, os.O_RDONLY | os.O_DIRECTORY))
    return _PathFolder(path)


def _as_root():
    return hasattr(os, "geteuid") and os.geteuid() == 0


# --- writing into Lazarus's folder without following links

def _enter_folder(folder, name, owner):
    """Open the subfolder `name`, creating it; a file or link in its place is removed first."""
    try:
        st = folder.lstat(name)
    except FileNotFoundError:
        st = None
    if st is not None and owner is not None and st.st_uid != owner[0]:
        raise _Blocked("{} is owned by another user".format(name))
    if st is not None and (_is_link(st) or not stat.S_ISDIR(st.st_mode)):
        folder.unlink(name)  # the restored commit has a folder here; a file was in the snapshot before
        st = None
    created = st is None
    if created:
        folder.make_folder(name)
        st = folder.lstat(name)
    sub = folder.open_folder(name, st)
    if created:
        sub.adopt(owner)
    return sub


def _walk_to(root, path, owner, create):
    """The folders from `root` down to the one holding `path`'s last part, opened; None if one is missing."""
    opened = []
    folder = root
    try:
        for part in path.split("/")[:-1]:
            if create:
                folder = _enter_folder(folder, part, owner)
            else:
                st = folder.lstat(part)
                if _is_link(st) or not stat.S_ISDIR(st.st_mode):
                    raise _Blocked("{} isn't a folder".format(part))
                folder = folder.open_folder(part, st)
            opened.append(folder)
    except BaseException:
        _close_all(opened)
        raise
    return opened


def _close_all(folders):
    for folder in reversed(folders):
        folder.close()


def _place_file(root, path, data, mode, owner, remove_first=()):
    """Write `data` to `path` (with `/`) in Lazarus's folder, with `mode`, owned by `owner`.

    `remove_first`: paths in the same folder to remove before (a database's journal and WAL).
    """
    opened = _walk_to(root, path, owner, create=True)
    try:
        folder = opened[-1] if opened else root
        name = path.rsplit("/", 1)[-1]
        try:
            st = folder.lstat(name)
        except FileNotFoundError:
            st = None
        if st is not None and not _is_link(st) and stat.S_ISDIR(st.st_mode):
            raise _Blocked("a folder is in the way")
        if st is not None and owner is not None and st.st_uid != owner[0]:
            raise _Blocked("owned by another user")
        for other in remove_first:
            try:
                folder.unlink(other.rsplit("/", 1)[-1])
            except FileNotFoundError:
                pass
        folder.write_file(name, data, mode, owner)
    finally:
        _close_all(opened)


def _remove_file(root, path, owner):
    """Remove the file (or link) `path` from Lazarus's folder, and the folders it leaves empty."""
    try:
        opened = _walk_to(root, path, owner, create=False)
    except (FileNotFoundError, _Blocked):
        return  # its folder is gone or was replaced: so is the file
    try:
        folder = opened[-1] if opened else root
        name = path.rsplit("/", 1)[-1]
        try:
            st = folder.lstat(name)
        except FileNotFoundError:
            return
        if not _is_link(st) and stat.S_ISDIR(st.st_mode):
            raise _Blocked("a folder is in the way")
        if owner is not None and st.st_uid != owner[0]:
            raise _Blocked("owned by another user")
        folder.unlink(name)
        names = path.split("/")[:-1]
        for index in range(len(opened) - 1, -1, -1):
            parent = opened[index - 1] if index > 0 else root
            if opened[index].names():
                break
            parent.rmdir(names[index])
    finally:
        _close_all(opened)


def _build_database(path, sql):
    """Create the SQLite database `path` from a dump, allowing only what a dump needs (no ATTACH)."""
    db = sqlite3.connect(path, isolation_level=None)
    try:
        db.execute("PRAGMA trusted_schema = OFF")
        db.set_authorizer(_dump_authorizer)
        db.executescript(sql)
        check = db.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise RepoError("the rebuilt database is damaged: {}".format(check))
    finally:
        db.close()


# What a dump made by iterdump runs (tables, indexes, triggers, views, rows, the virtual tables of
# full-text search through writable_schema). ATTACH, VACUUM INTO and every other pragma are refused.
_DUMP_ACTIONS = {getattr(sqlite3, name) for name in (
    "SQLITE_CREATE_INDEX", "SQLITE_CREATE_TABLE", "SQLITE_CREATE_TRIGGER", "SQLITE_CREATE_VIEW", "SQLITE_DELETE",
    "SQLITE_INSERT", "SQLITE_READ", "SQLITE_SELECT", "SQLITE_TRANSACTION", "SQLITE_UPDATE", "SQLITE_FUNCTION",
    "SQLITE_SAVEPOINT", "SQLITE_CREATE_VTABLE", "SQLITE_RECURSIVE") if hasattr(sqlite3, name)}
_DUMP_PRAGMAS = ("foreign_keys", "writable_schema", "quick_check")


def _dump_authorizer(action, arg1, arg2, database, trigger):
    if action == sqlite3.SQLITE_PRAGMA and (arg1 or "").lower() in _DUMP_PRAGMAS:
        return sqlite3.SQLITE_OK
    if action in _DUMP_ACTIONS and database in (None, "main"):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _same(a, b):
    if a.st_ino and b.st_ino:
        return (a.st_ino, a.st_dev) == (b.st_ino, b.st_dev)
    return stat.S_IFMT(a.st_mode) == stat.S_IFMT(b.st_mode)


def _is_link(st):
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def _is_git_name(name):
    """`.git` and the spellings Windows and macOS file systems treat as `.git`."""
    lowered = name.lower()
    return lowered.rstrip(". ") == ".git" or lowered == "git~1"


def _sql_dump(path):
    db = sqlite3.connect(path)
    try:
        db.execute("PRAGMA trusted_schema = OFF")
        check = db.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise RepoError("the memory database is damaged: {}".format(check))
        return "\n".join(db.iterdump()) + "\n"
    finally:
        db.close()


def _megabytes(size):
    return "{:.1f} MB".format(size / 1024 ** 2)


def _why(error):
    if isinstance(error, OSError) and error.strerror:
        return error.strerror.lower()
    return str(error)


def _text(raw):
    return raw.decode("utf-8", "replace") if raw else ""
