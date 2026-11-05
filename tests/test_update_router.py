import unittest

from lazarus_backend.update_router import (COMMANDS, NOT_TEXT, ROLLBACK_CANCEL, ROLLBACK_CONFIRM, ROLLBACK_PICK,
                                           UNKNOWN_BUTTON, UNKNOWN_COMMAND_REPLY, UNKNOWN_USER, Command,
                                           ForwardText, Ignore, Reply, RollbackButton, rollback_button_data, route)
from tests.fakes import MEDIA, button_update, media_update, text_update

OWNER = 1111
STRANGER = 2222


class OwnerTextTest(unittest.TestCase):
    def test_owner_text_is_forwarded_with_its_id_and_time(self):
        action = route(text_update(10, OWNER, "Hello Lazarus", message_id=77), OWNER)
        self.assertEqual(ForwardText(chat_id=OWNER, message_id=77, text="Hello Lazarus", sent_at=1700000000),
                         action)

    def test_text_is_forwarded_unchanged(self):
        text = "  line one\nline two  "
        self.assertEqual(text, route(text_update(1, OWNER, text), OWNER).text)

    def test_text_with_a_slash_that_isnt_a_command_is_forwarded(self):
        for text in ("/etc/hosts is broken", "/ hello", "and/or", "1/2 cup sugar", "/"):
            with self.subTest(text=text):
                self.assertEqual(text, route(text_update(1, OWNER, text), OWNER).text)

    def test_blank_text_is_ignored(self):
        self.assertEqual(Ignore(NOT_TEXT, OWNER), route(text_update(1, OWNER, "   "), OWNER))


class OwnerNonTextTest(unittest.TestCase):
    """The chat is text only: everything else from the owner is ignored, with no reply and no forward."""

    def test_every_non_text_message_is_ignored(self):
        for kind in MEDIA:
            with self.subTest(kind=kind):
                self.assertEqual(Ignore(NOT_TEXT, OWNER), route(media_update(1, OWNER, kind), OWNER))

    def test_media_with_a_caption_is_ignored_too(self):
        update = media_update(1, OWNER, "photo", caption="look at this")
        self.assertEqual(Ignore(NOT_TEXT, OWNER), route(update, OWNER))

    def test_message_with_nothing_known_in_it_is_ignored(self):
        update = text_update(1, OWNER, "x")
        del update["message"]["text"]
        self.assertEqual(Ignore(NOT_TEXT, OWNER), route(update, OWNER))

    def test_non_text_from_a_stranger_is_ignored_as_unknown_user(self):
        update = media_update(1, STRANGER, "voice")
        self.assertEqual(Ignore(UNKNOWN_USER, STRANGER), route(update, OWNER))


class CommandTest(unittest.TestCase):
    def test_every_command_is_recognised(self):
        for name in ("help", "start", "status", "stop", "restart", "log", "mute", "unmute", "rollback"):
            with self.subTest(name=name):
                self.assertEqual(Command(chat_id=OWNER, name=name), route(text_update(1, OWNER, "/" + name), OWNER))

    def test_commands_list_matches_the_tested_names(self):
        self.assertEqual({"help", "start", "status", "stop", "restart", "log", "mute", "unmute", "rollback"},
                         set(COMMANDS))

    def test_bot_name_suffix_works_like_the_plain_command(self):
        for name in COMMANDS:
            with self.subTest(name=name):
                self.assertEqual(route(text_update(1, OWNER, "/" + name), OWNER),
                                 route(text_update(1, OWNER, "/{}@lazarus_bot".format(name)), OWNER))

    def test_arguments_are_split_on_whitespace(self):
        self.assertEqual(Command(chat_id=OWNER, name="rollback", args=("abc123", "memory")),
                         route(text_update(1, OWNER, "/rollback  abc123\nmemory "), OWNER))
        self.assertEqual(("abc123",), route(text_update(1, OWNER, "/rollback@lazarus_bot abc123"), OWNER).args)

    def test_command_name_is_case_insensitive_and_surrounding_space_ignored(self):
        self.assertEqual(Command(chat_id=OWNER, name="status"), route(text_update(1, OWNER, "  /Status "), OWNER))

    def test_unknown_command_gets_a_reply_pointing_to_help_and_isnt_forwarded(self):
        for text in ("/foo", "/foo@lazarus_bot", "/statuses now"):
            with self.subTest(text=text):
                action = route(text_update(1, OWNER, text), OWNER)
                self.assertIsInstance(action, Reply)
                self.assertIn("/help", action.text)
        self.assertEqual(Reply(chat_id=OWNER, text=UNKNOWN_COMMAND_REPLY.format("/foo")),
                         route(text_update(1, OWNER, "/foo bar"), OWNER))

    def test_command_from_a_stranger_is_ignored(self):
        self.assertEqual(Ignore(UNKNOWN_USER, STRANGER), route(text_update(1, STRANGER, "/status"), OWNER))

    def test_command_in_a_group_chat_is_ignored(self):
        self.assertIsInstance(route(text_update(1, OWNER, "/status", chat_type="group"), OWNER), Ignore)


SHA = "0123456789abcdef0123456789abcdef01234567"


class RollbackButtonTest(unittest.TestCase):
    def test_owner_presses_are_rollback_button_actions(self):
        for step, sha in ((ROLLBACK_PICK, SHA), (ROLLBACK_CONFIRM, SHA), (ROLLBACK_CANCEL, None)):
            with self.subTest(step=step):
                update = button_update(3, OWNER, rollback_button_data(step, sha), message_id=40, callback_id="cb9")
                self.assertEqual(RollbackButton(chat_id=OWNER, message_id=40, callback_id="cb9", step=step, sha=sha),
                                 route(update, OWNER))

    def test_button_data_fits_telegrams_64_bytes(self):
        for step in (ROLLBACK_PICK, ROLLBACK_CONFIRM):
            self.assertLessEqual(len(rollback_button_data(step, SHA).encode("utf-8")), 64)
        with self.assertRaises(ValueError):
            rollback_button_data(ROLLBACK_PICK, "abc1234")

    def test_a_strangers_press_is_ignored_as_unknown_user(self):
        update = button_update(3, STRANGER, rollback_button_data(ROLLBACK_CONFIRM, SHA))
        self.assertEqual(Ignore(UNKNOWN_USER, STRANGER), route(update, OWNER))
        self.assertEqual(Ignore(UNKNOWN_USER, OWNER), route(button_update(3, OWNER, "rb:no"), None))

    def test_malformed_or_foreign_data_is_ignored(self):
        for data in ("rb:pick:abc1234", "rb:yes:" + SHA.upper(), "rb:yes:" + SHA + "0", "rb:drop:" + SHA,
                     "rb:no:" + SHA, "rb:pick:" + SHA + "\n", "something else", "", None, 42):
            with self.subTest(data=data):
                self.assertEqual(Ignore(UNKNOWN_BUTTON, OWNER), route(button_update(3, OWNER, data), OWNER))

    def test_press_outside_the_private_chat_or_without_its_message_is_ignored(self):
        group = button_update(3, OWNER, "rb:no", chat_type="group")
        self.assertIsInstance(route(group, OWNER), Ignore)
        orphan = button_update(3, OWNER, "rb:no")
        del orphan["callback_query"]["message"]
        self.assertIsInstance(route(orphan, OWNER), Ignore)


class UnknownUserTest(unittest.TestCase):
    def test_stranger_is_ignored_with_their_user_id(self):
        self.assertEqual(Ignore(UNKNOWN_USER, STRANGER), route(text_update(1, STRANGER, "hi"), OWNER))

    def test_everyone_is_ignored_while_no_owner_is_configured(self):
        self.assertEqual(Ignore(UNKNOWN_USER, OWNER), route(text_update(1, OWNER, "hi"), None))

    def test_message_without_sender_is_ignored(self):
        update = text_update(1, OWNER, "hi")
        del update["message"]["from"]
        self.assertEqual(Ignore(UNKNOWN_USER, None), route(update, OWNER))

    def test_owner_id_must_match_exactly(self):
        self.assertIsInstance(route(text_update(1, OWNER, "hi"), str(OWNER)), Ignore)


class OtherUpdatesTest(unittest.TestCase):
    def test_owner_in_a_group_chat_is_ignored(self):
        action = route(text_update(1, OWNER, "hi", chat_type="group"), OWNER)
        self.assertIsInstance(action, Ignore)
        self.assertNotEqual(UNKNOWN_USER, action.reason)

    def test_update_without_a_message_is_ignored(self):
        edited = {"update_id": 5, "edited_message": text_update(5, OWNER, "hi")["message"]}
        self.assertIsInstance(route(edited, OWNER), Ignore)

    def test_route_returns_one_action_and_doesnt_change_the_update(self):
        update = text_update(1, OWNER, "hi")
        snapshot = repr(update)
        route(update, OWNER)
        self.assertEqual(snapshot, repr(update))


if __name__ == "__main__":
    unittest.main()
