"""InboundQueue: the owner's messages wait while Lazarus is down and arrive in order once it's back."""

import os
import shutil
import tempfile
import threading
import time
import unittest

from lazarus_backend.inbound_queue import DELIVERED, QUEUED, QUEUED_NEW_OUTAGE, InboundMessage, InboundQueue
from tests.fakes import quiet_logs


class FakeLazarus:
    """`deliver` for the queue: records messages while `up`, raises while down."""

    def __init__(self):
        self.up = False
        self.received = []
        self.lock = threading.Lock()

    def deliver(self, message):
        if not self.up:
            raise ConnectionRefusedError("lazarus-ai isn't running")
        with self.lock:
            self.received.append(message)

    def texts(self):
        with self.lock:
            return [m.text for m in self.received]


def text(message_id, body):
    return InboundMessage(message_id=message_id, chat_id=1111, text=body, sent_at=1700000000)


def wait_until(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


class InboundQueueTest(unittest.TestCase):
    def setUp(self):
        quiet_logs(self)
        self.lazarus = FakeLazarus()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "data", "inbound.db")

    def queue(self, path=None, ready=lambda: True):
        queue = InboundQueue(self.lazarus.deliver, path=path or self.path, ready=ready)
        self.addCleanup(queue.stop)
        return queue

    def test_delivers_straight_away_while_lazarus_is_up(self):
        self.lazarus.up = True
        queue = self.queue()
        self.assertEqual(DELIVERED, queue.submit(text(1, "hello")))
        self.assertEqual(["hello"], self.lazarus.texts())
        self.assertEqual([], queue.pending())

    def test_only_the_first_message_of_an_outage_starts_it(self):
        queue = self.queue()
        with self.assertLogs("lazarus_backend", level="ERROR"):
            results = [queue.submit(text(i, "m{}".format(i))) for i in (1, 2, 3)]
        self.assertEqual([QUEUED_NEW_OUTAGE, QUEUED, QUEUED], results)

    def test_queued_messages_are_delivered_in_order_and_new_ones_wait_behind_them(self):
        queue = self.queue()
        with self.assertLogs("lazarus_backend", level="ERROR"):
            queue.submit(text(1, "one"))
        queue.submit(text(2, "two"))
        self.lazarus.up = True
        # Lazarus is back, but older messages still wait: the new one queues behind them.
        self.assertEqual(QUEUED, queue.submit(text(3, "three")))
        self.assertEqual([], self.lazarus.texts())
        while queue.deliver_next() is not None:
            pass
        self.assertEqual(["one", "two", "three"], self.lazarus.texts())
        self.assertEqual(DELIVERED, queue.submit(text(4, "four")))

    def test_a_failed_delivery_keeps_the_message_first_and_backs_off(self):
        queue = InboundQueue(self.lazarus.deliver, path=":memory:", initial_backoff=5, max_backoff=60)
        self.addCleanup(queue.stop)
        with self.assertLogs("lazarus_backend", level="ERROR"):
            queue.submit(text(1, "one"))
        # The direct attempt in submit was the first failure (5 s); the retries double from there.
        self.assertEqual([10, 20, 40, 60, 60], [queue.deliver_next() for _ in range(5)])
        self.lazarus.up = True
        self.assertEqual(0, queue.deliver_next())
        self.assertIsNone(queue.deliver_next())

    def test_a_new_outage_after_the_queue_drained_is_announced_again(self):
        queue = self.queue()
        with self.assertLogs("lazarus_backend", level="ERROR"):
            self.assertEqual(QUEUED_NEW_OUTAGE, queue.submit(text(1, "one")))
        self.lazarus.up = True
        queue.deliver_next()
        self.lazarus.up = False
        with self.assertLogs("lazarus_backend", level="ERROR"):
            self.assertEqual(QUEUED_NEW_OUTAGE, queue.submit(text(2, "two")))

    def test_not_ready_queues_without_trying_lazarus(self):
        self.lazarus.up = True
        queue = self.queue(ready=lambda: False)
        self.assertEqual(QUEUED_NEW_OUTAGE, queue.submit(text(1, "one")))
        self.assertEqual([], self.lazarus.texts())

    def test_waiting_messages_survive_a_restart_and_the_outage_isnt_announced_twice(self):
        queue = self.queue()
        with self.assertLogs("lazarus_backend", level="ERROR"):
            queue.submit(text(1, "one"))
        queue.submit(InboundMessage(message_id="2", chat_id=1111, text="two", sent_at=None))
        queue.stop()

        reopened = self.queue()
        self.assertEqual([text(1, "one"), InboundMessage(message_id="2", chat_id=1111, text="two")],
                         reopened.pending())
        self.assertEqual(QUEUED, reopened.submit(text(3, "three")))

    def test_the_worker_delivers_once_lazarus_is_ready_again(self):
        ready = threading.Event()
        queue = self.queue(ready=ready.is_set)
        for i, body in enumerate(("one", "two", "three")):
            queue.submit(text(i, body))
        queue.start()
        time.sleep(0.05)
        self.assertEqual([], self.lazarus.texts())

        self.lazarus.up = True
        ready.set()
        queue.retry_now()  # the Supervisor saw Lazarus come up
        self.assertTrue(wait_until(lambda: len(self.lazarus.texts()) == 3))
        self.assertEqual(["one", "two", "three"], self.lazarus.texts())
        self.assertTrue(wait_until(lambda: queue.pending() == []))


if __name__ == "__main__":
    unittest.main()
