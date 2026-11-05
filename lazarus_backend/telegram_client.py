"""TelegramClient: a thin wrapper around the Telegram Bot API (messages, typing, inline buttons and their
callback answers). No logic beyond splitting long texts."""

import http.client
import json
import logging
import socket
import time
import urllib.error
import urllib.request

# Telegram's limit for one message, counted in UTF-16 code units.
MESSAGE_LIMIT = 4096
EMPTY_TEXT = "(empty answer)"
# A call slower than this is logged, so a slow network or a stuck connection shows up in the log.
SLOW_CALL_SECONDS = 3.0
# Each of api.telegram.org's addresses (IPv6 and IPv4) gets this long to accept a connection before the
# next one is tried. Without it a dead address costs the OS's own connect timeout (21 s on Windows).
CONNECT_SECONDS = 5.0
# A long poll may take this much longer than its own `timeout` before it counts as stalled.
POLL_SLACK_SECONDS = 5

log = logging.getLogger(__name__)


def connect_quickly(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None,
                    connect_seconds=CONNECT_SECONDS, getaddrinfo=socket.getaddrinfo):
    """Like socket.create_connection, but IPv4 addresses first, and each address gets at most
    `connect_seconds` to connect.

    IPv4 first because IPv6 to api.telegram.org was seen timing out now and then while IPv4 answered;
    IPv6 is still tried if IPv4 fails. The returned socket then uses `timeout` for reading, as
    http.client expects.
    """
    host, port = address
    read_timeout = timeout if isinstance(timeout, (int, float)) else socket.getdefaulttimeout()
    error = None
    addresses = sorted(getaddrinfo(host, port, 0, socket.SOCK_STREAM), key=lambda info: info[0] != socket.AF_INET)
    for family, kind, proto, _, sockaddr in addresses:
        sock = socket.socket(family, kind, proto)
        try:
            sock.settimeout(connect_seconds if read_timeout is None else min(connect_seconds, read_timeout))
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            sock.settimeout(read_timeout)
            return sock
        except OSError as e:
            error = e
            sock.close()
            log.info("couldn't connect to %s at %s (%s), trying the next address", host, sockaddr[0], e)
    raise error if error is not None else OSError("no address for {}".format(host))


class _HTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = connect_quickly


class _HTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = connect_quickly


class _HTTPHandler(urllib.request.HTTPHandler):
    def do_open(self, http_class, request, **kwargs):
        return super().do_open(_HTTPConnection, request, **kwargs)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def do_open(self, http_class, request, **kwargs):
        return super().do_open(_HTTPSConnection, request, **kwargs)


# urlopen for the Bot API: every connection uses connect_quickly.
_opener = urllib.request.build_opener(_HTTPHandler, _HTTPSHandler)
quick_urlopen = _opener.open


def _log_if_slow(method, params, seconds):
    # getUpdates is a long poll: it's meant to wait up to its `timeout` for a new update.
    expected = params.get("timeout", 0) if method == "getUpdates" else 0
    if seconds > expected + SLOW_CALL_SECONDS:
        log.warning("slow Telegram call: %s took %.1f s", method, seconds)


class TelegramError(Exception):
    """Telegram answered, but refused the request (ok: false or an HTTP error status).

    `code` is the HTTP status, or None if Telegram answered 200 with ok: false.
    """

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code

    @property
    def permanent(self):
        """True if sending the same request again can't succeed: a bad request (400) or a bot the
        chat blocked (403). Rate limits (429), server errors and a bad token (401, fixed by a
        restart) are worth retrying."""
        return self.code in (400, 403)


def split_text(text, limit=MESSAGE_LIMIT):
    """Split `text` into parts Telegram accepts, at line breaks where possible, else at spaces.

    Parts that would be blank are dropped (Telegram rejects them); an empty text becomes one
    placeholder part.
    """
    parts = []
    while text:
        end = _fitting_prefix(text, limit)
        if end >= len(text):
            parts.append(text)
            break
        cut = text.rfind("\n", 0, end + 1)
        if cut <= 0:
            cut = text.rfind(" ", 0, end + 1)
        if cut <= 0:
            parts.append(text[:end])
            text = text[end:]
        else:
            parts.append(text[:cut])
            text = text[cut + 1:]
    parts = [p for p in parts if p.strip()]
    return parts or [EMPTY_TEXT]


def _fitting_prefix(text, limit):
    """Number of characters at the start of `text` that fit into `limit` UTF-16 code units."""
    units = 0
    for i, ch in enumerate(text):
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > limit:
            return i
    return len(text)


class TelegramClient:
    """Calls the Bot API. `urlopen` is injectable for tests; `api_url` can point to a fake server."""

    def __init__(self, token, api_url="https://api.telegram.org", urlopen=quick_urlopen):
        self._base = "{}/bot{}/".format(api_url.rstrip("/"), token)
        self._urlopen = urlopen

    def call(self, method, http_timeout=30, **params):
        request = urllib.request.Request(
            self._base + method,
            data=json.dumps(params).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        started = time.monotonic()
        try:
            with self._urlopen(request, timeout=http_timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # Telegram reports refusals (bad request, unauthorized, flood control) as HTTP errors
            # with a JSON body; turn them into TelegramError with its description.
            try:
                description = json.loads(e.read().decode("utf-8")).get("description")
            except (OSError, ValueError, AttributeError):
                description = None
            finally:
                e.close()
            raise TelegramError("{} failed: HTTP {} {}".format(method, e.code, description or e.reason),
                                code=e.code)
        finally:
            _log_if_slow(method, params, time.monotonic() - started)
        if not body.get("ok"):
            raise TelegramError("{} failed: {}".format(method, body.get("description")))
        return body["result"]

    def get_updates(self, offset=None, timeout=50, allowed_updates=("message", "callback_query")):
        """Long-poll for new updates (messages and button presses). Raises URLError/OSError while offline."""
        params = {"timeout": timeout, "allowed_updates": list(allowed_updates)}
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", http_timeout=timeout + POLL_SLACK_SECONDS, **params)

    def send_message(self, chat_id, text, buttons=None):
        """Send `text`, split into several messages if it's longer than Telegram allows.

        `buttons`: rows of (label, callback data), shown as inline buttons under the last part.
        """
        parts = split_text(text)
        for index, part in enumerate(parts):
            params = {"chat_id": chat_id, "text": part}
            if buttons and index == len(parts) - 1:
                params["reply_markup"] = inline_keyboard(buttons)
            self.call("sendMessage", **params)

    def edit_message(self, chat_id, message_id, text, buttons=None):
        """Replace a sent message's text (cut to one message) and its buttons; no `buttons` removes them."""
        params = {"chat_id": chat_id, "message_id": message_id, "text": split_text(text)[0]}
        if buttons:
            params["reply_markup"] = inline_keyboard(buttons)
        self.call("editMessageText", **params)

    def answer_callback(self, callback_id, text=None):
        """Acknowledge a button press (ends the button's spinner); `text` shows as a short notice."""
        params = {"callback_query_id": callback_id}
        if text:
            params["text"] = text
        self.call("answerCallbackQuery", http_timeout=10, **params)

    def send_typing(self, chat_id):
        """Show "typing…" in the chat; Telegram clears it after about 5 s or with the next message."""
        self.call("sendChatAction", http_timeout=10, chat_id=chat_id, action="typing")


def inline_keyboard(rows):
    """Telegram's reply_markup for rows of (label, callback data) buttons."""
    return {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row] for row in rows]}
