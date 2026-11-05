import unittest

from lazarus_backend.telegram_client import (EMPTY_TEXT, MESSAGE_LIMIT, TelegramClient, TelegramError,
                                             split_text)
from tests.fakes import FakeTelegram


def utf16_len(text):
    return len(text.encode("utf-16-le")) // 2


class SplitTextTest(unittest.TestCase):
    def test_short_text_is_one_part(self):
        self.assertEqual(["hello"], split_text("hello"))

    def test_text_of_exactly_the_limit_is_one_part(self):
        text = "a" * MESSAGE_LIMIT
        self.assertEqual([text], split_text(text))

    def test_long_text_is_split_at_line_breaks(self):
        lines = ["line {:04d} ".format(i) + "x" * 60 for i in range(200)]
        text = "\n".join(lines)
        parts = split_text(text)
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(utf16_len(part), MESSAGE_LIMIT)
            self.assertTrue(part.startswith("line "), part[:20])
        self.assertEqual(lines, "\n".join(parts).split("\n"))

    def test_line_without_breaks_is_split_at_spaces(self):
        words = ["word{}".format(i) for i in range(1500)]
        parts = split_text(" ".join(words))
        self.assertGreater(len(parts), 1)
        self.assertEqual(words, " ".join(parts).split(" "))

    def test_text_without_any_break_is_cut_hard(self):
        parts = split_text("a" * (MESSAGE_LIMIT * 2 + 10))
        self.assertEqual([MESSAGE_LIMIT, MESSAGE_LIMIT, 10], [len(p) for p in parts])

    def test_limit_counts_utf16_units_so_emoji_count_double(self):
        parts = split_text("\U0001F600" * MESSAGE_LIMIT)
        self.assertEqual(2, len(parts))
        for part in parts:
            self.assertEqual(MESSAGE_LIMIT, utf16_len(part))

    def test_empty_text_becomes_a_placeholder(self):
        self.assertEqual([EMPTY_TEXT], split_text(""))
        self.assertEqual([EMPTY_TEXT], split_text("  \n "))


class TelegramClientTest(unittest.TestCase):
    def setUp(self):
        self.telegram = FakeTelegram()
        self.addCleanup(self.telegram.close)
        self.client = TelegramClient("123:token", api_url=self.telegram.url)

    def test_long_message_arrives_as_several_messages_in_order(self):
        text = "\n".join("paragraph {} ".format(i) + "y" * 100 for i in range(100))
        self.client.send_message(42, text)
        sent = self.telegram.bodies("/bot123:token/sendMessage")
        self.assertGreater(len(sent), 1)
        self.assertEqual({42}, {body["chat_id"] for body in sent})
        self.assertEqual(text, "\n".join(body["text"] for body in sent))

    def test_typing_sends_the_typing_chat_action(self):
        self.client.send_typing(42)
        self.assertEqual([{"chat_id": 42, "action": "typing"}], self.telegram.bodies("/sendChatAction"))

    def test_get_updates_passes_offset_and_returns_the_result(self):
        self.telegram.updates.put({"update_id": 7})
        self.assertEqual([{"update_id": 7}], self.client.get_updates(offset=7, timeout=1))
        self.assertEqual(7, self.telegram.bodies("/getUpdates")[0]["offset"])

    def test_get_updates_asks_for_messages_and_button_presses(self):
        self.client.get_updates(timeout=1)
        self.assertEqual(["message", "callback_query"], self.telegram.bodies("/getUpdates")[0]["allowed_updates"])

    def test_buttons_go_under_the_last_part_as_an_inline_keyboard(self):
        text = "\n".join("line {} ".format(i) + "x" * 100 for i in range(60))
        self.client.send_message(42, text, buttons=[[("abc1234 live", "rb:pick:abc")], [("Cancel", "rb:no")]])
        sent = self.telegram.bodies("/sendMessage")
        self.assertGreater(len(sent), 1)
        self.assertEqual([None] * (len(sent) - 1), [body.get("reply_markup") for body in sent[:-1]])
        self.assertEqual({"inline_keyboard": [[{"text": "abc1234 live", "callback_data": "rb:pick:abc"}],
                                              [{"text": "Cancel", "callback_data": "rb:no"}]]},
                         sent[-1]["reply_markup"])

    def test_edit_message_replaces_text_and_buttons(self):
        self.client.edit_message(42, 7, "Roll back?", buttons=[[("Yes", "rb:yes:x"), ("Cancel", "rb:no")]])
        self.client.edit_message(42, 7, "Rolling back…")
        first, second = self.telegram.bodies("/editMessageText")
        self.assertEqual((42, 7, "Roll back?"), (first["chat_id"], first["message_id"], first["text"]))
        self.assertEqual(2, len(first["reply_markup"]["inline_keyboard"][0]))
        self.assertNotIn("reply_markup", second)  # no buttons: Telegram removes them

    def test_answer_callback_acknowledges_the_press(self):
        self.client.answer_callback("cb1")
        self.client.answer_callback("cb2", "Out of date")
        self.assertEqual([{"callback_query_id": "cb1"}, {"callback_query_id": "cb2", "text": "Out of date"}],
                         self.telegram.bodies("/answerCallbackQuery"))

    def test_refusal_raises_telegram_error_with_the_description(self):
        with self.assertRaisesRegex(TelegramError, "message text is empty"):
            self.client.call("sendMessage", chat_id=42, text="")


if __name__ == "__main__":
    unittest.main()
