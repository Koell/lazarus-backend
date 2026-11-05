"""Outbox: order, persistence across reopening, retry with backoff, priority stored as given."""

import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error

from lazarus_backend.outbox import MAX_BACKOFF, Outbox, OutboxItem, backoff_delay, make_item
from lazarus_backend.speaker import QuietHours
from tests.fakes import FakeSpeaker, local_time


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class Recipient:
    """A deliver function that fails while `failures` is above zero, then records the items."""

    def __init__(self, failures=0, error=None):
        self.failures = failures
        self.error = error or urllib.error.URLError("network is unreachable")
        self.delivered = []
        self.attempts = 0
        self.arrived = threading.Event()

    def __call__(self, item):
        self.attempts += 1
        if self.failures > 0:
            self.failures -= 1
            raise self.error
        self.delivered.append(item)
        self.arrived.set()

    def texts(self):
        return [item.text for item in self.delivered]


class PermanentError(Exception):
    pass


def drain(outbox):
    """Deliver until the queue is empty or an attempt fails; return the delays the Outbox asked for."""
    delays = []
    while True:
        delay = outbox.deliver_next()
        if delay is None:
            return delays
        delays.append(delay)
        if delay > 0:
            return delays


class OutboxTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = os.path.join(self.dir, "data", "outbox.db")

    def open(self, deliver, **kwargs):
        outbox = Outbox(deliver, path=self.path, clock=self.clock, **kwargs)
        self.addCleanup(outbox.stop)
        return outbox

    def item(self, text, **kwargs):
        self.clock.now += 1
        return make_item(text, clock=self.clock, **kwargs)

    def test_items_are_delivered_in_the_order_they_were_put(self):
        recipient = Recipient()
        outbox = self.open(recipient)
        for text in ("one", "two", "three"):
            outbox.put(self.item(text))
        self.assertEqual([0, 0, 0], drain(outbox))
        self.assertEqual(["one", "two", "three"], recipient.texts())
        self.assertEqual([], outbox.pending())

    def test_items_survive_reopening_with_all_fields(self):
        first = Outbox(Recipient(failures=99), path=self.path, clock=self.clock)
        items = [self.item("hello", in_reply_to="42"),
                 self.item("say this", channel="speech", priority="urgent", in_reply_to=7),
                 self.item("later", priority="low")]
        for item in items:
            first.put(item)
        first.deliver_next()  # fails: nothing is lost
        first.stop()

        recipient = Recipient()
        reopened = self.open(recipient)
        self.assertEqual(items, reopened.pending())
        drain(reopened)
        self.assertEqual(items, recipient.delivered)
        reopened.stop()
        self.assertEqual([], self.open(Recipient()).pending())

    def test_failed_item_is_retried_with_capped_exponential_backoff_and_blocks_the_ones_behind(self):
        recipient = Recipient(failures=8)
        outbox = self.open(recipient)
        outbox.put(self.item("first"))
        outbox.put(self.item("second"))

        delays = [outbox.deliver_next() for _ in range(8)]
        self.assertEqual([5, 10, 20, 40, 80, 160, 300, 300], delays)
        self.assertEqual([], recipient.delivered)
        self.assertEqual(["first", "second"], [item.text for item in outbox.pending()])

        self.assertEqual([0, 0], drain(outbox))
        self.assertEqual(["first", "second"], recipient.texts())

    def test_backoff_starts_over_after_a_success(self):
        recipient = Recipient(failures=2)
        outbox = self.open(recipient)
        outbox.put(self.item("a"))
        self.assertEqual([5, 10, 0], [outbox.deliver_next() for _ in range(3)])
        recipient.failures = 1
        outbox.put(self.item("b"))
        self.assertEqual([5, 0], [outbox.deliver_next() for _ in range(2)])

    def test_retry_now_resets_the_backoff(self):
        outbox = self.open(Recipient(failures=3))
        outbox.put(self.item("a"))
        self.assertEqual([5, 10], [outbox.deliver_next() for _ in range(2)])
        outbox.retry_now()
        self.assertEqual(5, outbox.deliver_next())

    def test_item_refused_for_good_is_dropped_and_the_next_one_goes_out(self):
        recipient = Recipient(failures=1, error=PermanentError("Bad Request"))
        outbox = self.open(recipient, is_permanent=lambda e: isinstance(e, PermanentError))
        outbox.put(self.item("broken"))
        outbox.put(self.item("fine"))
        with self.assertLogs("lazarus_backend.outbox", level="ERROR"):
            self.assertEqual([0, 0], drain(outbox))
        self.assertEqual(["fine"], recipient.texts())

    def test_priority_is_stored_as_given_and_not_enforced(self):
        recipient = Recipient()
        outbox = self.open(recipient)
        for priority in ("low", "normal", "urgent"):
            outbox.put(self.item(priority, priority=priority))
        self.assertEqual(["low", "normal", "urgent"], [item.priority for item in outbox.pending()])
        drain(outbox)
        self.assertEqual(["low", "normal", "urgent"], [item.priority for item in recipient.delivered])

    def test_worker_thread_delivers_in_order_after_failures(self):
        recipient = Recipient(failures=2)
        outbox = self.open(recipient, initial_backoff=0.01, max_backoff=0.02)
        outbox.start()
        for text in ("one", "two", "three"):
            outbox.put(self.item(text))
        deadline = time.monotonic() + 5
        while len(recipient.delivered) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(["one", "two", "three"], recipient.texts())
        self.assertEqual(5, recipient.attempts)

    def test_worker_thread_picks_up_items_left_from_before(self):
        first = Outbox(Recipient(), path=self.path, clock=self.clock)
        first.put(self.item("left over"))
        first.stop()
        recipient = Recipient()
        outbox = self.open(recipient)
        outbox.start()
        self.assertTrue(recipient.arrived.wait(5))
        self.assertEqual(["left over"], recipient.texts())

    def test_in_memory_outbox_needs_no_folder(self):
        outbox = Outbox(Recipient(), clock=self.clock)
        self.addCleanup(outbox.stop)
        outbox.put(self.item("x"))
        self.assertEqual(["x"], [item.text for item in outbox.pending()])


class SpeechTest(unittest.TestCase):
    """Speech items: spoken when allowed, else to Telegram (muted, quiet hours 22:00-07:00, no tool)."""

    def setUp(self):
        self.clock = FakeClock(local_time(12, 0))
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = os.path.join(self.dir, "data", "outbox.db")
        self.telegram = Recipient()
        self.speaker = FakeSpeaker()

    def open(self, speak="default", quiet_hours=("22:00", "07:00")):
        speak = self.speaker.speak if speak == "default" else speak
        outbox = Outbox(self.telegram, path=self.path, clock=self.clock, speak=speak,
                        quiet_hours=QuietHours(*quiet_hours) if quiet_hours else None)
        self.addCleanup(outbox.stop)
        return outbox

    def send(self, outbox, text, channel="speech"):
        outbox.put(make_item(text, channel=channel, clock=self.clock))
        self.assertEqual([0], drain(outbox))

    def test_speech_is_spoken_with_its_text_and_not_written(self):
        outbox = self.open()
        self.send(outbox, "Good morning. It is going to rain today.")
        self.assertEqual(["Good morning. It is going to rain today."], self.speaker.said)
        self.assertEqual([], self.telegram.texts())
        self.assertEqual([], outbox.pending())

    def test_telegram_items_are_never_spoken(self):
        outbox = self.open()
        self.send(outbox, "just text", channel="telegram")
        self.assertEqual([], self.speaker.said)
        self.assertEqual(["just text"], self.telegram.texts())

    def test_speech_goes_to_telegram_while_muted_and_is_spoken_again_after_unmute(self):
        outbox = self.open()
        self.assertFalse(outbox.muted())
        outbox.set_muted(True)
        self.send(outbox, "quiet please")
        self.assertEqual([], self.speaker.said)
        self.assertEqual(["quiet please"], self.telegram.texts())
        outbox.set_muted(False)
        self.send(outbox, "loud again")
        self.assertEqual(["loud again"], self.speaker.said)

    def test_mute_is_kept_across_reopening(self):
        first = Outbox(self.telegram, path=self.path, clock=self.clock, speak=self.speaker.speak)
        first.set_muted(True)
        first.stop()
        outbox = self.open()
        self.assertTrue(outbox.muted())
        self.send(outbox, "still muted")
        self.assertEqual([], self.speaker.said)
        self.assertEqual(["still muted"], self.telegram.texts())
        outbox.set_muted(False)
        outbox.stop()
        self.assertFalse(self.open().muted())

    def test_quiet_hours_boundaries(self):
        # (hour, minute, second, spoken): 22:00 is the first quiet minute, 07:00 the first loud one.
        cases = [(21, 59, 0, True), (21, 59, 59, True), (22, 0, 0, False), (22, 1, 0, False), (23, 0, 0, False),
                 (0, 0, 0, False), (3, 0, 0, False), (6, 59, 0, False), (6, 59, 59, False), (7, 0, 0, True),
                 (7, 1, 0, True), (12, 0, 0, True)]
        outbox = self.open()
        for hour, minute, second, spoken in cases:
            with self.subTest(time="{:02}:{:02}:{:02}".format(hour, minute, second)):
                self.speaker.said.clear()
                self.telegram.delivered.clear()
                self.clock.now = local_time(hour, minute, second)
                self.assertEqual(not spoken, outbox.in_quiet_hours())
                self.send(outbox, "it is {}:{}".format(hour, minute))
                self.assertEqual(1 if spoken else 0, len(self.speaker.said))
                self.assertEqual(0 if spoken else 1, len(self.telegram.delivered))

    def test_quiet_hours_are_decided_at_delivery_not_when_the_item_was_put(self):
        outbox = self.open()
        self.clock.now = local_time(21, 59)
        outbox.put(make_item("queued before 22:00", channel="speech", clock=self.clock))
        self.clock.now = local_time(22, 0)
        drain(outbox)
        self.assertEqual(["queued before 22:00"], self.telegram.texts())

    def test_quiet_hours_within_one_day_and_none(self):
        outbox = self.open(quiet_hours=("13:00", "15:00"))
        for hour, quiet in ((12, False), (13, True), (14, True), (15, False), (23, False)):
            self.clock.now = local_time(hour, 0)
            self.assertEqual(quiet, outbox.in_quiet_hours(), hour)
        outbox = Outbox(self.telegram, clock=self.clock, speak=self.speaker.speak)
        self.addCleanup(outbox.stop)
        self.clock.now = local_time(23, 0)
        self.assertFalse(outbox.in_quiet_hours())
        self.assertTrue(outbox.may_speak())

    def test_without_a_speaker_or_a_tool_speech_is_written(self):
        outbox = self.open(speak=None)
        self.send(outbox, "no speaker")
        self.assertFalse(outbox.may_speak())
        self.speaker = FakeSpeaker(ready=False)
        outbox = self.open()
        self.send(outbox, "no tool")
        self.assertEqual(["no speaker", "no tool"], self.telegram.texts())

    def test_a_speaker_that_raises_falls_back_to_telegram(self):
        def broken(text):
            raise RuntimeError("audio device busy")
        outbox = self.open(speak=broken)
        with self.assertLogs("lazarus_backend.outbox", level="ERROR"):
            self.send(outbox, "said anyway")
        self.assertEqual(["said anyway"], self.telegram.texts())

    def test_written_speech_is_retried_like_any_item_while_telegram_is_down(self):
        self.telegram.failures = 1
        outbox = self.open()
        outbox.set_muted(True)
        outbox.put(make_item("later", channel="speech", clock=self.clock))
        self.assertEqual(1, len(drain(outbox)))
        self.assertEqual(["later"], [item.text for item in outbox.pending()])
        drain(outbox)
        self.assertEqual(["later"], self.telegram.texts())


class MakeItemTest(unittest.TestCase):
    def test_defaults_and_creation_time(self):
        self.assertEqual(OutboxItem("hi", "telegram", "normal", None, 12.5), make_item("hi", clock=lambda: 12.5))

    def test_rejects_invalid_fields(self):
        for kwargs in ({"text": ""}, {"text": "  "}, {"text": None}, {"text": "x", "channel": "pigeon"},
                       {"text": "x", "priority": "max"}, {"text": "x", "in_reply_to": [1]},
                       {"text": "x", "in_reply_to": True}):
            with self.assertRaises(ValueError, msg=kwargs):
                make_item(**kwargs)

    def test_backoff_delay_doubles_up_to_the_cap(self):
        self.assertEqual([5, 10, 20, 40], [backoff_delay(n) for n in range(1, 5)])
        self.assertEqual(MAX_BACKOFF, backoff_delay(1000))


if __name__ == "__main__":
    unittest.main()
