"""LocalAPI: the backend's HTTP interface for lazarus-ai on 127.0.0.1, plus the client for lazarus-ai's Inbox.

Backend endpoints (JSON in and out):
  POST /send    {"text", "channel"?, "priority"?, "in_reply_to"?}  -> 202 {"ok": true}
  GET  /health                                                    -> 200 {"ok": true, ...}
lazarus-ai's Inbox, called by LazarusClient:
  POST /message {"id", "text", "sent_at"}                         -> 202, answered later via /send
  GET  /health

No authentication in M1: the phone is single-user and the server binds to localhost only.
"""

import json
import logging
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .outbox import make_item

MAX_BODY = 1024 * 1024

log = logging.getLogger(__name__)


class LocalAPI:
    """Serves `send` and `health`. `on_send(item)` receives each validated OutboxItem;
    `health()` returns a dict merged into the health answer."""

    def __init__(self, host, port, on_send, health=lambda: {}):
        handler = type("LocalAPIHandler", (_Handler,), {"api": self})
        self.on_send = on_send
        self.health = health
        self._server = ThreadingHTTPServer((host, port), handler)
        self._server.daemon_threads = True
        self._thread = None

    @property
    def address(self):
        return self._server.server_address[:2]

    def start(self):
        self._thread = threading.Thread(target=self._server.serve_forever, name="local-api", daemon=True)
        self._thread.start()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


class _Handler(BaseHTTPRequestHandler):
    api = None  # set per server by LocalAPI

    def do_GET(self):
        if self.path == "/health":
            answer = {"ok": True}
            answer.update(self.api.health())
            self._reply(200, answer)
        else:
            self._reply(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path != "/send":
            self._reply(404, {"ok": False, "error": "not found"})
            return
        try:
            body = read_json(self)
            item = make_item(body.get("text"), channel=body.get("channel", "telegram"),
                             priority=body.get("priority", "normal"), in_reply_to=body.get("in_reply_to"))
        except ValueError as e:
            self._reply(400, {"ok": False, "error": str(e)})
            return
        self.api.on_send(item)
        self._reply(202, {"ok": True})

    def _reply(self, status, body):
        write_json(self, status, body)

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)


def read_json(handler):
    """The request body of `handler` as a JSON object; ValueError if it isn't one."""
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0 or length > MAX_BODY:
        raise ValueError("expected a JSON body of at most {} bytes".format(MAX_BODY))
    body = json.loads(handler.rfile.read(length).decode("utf-8"))
    if not isinstance(body, dict):
        raise ValueError("expected a JSON object")
    return body


def write_json(handler, status, body):
    data = json.dumps(body).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class LazarusUnavailable(Exception):
    """lazarus-ai's Inbox didn't accept the request (not running, or it answered with an error)."""


class LazarusClient:
    """Talks to lazarus-ai's Inbox. `urlopen` is injectable for tests."""

    def __init__(self, base_url, urlopen=urllib.request.urlopen, timeout=10):
        self._base = base_url.rstrip("/")
        self._urlopen = urlopen
        self._timeout = timeout

    def post_message(self, message_id, text, sent_at):
        """Hand one owner message to Lazarus. Returns once it's accepted; the answer comes via /send."""
        self._request("/message", {"id": str(message_id), "text": text, "sent_at": sent_at})

    def health(self):
        """True if lazarus-ai answers its health check."""
        try:
            self._request("/health")
            return True
        except LazarusUnavailable:
            return False

    def _request(self, path, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(self._base + path, data=data,
                                         headers={"Content-Type": "application/json"})
        try:
            with self._urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            if isinstance(e, urllib.error.HTTPError):
                e.close()
            raise LazarusUnavailable("{} {}: {}".format(self._base, path, e))
