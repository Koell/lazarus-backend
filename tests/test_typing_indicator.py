import unittest

from lazarus_backend.typing_indicator import TypingIndicator


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class TypingIndicatorTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.sent = []
        self.typing = TypingIndicator(self.sent.append, clock=self.clock, max_seconds=600)

    def test_typing_is_sent_while_a_message_waits(self):
        self.typing.start(1, chat_id=42)
        self.typing.tick()
        self.typing.tick()
        self.assertEqual([42, 42], self.sent)

    def test_matching_reply_stops_it_also_when_the_id_comes_back_as_a_string(self):
        self.typing.start(1, chat_id=42)
        self.typing.stop("1")
        self.typing.tick()
        self.assertEqual([], self.sent)

    def test_one_typing_per_chat_for_several_waiting_messages(self):
        self.typing.start(1, chat_id=42)
        self.typing.start(2, chat_id=42)
        self.typing.tick()
        self.assertEqual([42], self.sent)
        self.typing.stop(1)
        self.typing.tick()
        self.assertEqual([42, 42], self.sent)

    def test_stops_after_ten_minutes_without_reply(self):
        self.typing.start(1, chat_id=42)
        self.clock.now += 599
        self.typing.tick()
        self.clock.now += 1
        with self.assertLogs("lazarus_backend.typing_indicator", level="WARNING"):
            self.typing.tick()
        self.assertEqual([42], self.sent)
        self.assertEqual([], self.typing.pending())

    def test_failing_send_doesnt_break_the_tick(self):
        def fail(chat_id):
            raise OSError("offline")
        typing = TypingIndicator(fail, clock=self.clock)
        typing.start(1, chat_id=42)
        typing.tick()
        self.assertEqual(["1"], typing.pending())


if __name__ == "__main__":
    unittest.main()
