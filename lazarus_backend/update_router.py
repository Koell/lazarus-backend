"""UpdateRouter: maps one Telegram update to exactly one action. Pure: no I/O, no clock, no logging.

The caller executes the returned action (and logs `Ignore` actions). New kinds of input are added
here as new action types. The chat is text only: every other owner message (voice, audio, sticker,
photo, file, …) is ignored without a reply. Button presses (callback queries) count only from the
owner, in the private chat, and only with callback data the backend's rollback buttons carry.
"""

import re
from dataclasses import dataclass

UNKNOWN_USER = "unknown user"
NOT_TEXT = "not a text message"
UNKNOWN_BUTTON = "unknown button"
UNKNOWN_COMMAND_REPLY = "I don't know {}. /help lists the commands."

# Every command the backend handles itself, in the order /help lists them.
COMMANDS = ("status", "stop", "restart", "log", "mute", "unmute", "rollback", "help", "start")

# The steps of a rollback button: pick a commit from the list, confirm it, or cancel.
ROLLBACK_PICK = "pick"
ROLLBACK_CONFIRM = "yes"
ROLLBACK_CANCEL = "no"

# `/name`, optionally `@botname`, then whitespace or the end. `/etc/hosts` or `/ x` is plain text.
_COMMAND = re.compile(r"/([A-Za-z0-9_]+)(?:@[A-Za-z0-9_]+)?(?=\s|$)")
# Callback data of the rollback buttons (Telegram allows 64 bytes): a full 40-digit sha.
_ROLLBACK_DATA = re.compile(r"rb:(?:(pick|yes):([0-9a-f]{40})|(no))")


def rollback_button_data(step, sha=None):
    """The callback data of a rollback button; `sha` (a full sha) for every step but cancel."""
    if step == ROLLBACK_CANCEL:
        return "rb:" + step
    if step not in (ROLLBACK_PICK, ROLLBACK_CONFIRM) or not isinstance(sha, str) or not re.fullmatch(
            r"[0-9a-f]{40}", sha):
        raise ValueError("a rollback button needs a step and a full sha: {!r} {!r}".format(step, sha))
    return "rb:{}:{}".format(step, sha)


@dataclass(frozen=True)
class Ignore:
    """Drop the update. `user_id` is the sender, if any, so the caller can log it."""
    reason: str
    user_id: object = None


@dataclass(frozen=True)
class Reply:
    """Answer the owner directly from the backend, without involving Lazarus."""
    chat_id: int
    text: str


@dataclass(frozen=True)
class ForwardText:
    """Hand the owner's text to Lazarus. `message_id` becomes the message id lazarus-ai answers to."""
    chat_id: int
    message_id: int
    text: str
    sent_at: int


@dataclass(frozen=True)
class Command:
    """A backend command (`name` from COMMANDS, lower case, without `/` or bot suffix) and its arguments."""
    chat_id: int
    name: str
    args: tuple = ()


@dataclass(frozen=True)
class RollbackButton:
    """The owner pressed a rollback button: `step` (pick, confirm, cancel) and the full `sha` (None to cancel).

    `message_id` is the message carrying the button, `callback_id` what answerCallbackQuery needs.
    The sha is only well-formed: whether it was offered and is on the remote is the caller's check.
    """
    chat_id: int
    message_id: int
    callback_id: str
    step: str
    sha: object = None


def parse_command(text):
    """(name, args) if `text` is a command, else None. The name is lower-cased, a `@botname` suffix dropped."""
    match = _COMMAND.match(text.strip())
    if not match:
        return None
    return match.group(1).lower(), tuple(text.strip()[match.end():].split())


def route(update, owner_id):
    """The action for `update` (a decoded Telegram update dict), given the owner's user id."""
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        return _route_button(callback, owner_id)
    message = update.get("message")
    if not isinstance(message, dict):
        return Ignore("unsupported update type")

    user_id = (message.get("from") or {}).get("id")
    if owner_id is None or user_id != owner_id:
        return Ignore(UNKNOWN_USER, user_id)

    chat = message.get("chat") or {}
    if chat.get("type") != "private":
        return Ignore("not a private chat", user_id)
    chat_id = chat.get("id")

    text = message.get("text")
    command = parse_command(text) if isinstance(text, str) else None
    if command is not None:
        name, args = command
        if name in COMMANDS:
            return Command(chat_id=chat_id, name=name, args=args)
        return Reply(chat_id=chat_id, text=UNKNOWN_COMMAND_REPLY.format("/" + name))
    if isinstance(text, str) and text.strip():
        return ForwardText(chat_id=chat_id, message_id=message.get("message_id"), text=text,
                           sent_at=message.get("date"))
    return Ignore(NOT_TEXT, user_id)


def _route_button(callback, owner_id):
    user_id = (callback.get("from") or {}).get("id")
    if owner_id is None or user_id != owner_id:
        return Ignore(UNKNOWN_USER, user_id)
    message = callback.get("message")
    chat = (message.get("chat") if isinstance(message, dict) else None) or {}
    if chat.get("type") != "private":
        return Ignore("button outside the private chat", user_id)
    data = callback.get("data")
    match = _ROLLBACK_DATA.fullmatch(data) if isinstance(data, str) else None
    if match is None or not isinstance(callback.get("id"), str):
        return Ignore(UNKNOWN_BUTTON, user_id)
    return RollbackButton(chat_id=chat.get("id"), message_id=message.get("message_id"), callback_id=callback["id"],
                          step=match.group(1) or match.group(3), sha=match.group(2))
