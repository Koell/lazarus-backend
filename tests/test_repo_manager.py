"""RepoManager against real temporary git repos: a bare remote that refuses non-fast-forward updates
and deletions (like GitHub's rulesets on `live`), and a Lazarus folder with code, memory and junk."""

import datetime
import os
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

from lazarus_backend import supervisor as supervision
from lazarus_backend.app import SNAPSHOT_FAILED, Backend
from lazarus_backend.commands import (ROLLBACK_CANCELLED, ROLLBACK_PICK_TEXT, ROLLBACK_STALE, ROLLBACK_STARTED,
                                      ROLLBACK_USAGE)
from lazarus_backend.config import Config
from lazarus_backend.repo_manager import (MEMORY_DUMP, RepoError, RepoManager, RestoreRefused, git_environment,
                                          prepare_git_home, rmtree)
from lazarus_backend.stability_ledger import StabilityLedger
from lazarus_backend.update_router import (ROLLBACK_CANCEL, ROLLBACK_CONFIRM, ROLLBACK_PICK,
                                           rollback_button_data)
from tests.fakes import FakeClock, FakeLauncher, button_update, quiet_logs, text_update

OWNER = 1111
STRANGER = 2222

HISTORY = [("owner", "user", "What's the capital of France?"), ("owner", "assistant", "Paris.")]


class RepoFixture:
    """A bare remote with `main` and `live`, a Lazarus folder, and a ledger, all in a temp folder."""

    def __init__(self, test):
        self.test = test
        self.root = tempfile.mkdtemp()
        test.addCleanup(rmtree, self.root)
        home = os.path.join(self.root, "home")
        prepare_git_home(home)
        self.env = git_environment(home)
        self.now = 1762311600.0
        self.ledger = StabilityLedger(":memory:", clock=lambda: self.now)
        test.addCleanup(self.ledger.close)

        self.remote = os.path.join(self.root, "remote.git")
        self.git(self.root, "init", "--quiet", "--bare", self.remote)
        self.git(self.remote, "config", "receive.denyNonFastForwards", "true")
        self.git(self.remote, "config", "receive.denyDeletes", "true")

        self.seed = os.path.join(self.root, "seed")
        self.git(self.root, "init", "--quiet", self.seed)
        self.git(self.seed, "symbolic-ref", "HEAD", "refs/heads/main")
        self.write(self.seed, "README.md", "lazarus-ai on main\n")
        self.git(self.seed, "add", "--all")
        self.git(self.seed, "commit", "--quiet", "-m", "Initial")
        self.git(self.seed, "remote", "add", "origin", self.remote)
        self.git(self.seed, "push", "--quiet", "origin", "main", "main:live")
        self.initial = self.rev("live")

        self.lazarus = os.path.join(self.root, "lazarus-ai")
        self.write(self.lazarus, "README.md", "lazarus-ai\n")
        self.write(self.lazarus, ".gitignore", "*.db\n/config.local.json\n/logs/\n")
        self.write(self.lazarus, "lazarus_ai/__init__.py", "")
        self.write(self.lazarus, "lazarus_ai/app.py", "print('hello')\n")
        self.write(self.lazarus, "lazarus_ai/__pycache__/app.cpython-38.pyc", "bytecode")
        self.write(self.lazarus, "logs/lazarus.log", "a log line\n")
        self.write(self.lazarus, "config.json", '{"model": "phone"}\n')
        self.write(self.lazarus, "config.local.json", '{"model": "pc"}\n')
        self.memory = os.path.join(self.lazarus, "data", "memory.db")
        self.write_memory(HISTORY)

        self.clone = os.path.join(self.root, "backend", "data", "repo")

    def manager(self, **kwargs):
        kwargs.setdefault("clock", lambda: self.now)
        return RepoManager(self.lazarus, self.clone, self.remote, self.ledger, sleep=lambda seconds: None, **kwargs)

    # --- helpers

    def git(self, cwd, *args):
        result = subprocess.run(["git"] + list(args), cwd=cwd, env=self.env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode != 0:
            raise AssertionError("git {} failed: {}".format(" ".join(args), result.stderr.decode()))
        return result.stdout.decode("utf-8")

    def rev(self, ref):
        return self.git(self.remote, "rev-parse", ref).strip()

    def files(self, ref="live"):
        return sorted(self.git(self.remote, "ls-tree", "-r", "--name-only", ref).split())

    def show(self, path, ref="live"):
        return self.git(self.remote, "show", "{}:{}".format(ref, path))

    def message(self, ref="live"):
        return self.git(self.remote, "log", "-1", "--format=%B", ref)

    def parents(self, ref="live"):
        return self.git(self.remote, "log", "-1", "--format=%P", ref).split()

    def write(self, folder, relative, content):
        path = os.path.join(folder, *relative.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(content.encode("utf-8") if isinstance(content, str) else content)
        return path

    def write_memory(self, rows):
        os.makedirs(os.path.dirname(self.memory), exist_ok=True)
        db = sqlite3.connect(self.memory)
        try:
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, chat TEXT, "
                           "role TEXT, content TEXT)")
                db.execute("DELETE FROM history")
                db.executemany("INSERT INTO history (chat, role, content) VALUES (?, ?, ?)", rows)
        finally:
            db.close()

    def go_offline(self):
        os.rename(self.remote, self.remote + ".away")

    def go_online(self):
        os.rename(self.remote + ".away", self.remote)

    def push_commit(self, branch, files, message, base="origin/main", links=()):
        """Commit `files` (path: content) on top of `base` from the owner's PC and push it to `branch`.

        `links` are (path, target) symbolic links put into the commit with plumbing (works on Windows too).
        """
        self.git(self.seed, "fetch", "--quiet", "origin")
        self.git(self.seed, "checkout", "--quiet", "-B", "work", base)
        for path, content in files.items():
            self.write(self.seed, path, content)
        self.git(self.seed, "add", "--all")
        for path, target in links:
            blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=self.seed, env=self.env,
                                  input=target.encode("utf-8"), stdout=subprocess.PIPE, check=True).stdout
            self.git(self.seed, "update-index", "--add", "--cacheinfo", "120000,{},{}".format(
                blob.decode("ascii").strip(), path))
        self.git(self.seed, "commit", "--quiet", "-m", message)
        self.git(self.seed, "push", "--quiet", "origin", "work:refs/heads/" + branch)
        return self.git(self.seed, "rev-parse", "HEAD").strip()

    def read(self, relative):
        with open(os.path.join(self.lazarus, *relative.split("/")), "rb") as f:
            return f.read().decode("utf-8")

    def exists(self, relative):
        return os.path.lexists(os.path.join(self.lazarus, *relative.split("/")))

    def lazarus_files(self):
        """Every file in Lazarus's folder with its content: what "nothing changed" compares."""
        found = {}
        for folder, _, names in os.walk(self.lazarus):
            for name in names:
                path = os.path.join(folder, name)
                with open(path, "rb") as f:
                    found[os.path.relpath(path, self.lazarus).replace(os.sep, "/")] = f.read()
        return found

    def memory_rows(self):
        db = sqlite3.connect(self.memory)
        try:
            return db.execute("SELECT chat, role, content FROM history ORDER BY id").fetchall()
        finally:
            db.close()

    def is_ancestor(self, ancestor, descendant="live"):
        return subprocess.run(["git", "merge-base", "--is-ancestor", ancestor, descendant], cwd=self.remote,
                              env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode == 0

    def changed_files(self, old, new="live"):
        return self.git(self.remote, "diff", "--name-only", old, new).split()


def rows_from_dump(sql):
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(sql)
        return db.execute("SELECT chat, role, content FROM history ORDER BY id").fetchall()
    finally:
        db.close()


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.repo = RepoFixture(self)

    def test_snapshot_commits_code_and_a_readable_memory_dump_to_live_and_pushes_it(self):
        result = self.repo.manager().snapshot()

        self.assertTrue(result.created)
        self.assertTrue(result.pushed)
        self.assertEqual(result.commit, self.repo.rev("live"))
        self.assertEqual([self.repo.initial], self.repo.parents("live"))
        self.assertEqual([".gitignore", "README.md", "config.json", "lazarus_ai/__init__.py", "lazarus_ai/app.py",
                          MEMORY_DUMP], self.repo.files())  # config.json is tracked, config.local.json not
        self.assertEqual("print('hello')\n", self.repo.show("lazarus_ai/app.py"))
        self.assertEqual(HISTORY, rows_from_dump(self.repo.show(MEMORY_DUMP)))
        self.assertIn("INSERT INTO", self.repo.show(MEMORY_DUMP))
        self.assertEqual(self.repo.initial, self.repo.rev("main"))  # main is the owner's, never touched
        self.assertTrue(self.repo.message().startswith("Snapshot "))

    def test_deployment_and_push_are_recorded(self):
        manager = self.repo.manager()
        result = manager.snapshot()

        deployment = self.repo.ledger.current_deployment()
        self.assertEqual((result.commit, "live", self.repo.now), (deployment.sha, deployment.branch,
                                                                    deployment.deployed_at))
        self.assertEqual((result.commit, self.repo.now), (self.repo.ledger.last_push().sha,
                                                          self.repo.ledger.last_push().pushed_at))
        status = manager.status()
        self.assertEqual((result.commit[:7], "live", self.repo.now, 0),
                         (status.commit, status.branch, status.last_push, status.pending))

    def test_files_over_the_size_limit_are_left_out_and_listed_in_the_commit_message(self):
        self.repo.write(self.repo.lazarus, "models/big.bin", b"x" * 3000)
        self.repo.write(self.repo.lazarus, "models/small.bin", b"x" * 100)

        result = self.repo.manager(size_limit=2000).snapshot()

        self.assertNotIn("models/big.bin", self.repo.files())
        self.assertIn("models/small.bin", self.repo.files())
        self.assertIn("models/big.bin (0.0 MB, over the 0.0 MB limit)", result.skipped)
        self.assertIn("Skipped:\n- models/big.bin (", self.repo.message())

    def test_unchanged_folder_makes_no_new_commit(self):
        manager = self.repo.manager()
        first = manager.snapshot()
        second = manager.snapshot()

        self.assertFalse(second.created)
        self.assertEqual(first.commit, second.commit)
        self.assertEqual(first.commit, self.repo.rev("live"))

    def test_memory_change_alone_keeps_the_running_commit_and_a_code_change_deploys_a_new_one(self):
        manager = self.repo.manager()
        first = manager.snapshot()

        self.repo.write_memory(HISTORY + [("owner", "user", "And Spain?")])
        self.repo.now += 86400
        memory_only = manager.snapshot()
        self.assertTrue(memory_only.created)
        self.assertEqual(first.commit, self.repo.ledger.current_deployment().sha)

        self.repo.write(self.repo.lazarus, "lazarus_ai/app.py", "print('changed')\n")
        self.repo.now += 86400
        code = manager.snapshot()
        self.assertEqual(code.commit, self.repo.ledger.current_deployment().sha)
        self.assertEqual(2, len(self.repo.ledger.deployments()))

    def test_missing_memory_database_is_noted_and_the_code_still_committed(self):
        os.remove(self.repo.memory)
        self.repo.manager().snapshot()
        self.assertNotIn(MEMORY_DUMP, self.repo.files())
        self.assertIn("No memory database at data/memory.db.", self.repo.message())

    def test_damaged_memory_database_fails_the_snapshot_without_a_commit(self):
        with open(self.repo.memory, "wb") as f:
            f.write(b"this is not a database" * 100)
        manager = self.repo.manager()
        with self.assertLogs("lazarus_backend.repo_manager", "INFO"), self.assertRaises(RepoError):
            manager.snapshot()
        self.assertEqual(self.repo.initial, self.repo.rev("live"))

    def test_missing_lazarus_folder_fails_without_touching_live(self):
        rmtree(self.repo.lazarus)
        with self.assertRaises(RepoError):
            self.repo.manager().snapshot()
        self.assertEqual(self.repo.initial, self.repo.rev("live"))


class LazarusCannotReachRootTest(unittest.TestCase):
    """Nothing Lazarus writes into its folder makes the backend run code or copy git metadata."""

    def setUp(self):
        self.repo = RepoFixture(self)
        self.marker = os.path.join(self.repo.root, "executed")
        marker = self.marker.replace("\\", "/")
        hook = "#!/bin/sh\necho hook >> '{}'\n".format(marker)
        planted = os.path.join(self.repo.lazarus, ".git")
        for name in ("pre-commit", "post-commit", "pre-push", "reference-transaction", "post-checkout"):
            path = self.repo.write(planted, "hooks/" + name, hook)
            os.chmod(path, 0o755)
        evil_config = ("[core]\n\thooksPath = {hooks}\n\tfsmonitor = {hooks}/pre-commit\n"
                       "[filter \"evil\"]\n\tclean = sh -c \"echo filter >> '{marker}'; cat\"\n\trequired = true\n"
                       .format(hooks=os.path.join(planted, "hooks").replace("\\", "/"), marker=marker))
        self.repo.write(planted, "config", evil_config)
        self.repo.write(planted, "HEAD", "ref: refs/heads/live\n")
        self.repo.write(self.repo.lazarus, "vendor/lib/.git", "gitdir: ../../../elsewhere\n")
        self.repo.write(self.repo.lazarus, "vendor/lib/lib.py", "x = 1\n")
        self.repo.write(self.repo.lazarus, ".gitattributes", "* filter=evil\n*.txt text eol=crlf\n")
        self.repo.write(self.repo.lazarus, ".gitignore", "*\n")
        self.repo.write(self.repo.lazarus, "notes.txt", b"line one\nline two\n")
        # A global config that git would read if the backend didn't isolate it.
        self.evil_global = self.repo.write(self.repo.root, "evil.gitconfig", evil_config)

    def test_planted_git_folder_and_hooks_are_neither_copied_nor_run(self):
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": self.evil_global, "GIT_DIR": self.repo.lazarus}):
            result = self.repo.manager().snapshot()

        self.assertTrue(result.pushed)
        self.assertFalse(os.path.exists(self.marker), "a planted hook or filter ran")
        files = self.repo.files()
        self.assertFalse([f for f in files if ".git/" in f or f.endswith("/.git") or f == ".git"], files)
        self.assertIn("vendor/lib/lib.py", files)
        self.assertIn("notes.txt", files)  # Lazarus's .gitignore has no say
        self.assertEqual("line one\nline two\n", self.repo.show("notes.txt"))  # no filter, no eol conversion
        self.assertIn(".git (git metadata, never copied)", result.skipped)
        self.assertIn("vendor/lib/.git (git metadata, never copied)", result.skipped)
        clone_hooks = os.path.join(self.repo.clone, ".git", "hooks")
        self.assertFalse(os.path.isdir(clone_hooks) and os.listdir(clone_hooks))

    def test_symbolic_link_is_skipped_not_followed(self):
        secret = self.repo.write(self.repo.root, "secrets/deploy_key", "PRIVATE KEY")
        try:
            os.symlink(secret, os.path.join(self.repo.lazarus, "key"))
            os.symlink(os.path.dirname(secret), os.path.join(self.repo.lazarus, "secrets"),
                       target_is_directory=True)
        except (OSError, NotImplementedError) as e:
            self.skipTest("can't create symbolic links here: {}".format(e))

        result = self.repo.manager().snapshot()

        self.assertNotIn("key", self.repo.files())
        self.assertNotIn("secrets/deploy_key", self.repo.files())
        self.assertIn("key (symbolic link)", result.skipped)
        self.assertIn("secrets (symbolic link)", result.skipped)


class PushLaterTest(unittest.TestCase):
    def setUp(self):
        self.repo = RepoFixture(self)
        self.addCleanup(self._back_online)

    def _back_online(self):
        if os.path.exists(self.repo.remote + ".away"):
            self.repo.go_online()

    def test_remote_refuses_rewrites_like_github(self):
        manager = self.repo.manager()
        manager.snapshot()
        with self.assertRaises(AssertionError):
            self.repo.git(self.repo.seed, "push", "--quiet", "--force", "origin", "main:live")
        with self.assertRaises(AssertionError):
            self.repo.git(self.repo.seed, "push", "--quiet", "origin", ":live")

    def test_failed_push_keeps_the_commit_and_pushes_it_on_the_next_connection(self):
        manager = self.repo.manager()
        online = manager.snapshot()
        self.repo.write_memory(HISTORY + [("owner", "user", "Are you there?")])
        self.repo.go_offline()
        with self.assertLogs("lazarus_backend.repo_manager", "WARNING"):
            offline = manager.snapshot()
        self.assertTrue(offline.created)
        self.assertFalse(offline.pushed)
        self.assertEqual(1, manager.pending_count())
        self.assertEqual(online.commit, self.repo.ledger.last_push().sha)

        self.repo.write_memory(HISTORY + [("owner", "user", "Still offline?")])
        with self.assertLogs("lazarus_backend.repo_manager", "WARNING"):
            second = manager.snapshot()
            self.assertFalse(manager.push_pending())
        self.assertEqual(2, manager.pending_count())

        self.repo.go_online()
        self.assertEqual(online.commit, self.repo.rev("live"))
        self.assertTrue(manager.push_pending())

        self.assertEqual(second.commit, self.repo.rev("live"))
        self.assertEqual([offline.commit], self.repo.parents("live"))
        self.assertEqual([online.commit], self.repo.parents(offline.commit))
        self.assertEqual(0, manager.pending_count())
        self.assertEqual(second.commit, self.repo.ledger.last_push().sha)

    def test_a_remote_that_moved_on_meanwhile_is_joined_without_rewriting_either_side(self):
        manager = self.repo.manager()
        self.repo.go_offline()
        with self.assertLogs("lazarus_backend.repo_manager", "WARNING"):
            offline = manager.snapshot()
        self.repo.go_online()
        # The owner merges main into live on the PC and pushes while the phone is offline.
        self.repo.git(self.repo.seed, "checkout", "--quiet", "-b", "live-pc", "origin/live")
        self.repo.write(self.repo.seed, "FROM_PC.md", "owner's change\n")
        self.repo.git(self.repo.seed, "add", "--all")
        self.repo.git(self.repo.seed, "commit", "--quiet", "-m", "Owner's change")
        self.repo.git(self.repo.seed, "push", "--quiet", "origin", "live-pc:live")
        owners = self.repo.rev("live")

        self.assertTrue(manager.push_pending())

        self.assertEqual([offline.commit, owners], self.repo.parents("live"))
        self.assertEqual(self.repo.git(self.repo.remote, "rev-parse", offline.commit + "^{tree}"),
                         self.repo.git(self.repo.remote, "rev-parse", "live^{tree}"))
        self.assertEqual(0, manager.pending_count())

    def test_first_snapshot_offline_is_joined_to_the_existing_live_later(self):
        manager = self.repo.manager()
        self.repo.go_offline()
        with self.assertLogs("lazarus_backend.repo_manager", "WARNING"):
            offline = manager.snapshot()
        self.assertEqual(1, manager.pending_count())
        self.repo.go_online()

        self.assertTrue(manager.push_pending())

        self.assertEqual({offline.commit, self.repo.initial}, set(self.repo.parents("live")))
        self.assertIn(MEMORY_DUMP, self.repo.files())


LATER = HISTORY + [("owner", "user", "And Spain?"), ("owner", "assistant", "Madrid.")]


class RestoreTest(unittest.TestCase):
    """A snapshot (`first`), then Lazarus changes its code, config and memory, then the owner rolls back."""

    def setUp(self):
        self.repo = RepoFixture(self)
        self.manager = self.repo.manager()
        self.calls = []
        self.first = self.manager.snapshot()
        self.repo.now += 3600
        self.repo.write(self.repo.lazarus, "lazarus_ai/app.py", "print('changed')\n")
        self.repo.write(self.repo.lazarus, "lazarus_ai/tools/new.py", "x = 1\n")
        self.repo.write(self.repo.lazarus, "config.json", '{"model": "changed"}\n')
        self.repo.write_memory(LATER)

    def restore(self, sha, with_memory=False, stop=None):
        return self.manager.restore(sha, with_memory=with_memory, stop_lazarus=stop or self._stop,
                                    start_lazarus=lambda: self.calls.append("start"))

    def _stop(self):
        self.calls.append("stop")

    def assert_nothing_changed(self, files, live):
        self.assertEqual([], self.calls)
        self.assertEqual(files, self.repo.lazarus_files())
        self.assertEqual(LATER, self.repo.memory_rows())
        self.assertEqual(live, self.repo.rev("live"))

    def test_code_only_restore_brings_back_code_and_config_and_keeps_the_current_memory(self):
        result = self.restore(self.first.commit[:7])

        self.assertEqual(["stop", "start"], self.calls)
        self.assertEqual("print('hello')\n", self.repo.read("lazarus_ai/app.py"))
        self.assertEqual('{"model": "phone"}\n', self.repo.read("config.json"))  # tracked: restored like code
        self.assertFalse(self.repo.exists("lazarus_ai/tools/new.py"))
        self.assertFalse(self.repo.exists("lazarus_ai/tools"))
        self.assertEqual(LATER, self.repo.memory_rows())
        # Excluded and untracked files stay as they are.
        self.assertEqual('{"model": "pc"}\n', self.repo.read("config.local.json"))
        self.assertTrue(self.repo.exists("logs/lazarus.log"))
        self.assertTrue(self.repo.exists("lazarus_ai/__pycache__/app.cpython-38.pyc"))

        self.assertEqual((self.first.commit, "live", False), (result.target, result.target_branch, result.with_memory))
        self.assertEqual(result.commit, self.repo.rev("live"))
        self.assertTrue(result.pushed)
        self.assertEqual([MEMORY_DUMP], self.repo.changed_files(self.first.commit))  # the code is the target's
        self.assertEqual(LATER, rows_from_dump(self.repo.show(MEMORY_DUMP)))         # the dump stays current
        self.assertTrue(self.repo.message().startswith("Roll back to {} (code only)".format(self.first.commit[:7])))
        self.assertEqual(result.commit, self.repo.ledger.current_deployment().sha)
        self.assertEqual("live", self.repo.ledger.current_deployment().branch)

    def test_pre_rollback_snapshot_holds_the_replaced_state_and_live_only_grows(self):
        result = self.restore(self.first.commit)

        self.assertEqual([result.snapshot], self.repo.parents("live"))
        self.assertEqual([self.first.commit], self.repo.parents(result.snapshot))
        self.assertEqual("print('changed')\n", self.repo.show("lazarus_ai/app.py", result.snapshot))
        self.assertEqual('{"model": "changed"}\n', self.repo.show("config.json", result.snapshot))
        self.assertIn("lazarus_ai/tools/new.py", self.repo.files(result.snapshot))
        self.assertEqual(LATER, rows_from_dump(self.repo.show(MEMORY_DUMP, result.snapshot)))
        self.assertTrue(self.repo.is_ancestor(self.first.commit))  # a new commit, never a rewrite

    def test_restore_with_memory_rebuilds_the_database_from_the_commit(self):
        def stop():
            self.calls.append("stop")
            # A stopped Lazarus can leave a WAL behind; it belongs to the old database and must go.
            self.repo.write(self.repo.lazarus, "data/memory.db-wal", b"stale write-ahead log" * 20)

        result = self.restore(self.first.commit, with_memory=True, stop=stop)

        self.assertEqual(["stop", "start"], self.calls)
        self.assertEqual(HISTORY, self.repo.memory_rows())
        self.assertFalse(self.repo.exists("data/memory.db-wal"))
        self.assertEqual("print('hello')\n", self.repo.read("lazarus_ai/app.py"))
        self.assertTrue(result.with_memory)
        self.assertEqual([], self.repo.changed_files(self.first.commit))  # the target's tree exactly
        self.assertEqual(LATER, rows_from_dump(self.repo.show(MEMORY_DUMP, result.snapshot)))
        self.assertIn("(with memory)", self.repo.message())

    def test_restore_with_memory_works_when_the_current_database_is_damaged(self):
        with open(self.repo.memory, "wb") as f:
            f.write(b"this is not a database" * 100)

        with self.assertLogs("lazarus_backend.repo_manager", "INFO"):
            result = self.restore(self.first.commit, with_memory=True)

        self.assertEqual(HISTORY, self.repo.memory_rows())
        self.assertIn("can't dump the memory database", self.repo.message(result.snapshot))
        self.assertIn("memory.sql is the previous commit's", self.repo.message(result.snapshot))

    def test_restore_from_a_main_commit_runs_the_owners_code_with_the_current_memory(self):
        owners = self.repo.push_commit("main", {"lazarus_ai/__init__.py": "", "lazarus_ai/app.py": "print('main')\n",
                                                "config.json": '{"model": "main"}\n'}, "New chat engine")

        result = self.restore(owners[:10])

        self.assertEqual(("main", "New chat engine", owners), (result.target_branch, result.subject, result.target))
        self.assertEqual("print('main')\n", self.repo.read("lazarus_ai/app.py"))
        self.assertEqual('{"model": "main"}\n', self.repo.read("config.json"))
        self.assertEqual("lazarus-ai on main\n", self.repo.read("README.md"))
        self.assertFalse(self.repo.exists(".gitignore"))  # Lazarus's, committed in the snapshot, not on main
        self.assertEqual(LATER, self.repo.memory_rows())
        self.assertEqual(owners, self.repo.rev("main"))  # main is the owner's, never touched
        self.assertEqual([MEMORY_DUMP], self.repo.changed_files(owners))
        self.assertEqual([result.snapshot], self.repo.parents("live"))

    def test_restore_with_memory_from_a_commit_without_a_dump_is_refused(self):
        owners = self.repo.push_commit("main", {"lazarus_ai/app.py": "print('main')\n"}, "No memory here")
        files, live = self.repo.lazarus_files(), self.repo.rev("live")

        with self.assertRaisesRegex(RestoreRefused, "no memory dump"):
            self.restore(owners, with_memory=True)

        self.assert_nothing_changed(files, live)

    def test_rolling_back_to_the_commit_before_the_rollback_undoes_it(self):
        rollback = self.restore(self.first.commit)
        undo = self.restore(rollback.snapshot[:7])

        self.assertEqual("print('changed')\n", self.repo.read("lazarus_ai/app.py"))
        self.assertEqual("x = 1\n", self.repo.read("lazarus_ai/tools/new.py"))
        self.assertEqual('{"model": "changed"}\n', self.repo.read("config.json"))
        self.assertEqual(LATER, self.repo.memory_rows())
        self.assertEqual([], self.repo.changed_files(rollback.snapshot))
        # Nothing changed between the two rollbacks, so the first one's commit is the second's "before".
        self.assertEqual(rollback.commit, undo.snapshot)
        self.assertEqual([rollback.commit], self.repo.parents("live"))
        for earlier in (self.first.commit, rollback.snapshot, rollback.commit):
            self.assertTrue(self.repo.is_ancestor(earlier), earlier)
        self.assertEqual([undo.commit, rollback.commit], [d.sha for d in self.repo.ledger.deployments(limit=2)])

    def test_unknown_unpushed_or_foreign_sha_is_refused_and_changes_nothing(self):
        feature = self.repo.push_commit("feature", {"lazarus_ai/app.py": "print('feature')\n"}, "Not on main or live")
        self.repo.go_offline()
        with self.assertLogs("lazarus_backend.repo_manager", "WARNING"):
            unpushed = self.manager.snapshot().commit  # waits for a push: not on the remote
        self.repo.go_online()
        files, live = self.repo.lazarus_files(), self.repo.rev("live")

        for sha in ("deadbeef", "DEADBEEF", "hello", "", "abc", feature, unpushed, unpushed[:7]):
            with self.subTest(sha=sha), self.assertRaises(RestoreRefused):
                self.restore(sha)

        self.assert_nothing_changed(files, live)
        self.assertEqual(1, self.manager.pending_count())

    def test_restore_while_offline_is_refused_and_changes_nothing(self):
        files, live = self.repo.lazarus_files(), self.repo.rev("live")
        self.repo.go_offline()
        try:
            with self.assertLogs("lazarus_backend.repo_manager", "WARNING"), \
                    self.assertRaisesRegex(RestoreRefused, "can't be reached"):
                self.restore(self.first.commit)
        finally:
            self.repo.go_online()
        self.assert_nothing_changed(files, live)

    def test_excluded_files_links_and_the_memory_database_of_a_commit_are_never_written(self):
        owners = self.repo.push_commit("main", {
            "lazarus_ai/app.py": "print('main')\n",
            "config.local.json": '{"model": "from git"}\n',
            "data/memory.db": "not a database",
            "logs/other.log": "log\n",
            "lazarus_ai/__pycache__/x.cpython-38.pyc": "bytecode",
        }, "Junk in the commit", links=[("evil-link", "/etc/shadow")])

        result = self.restore(owners)

        self.assertEqual("print('main')\n", self.repo.read("lazarus_ai/app.py"))
        self.assertEqual('{"model": "pc"}\n', self.repo.read("config.local.json"))
        self.assertEqual(LATER, self.repo.memory_rows())
        self.assertFalse(self.repo.exists("logs/other.log"))
        self.assertFalse(self.repo.exists("lazarus_ai/__pycache__/x.cpython-38.pyc"))
        self.assertFalse(self.repo.exists("evil-link"))
        self.assertIn("evil-link (symbolic link)", result.skipped)
        self.assertIn("Not restored:\n- evil-link (symbolic link)", self.repo.message())

    def test_links_planted_in_lazarus_folder_are_replaced_not_written_through(self):
        secret = self.repo.write(self.repo.root, "secrets/deploy_key", "PRIVATE KEY")
        outside = os.path.join(self.repo.root, "outside")
        os.makedirs(outside)
        app = os.path.join(self.repo.lazarus, "lazarus_ai", "app.py")
        os.remove(app)
        try:
            os.symlink(secret, app)
            os.symlink(outside, os.path.join(self.repo.lazarus, "docs"), target_is_directory=True)
        except (OSError, NotImplementedError) as e:
            self.skipTest("can't create symbolic links here: {}".format(e))
        owners = self.repo.push_commit("main", {"lazarus_ai/app.py": "print('main')\n", "docs/guide.md": "guide\n"},
                                       "With docs")

        self.restore(owners)

        with open(secret) as f:
            self.assertEqual("PRIVATE KEY", f.read())
        self.assertEqual([], os.listdir(outside))
        self.assertFalse(os.path.islink(app))
        self.assertEqual("print('main')\n", self.repo.read("lazarus_ai/app.py"))
        self.assertFalse(os.path.islink(os.path.join(self.repo.lazarus, "docs")))
        self.assertEqual("guide\n", self.repo.read("docs/guide.md"))

    def test_memory_dump_that_attaches_another_database_is_refused(self):
        evil = os.path.join(self.repo.root, "evil.db").replace("\\", "/")
        dump = "BEGIN TRANSACTION;\nATTACH DATABASE '{}' AS evil;\nCREATE TABLE evil.x (a);\nCOMMIT;\n".format(evil)
        owners = self.repo.push_commit("main", {MEMORY_DUMP: dump}, "A dump that writes elsewhere")
        files, live = self.repo.lazarus_files(), self.repo.rev("live")

        with self.assertRaisesRegex(RestoreRefused, "can't be loaded"):
            self.restore(owners, with_memory=True)

        self.assertFalse(os.path.exists(evil))
        self.assert_nothing_changed(files, live)


class ListCommitsTest(unittest.TestCase):
    def test_lists_the_latest_commits_of_main_and_live_newest_first(self):
        repo = RepoFixture(self)
        manager = repo.manager()
        first = manager.snapshot()
        repo.write(repo.lazarus, "lazarus_ai/app.py", "print('changed')\n")
        second = manager.snapshot()
        owners = repo.push_commit("main", {"x.py": "x = 1\n"}, "Owner's change")

        commits = manager.list_commits(count=2)

        self.assertEqual([(owners, "main", "Owner's change"), (repo.initial, "main", "Initial"),
                          (second.commit, "live", repo.message(second.commit).splitlines()[0]),
                          (first.commit, "live", repo.message(first.commit).splitlines()[0])],
                         [(c.sha, c.branch, c.subject) for c in commits])
        self.assertEqual(owners[:7], commits[0].short)
        self.assertIsInstance(commits[0].date, float)


class SentMessages:
    """Telegram stand-in for the Backend: records what it sends, edits and answers."""

    def __init__(self):
        self.sent = []
        self.buttons = []   # (text, rows of (label, data)) of messages sent with buttons
        self.edits = []     # (message_id, text, buttons)
        self.answers = []   # (callback_id, text)

    def send_message(self, chat_id, text, buttons=None):
        self.sent.append(text)
        if buttons:
            self.buttons.append((text, buttons))

    def edit_message(self, chat_id, message_id, text, buttons=None):
        self.edits.append((message_id, text, buttons))

    def answer_callback(self, callback_id, text=None):
        self.answers.append((callback_id, text))

    def send_typing(self, chat_id):
        pass


class BackendWiringTest(unittest.TestCase):
    def setUp(self):
        self.repo = RepoFixture(self)
        self.telegram = SentMessages()

    def backend(self, manager):
        config = Config(telegram_user_id=OWNER, api_port=0, outbox_db=":memory:", inbound_db=":memory:")
        backend = Backend(config, self.telegram, lazarus=mock.Mock(), repo=manager)
        self.addCleanup(backend.outbox.stop)
        self.addCleanup(backend.inbound.stop)
        return backend

    def status_reply(self, backend):
        backend.handle_update(text_update(1, OWNER, "/status"))
        self.assertEqual(0, backend.outbox.deliver_next())
        return self.telegram.sent[-1]

    def test_status_shows_the_running_commit_and_the_last_push(self):
        manager = self.repo.manager()
        backend = self.backend(manager)
        self.assertIn("\nCommit: ?\nLast push: ?", self.status_reply(backend))

        result = manager.snapshot()

        pushed = datetime.datetime.fromtimestamp(self.repo.now).strftime("%Y-%m-%d %H:%M")
        self.assertIn("\nCommit: {} on live\nLast push: {}".format(result.commit[:7], pushed),
                      self.status_reply(backend))

    def test_failed_nightly_snapshot_is_reported_to_the_owner(self):
        rmtree(self.repo.lazarus)
        backend = self.backend(self.repo.manager())
        clock = [datetime.datetime(2026, 11, 5, 2, 59)]
        backend.snapshots._now = lambda: clock[0]
        backend.snapshots.tick()
        clock[0] += datetime.timedelta(minutes=1)
        with self.assertLogs("lazarus_backend", "ERROR"):
            backend.snapshots.tick()

        self.assertEqual(0, backend.outbox.deliver_next())
        self.assertTrue(self.telegram.sent[-1].startswith(SNAPSHOT_FAILED.format("")), self.telegram.sent)
        self.assertIn("doesn't exist", self.telegram.sent[-1])


class RollbackWiringTest(unittest.TestCase):
    """/rollback and its buttons through the wired Backend, with a Supervisor running a fake Lazarus."""

    def setUp(self):
        quiet_logs(self)
        self.repo = RepoFixture(self)
        self.manager = self.repo.manager()
        self.first = self.manager.snapshot()
        self.repo.write(self.repo.lazarus, "lazarus_ai/app.py", "print('changed')\n")
        self.repo.write_memory(LATER)
        self.telegram = SentMessages()
        self.launcher = FakeLauncher()
        self.supervisor = supervision.Supervisor(self.launcher, lambda: True, supervision.SupervisorState(":memory:"),
                                                 clock=FakeClock())
        self.supervisor.tick()  # Lazarus runs
        config = Config(telegram_user_id=OWNER, api_port=0, outbox_db=":memory:", inbound_db=":memory:")
        self.backend = Backend(config, self.telegram, lazarus=mock.Mock(), supervisor=self.supervisor,
                               repo=self.manager)
        self.backend.run_repo_task = lambda task: task()  # in place instead of on a thread
        self.addCleanup(self.backend.outbox.stop)
        self.addCleanup(self.backend.inbound.stop)
        self.files, self.live = self.repo.lazarus_files(), self.repo.rev("live")

    def replies(self):
        return [item.text for item in self.backend.outbox.pending()]

    def command(self, text):
        self.backend.handle_update(text_update(1, OWNER, text))

    def press(self, data, user=OWNER, callback_id="cb1"):
        self.backend.handle_update(button_update(2, user, data, message_id=30, callback_id=callback_id))

    def assert_lazarus_restarted(self):
        self.assertTrue(self.launcher.processes[0].stopped)
        self.assertEqual(2, len(self.launcher.processes))
        self.assertEqual(1, len(self.launcher.running))
        self.assertNotEqual(supervision.STOPPED, self.supervisor.state())

    def assert_nothing_changed(self):
        self.assertEqual(self.files, self.repo.lazarus_files())
        self.assertEqual(self.live, self.repo.rev("live"))
        self.assertEqual(1, len(self.launcher.processes))
        self.assertEqual(1, len(self.launcher.running))

    def test_rollback_sha_runs_its_code_with_the_current_memory_and_confirms(self):
        self.command("/rollback " + self.first.commit[:7])

        started, done = self.replies()
        self.assertEqual(ROLLBACK_STARTED.format(self.first.commit[:7], "code only"), started)
        self.assertEqual("print('hello')\n", self.repo.read("lazarus_ai/app.py"))
        self.assertEqual(LATER, self.repo.memory_rows())
        self.assert_lazarus_restarted()
        before = self.repo.parents("live")[0]
        self.assertIn("Lazarus runs {} now (live: Snapshot ".format(self.first.commit[:7]), done)
        self.assertIn("memory kept as it was", done)
        self.assertIn("Deployed as {} on live.".format(self.repo.rev("live")[:7]), done)
        self.assertIn("/rollback {} undoes this".format(before[:7]), done)

    def test_rollback_sha_memory_also_brings_back_the_conversation(self):
        self.command("/rollback {} Memory".format(self.first.commit))

        self.assertEqual(HISTORY, self.repo.memory_rows())
        self.assertIn("memory restored from {}".format(self.first.commit[:7]), self.replies()[-1])
        self.assert_lazarus_restarted()

    def test_unknown_sha_gets_an_error_and_changes_nothing(self):
        self.command("/rollback deadbeef")

        self.assertEqual("Can't roll back to deadbeef: it isn't a pushed commit of main or live. Nothing was changed.",
                         self.replies()[-1])
        self.assert_nothing_changed()

    def test_wrong_arguments_get_the_usage(self):
        for text in ("/rollback abc1234 code", "/rollback a b c"):
            self.command(text)
        self.assertEqual([ROLLBACK_USAGE, ROLLBACK_USAGE], self.replies())
        self.assert_nothing_changed()

    def test_buttons_list_both_branches_and_a_confirmed_press_deploys_that_state(self):
        self.command("/rollback")

        self.assertEqual([], self.replies())
        (text, rows), = self.telegram.buttons
        self.assertEqual(ROLLBACK_PICK_TEXT, text)
        labels = [row[0][0] for row in rows]
        self.assertTrue(labels[0].startswith("{} main ".format(self.repo.initial[:7])), labels)
        self.assertTrue(labels[1].startswith("{} live ".format(self.first.commit[:7])), labels)
        pick = rows[1][0][1]
        self.assertEqual(rollback_button_data(ROLLBACK_PICK, self.first.commit), pick)

        self.press(pick)
        self.assert_nothing_changed()  # picking only asks
        message_id, question, confirm = self.telegram.edits[-1]
        self.assertEqual(30, message_id)
        self.assertTrue(question.startswith("Roll back to {} (live, ".format(self.first.commit[:7])), question)
        self.assertEqual([rollback_button_data(ROLLBACK_CONFIRM, self.first.commit),
                          rollback_button_data(ROLLBACK_CANCEL)], [data for _, data in confirm[0]])

        self.press(confirm[0][0][1])
        self.assertEqual("print('hello')\n", self.repo.read("lazarus_ai/app.py"))
        self.assertEqual(LATER, self.repo.memory_rows())
        self.assert_lazarus_restarted()
        self.assertTrue(self.telegram.edits[-1][1].startswith("Rolling back to {}".format(self.first.commit[:7])))
        self.assertIsNone(self.telegram.edits[-1][2])  # the buttons are gone
        self.assertIn("Lazarus runs {} now".format(self.first.commit[:7]), self.replies()[-1])
        self.assertEqual([("cb1", None), ("cb1", None)], self.telegram.answers)

        live = self.repo.rev("live")
        self.press(confirm[0][0][1], callback_id="cb2")  # the used-up list can't deploy again
        self.assertEqual(("cb2", ROLLBACK_STALE), self.telegram.answers[-1])
        self.assertEqual(live, self.repo.rev("live"))

    def test_cancel_changes_nothing(self):
        self.command("/rollback")
        self.press(self.telegram.buttons[0][1][1][0][1])
        self.press(rollback_button_data(ROLLBACK_CANCEL))

        self.assertEqual(ROLLBACK_CANCELLED, self.telegram.edits[-1][1])
        self.assert_nothing_changed()

    def test_presses_for_shas_never_offered_or_by_strangers_change_nothing(self):
        self.press(rollback_button_data(ROLLBACK_CONFIRM, self.first.commit))  # a real commit, but no list
        self.assertEqual([("cb1", ROLLBACK_STALE)], self.telegram.answers)

        self.command("/rollback")
        self.press(rollback_button_data(ROLLBACK_CONFIRM, self.first.commit), user=STRANGER, callback_id="cb3")
        self.assertEqual([("cb1", ROLLBACK_STALE)], self.telegram.answers)  # a stranger gets no answer at all
        self.assertEqual([], self.telegram.edits)
        self.assert_nothing_changed()


if __name__ == "__main__":
    unittest.main()
