"""The wired backend with a Supervisor (fake clock, fake launcher): /stop, /restart, /log, the Lazarus
state in /status, and owner messages queued while Lazarus is down, against a fake Telegram and Inbox."""

import os
import shutil
import tempfile
import time
import unittest

from lazarus_backend.app import LAZARUS_DOWN_QUEUED, LAZARUS_STOPPED_QUEUED, Backend
from lazarus_backend.commands import (ALREADY_STOPPED_REPLY, LOG_EMPTY, LOG_HEADER, NOT_SUPERVISED, RESTART_REPLY,
                                      START_REPLY, STOP_REPLY)
from lazarus_backend.config import Config
from lazarus_backend.local_api import LazarusClient
from lazarus_backend.supervisor import Supervisor, SupervisorState
from lazarus_backend.telegram_client import TelegramClient
from tests.fakes import FakeClock, FakeLauncher, FakeTelegram, RecordingServer, quiet_logs, text_update

OWNER = 1111


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)
        self.telegram = FakeTelegram()
        self.addCleanup(self.telegram.close)
        self.inbox = RecordingServer(lambda path, body: (202, {"ok": True}))
        self.addCleanup(self.inbox.close)
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.log_path = os.path.join(self.dir, "logs", "lazarus.log")
        self.clock = FakeClock()
        self.launcher = FakeLauncher()
        self.update_id = 0
        self.healthy = True

    def backend(self, supervised=True):
        config = Config(telegram_user_id=OWNER, api_port=0, lazarus_url=self.inbox.url,
                        telegram_api_url=self.telegram.url, poll_timeout=1, outbox_db=":memory:",
                        inbound_db=":memory:", supervisor_db=":memory:", lazarus_log=self.log_path)
        lazarus = LazarusClient(self.inbox.url, timeout=2)
        self.supervisor = None
        if supervised:
            state = SupervisorState(config.supervisor_db)
            self.addCleanup(state.close)
            # Health is "the process runs": the fake launcher's process stands for the Inbox.
            self.supervisor = Supervisor(self.launcher, lambda: self.healthy and bool(self.launcher.running), state,
                                         clock=self.clock, wall_clock=self.clock.wall)
            self.supervisor.run_in_background = lambda *args, **kwargs: None  # the test ticks by hand
        backend = Backend(config, TelegramClient("123:token", api_url=self.telegram.url), lazarus,
                          supervisor=self.supervisor)
        backend.start()
        self.addCleanup(backend.stop)
        return backend

    def send(self, backend, text):
        self.update_id += 1
        backend.handle_update(text_update(self.update_id, OWNER, text, message_id=100 + self.update_id))

    def replies(self, backend):
        """Every reply sent since the last clear, once the Outbox has sent all it holds."""
        self.telegram.wait_for("/sendMessage")
        deadline = time.monotonic() + 5
        while backend.outbox.pending() and time.monotonic() < deadline:
            time.sleep(0.01)
        return [body["text"] for body in self.telegram.bodies("/sendMessage")]

    def answer_to(self, backend, text):
        self.telegram.requests.clear()
        self.send(backend, text)
        return "\n".join(self.replies(backend))

    def test_status_shows_starting_up_restarting_and_stopped_by_owner(self):
        backend = self.backend()
        self.assertIn("Lazarus: starting", self.answer_to(backend, "/status"))
        self.healthy = False
        self.supervisor.tick()  # started
        self.supervisor.tick()  # no health answer yet
        self.assertIn("Lazarus: starting", self.answer_to(backend, "/status"))
        self.healthy = True
        self.supervisor.tick()
        self.assertIn("Lazarus: up", self.answer_to(backend, "/status"))
        self.healthy = False
        self.launcher.running[0].exit(1)
        self.supervisor.tick()
        self.assertIn("Lazarus: down (restarting)", self.answer_to(backend, "/status"))
        self.clock.advance(1)
        self.supervisor.tick()  # started again
        self.assertIn("Lazarus: starting", self.answer_to(backend, "/status"))
        self.healthy = True
        self.supervisor.tick()
        self.assertIn("Lazarus: up", self.answer_to(backend, "/status"))
        self.answer_to(backend, "/stop")
        self.assertIn("Lazarus: stopped by owner", self.answer_to(backend, "/status"))

    def test_messages_sent_while_stopped_get_one_reply_and_arrive_in_order_after_restart(self):
        backend = self.backend()
        self.supervisor.tick()
        self.supervisor.tick()
        self.assertEqual(STOP_REPLY, self.answer_to(backend, "/stop"))
        self.assertEqual([], self.launcher.running)
        self.assertEqual(ALREADY_STOPPED_REPLY, self.answer_to(backend, "/stop"))

        self.telegram.requests.clear()
        for text in ("one", "two", "three"):
            self.send(backend, text)
        self.assertEqual([LAZARUS_STOPPED_QUEUED], self.replies(backend))
        self.assertEqual([], self.inbox.bodies("/message"))

        self.assertEqual(START_REPLY, self.answer_to(backend, "/restart"))
        self.assertEqual(1, len(self.launcher.running))
        self.assertEqual([], self.inbox.bodies("/message"), "not before Lazarus answers health")
        self.supervisor.tick()  # healthy: the queue is delivered
        bodies = self.inbox.wait_for("/message", count=3)
        self.assertEqual(["one", "two", "three"], [body["text"] for body in bodies])
        self.assertEqual(["103", "104", "105"], [body["id"] for body in bodies])

    def test_messages_while_lazarus_restarts_after_a_crash_get_the_down_reply(self):
        backend = self.backend()
        self.supervisor.tick()
        self.supervisor.tick()
        self.launcher.running[0].exit(1)
        self.supervisor.tick()
        self.telegram.requests.clear()
        self.send(backend, "are you there?")
        self.send(backend, "hello?")
        self.assertEqual([LAZARUS_DOWN_QUEUED], self.replies(backend))

        self.clock.advance(1)
        self.supervisor.tick()  # restarted
        self.supervisor.tick()  # healthy
        bodies = self.inbox.wait_for("/message", count=2)
        self.assertEqual(["are you there?", "hello?"], [body["text"] for body in bodies])

    def test_restart_of_a_running_lazarus(self):
        backend = self.backend()
        self.supervisor.tick()
        self.supervisor.tick()
        self.assertEqual(RESTART_REPLY, self.answer_to(backend, "/restart"))
        self.assertEqual(2, len(self.launcher.processes))
        self.assertTrue(self.launcher.processes[0].stopped)

    def test_log_shows_the_last_30_lines_of_lazarus_output(self):
        backend = self.backend()
        self.assertEqual(LOG_EMPTY, self.answer_to(backend, "/log"))
        os.makedirs(os.path.dirname(self.log_path))
        with open(self.log_path, "w", encoding="utf-8") as f:
            for i in range(40):
                f.write("line {}\n".format(i))
            f.write("Traceback (most recent call last):\n")
            f.write("SyntaxError: invalid syntax\n")
        answer = self.answer_to(backend, "/log").split("\n")
        self.assertEqual(LOG_HEADER.format(30), answer[0])
        self.assertEqual(["line 12"], answer[1:2])
        self.assertEqual(["Traceback (most recent call last):", "SyntaxError: invalid syntax"], answer[-2:])

    def test_without_a_supervisor_the_lifecycle_commands_say_so(self):
        backend = self.backend(supervised=False)
        for name in ("stop", "restart", "log"):
            self.assertEqual(NOT_SUPERVISED.format(name), self.answer_to(backend, "/" + name))


if __name__ == "__main__":
    unittest.main()
