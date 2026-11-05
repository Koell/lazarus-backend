"""Fake HTTP servers for tests, built on the standard library's http.server (no network needed)."""

import json
import logging
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class RecordingServer:
    """Records every JSON POST as (path, body) and answers with `respond(path, body)` -> (status, body)."""

    def __init__(self, respond=lambda path, body: (200, {"ok": True})):
        self.requests = []
        self._respond = respond
        self._lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8") or "null")
                with server._lock:
                    server.requests.append((self.path, body))
                status, answer = server._respond(self.path, body)
                self._send(status, answer)

            def do_GET(self):
                status, answer = server._respond(self.path, None)
                self._send(status, answer)

            def _send(self, status, answer):
                data = json.dumps(answer).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = "http://127.0.0.1:{}".format(self._server.server_address[1])
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def bodies(self, path_suffix):
        with self._lock:
            return [body for path, body in self.requests if path.endswith(path_suffix)]

    def wait_for(self, path_suffix, count=1, timeout=5):
        """Wait until `count` requests to a path ending in `path_suffix` arrived; return their bodies."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            bodies = self.bodies(path_suffix)
            if len(bodies) >= count:
                return bodies
            time.sleep(0.01)
        raise AssertionError("expected {} request(s) to {}, got {}".format(
            count, path_suffix, self.bodies(path_suffix)))

    def close(self):
        self._server.shutdown()
        self._server.server_close()


class FakeTelegram(RecordingServer):
    """A fake Bot API: getUpdates hands out queued updates once, every other method answers ok."""

    def __init__(self):
        self.updates = queue.Queue()
        super().__init__(self._answer)

    def _answer(self, path, body):
        method = path.rsplit("/", 1)[-1]
        if method == "getUpdates":
            try:
                return 200, {"ok": True, "result": [self.updates.get(timeout=0.2)]}
            except queue.Empty:
                return 200, {"ok": True, "result": []}
        if method == "sendMessage" and not body.get("text", "").strip():
            return 400, {"ok": False, "error_code": 400, "description": "Bad Request: message text is empty"}
        return 200, {"ok": True, "result": True}


def text_update(update_id, user_id, text, message_id=None, chat_type="private"):
    """A Telegram update with a text message from `user_id` in their private chat."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id if message_id is not None else update_id,
            "from": {"id": user_id, "is_bot": False, "first_name": "Someone"},
            "chat": {"id": user_id, "type": chat_type},
            "date": 1700000000,
            "text": text,
        },
    }


def button_update(update_id, user_id, data, message_id=30, callback_id="cb1", chat_type="private"):
    """A Telegram update for `user_id` pressing an inline button with callback `data` under `message_id`."""
    return {
        "update_id": update_id,
        "callback_query": {
            "id": callback_id,
            "from": {"id": user_id, "is_bot": False, "first_name": "Someone"},
            "message": {"message_id": message_id, "chat": {"id": user_id, "type": chat_type}, "date": 1700000000,
                        "text": "Which version should Lazarus run?"},
            "chat_instance": "123",
            "data": data,
        },
    }


# What Telegram sends for some kinds of non-text message, as `media_update` puts them in.
MEDIA = {
    "voice": {"file_id": "v1", "file_unique_id": "uv1", "duration": 3, "mime_type": "audio/ogg", "file_size": 4000},
    "audio": {"file_id": "a1", "file_unique_id": "ua1", "duration": 180, "mime_type": "audio/mpeg",
              "file_size": 3000000},
    "sticker": {"file_id": "s1", "file_unique_id": "us1", "type": "regular", "width": 512, "height": 512},
    "photo": [{"file_id": "p1", "file_unique_id": "up1", "width": 90, "height": 90},
              {"file_id": "p2", "file_unique_id": "up2", "width": 800, "height": 800}],
    "document": {"file_id": "d1", "file_unique_id": "ud1", "file_name": "notes.pdf", "mime_type": "application/pdf"},
    "video": {"file_id": "m1", "file_unique_id": "um1", "duration": 5, "width": 640, "height": 360},
    "video_note": {"file_id": "n1", "file_unique_id": "un1", "duration": 5, "length": 240},
    "location": {"latitude": 48.2, "longitude": 16.37},
    "contact": {"phone_number": "+100", "first_name": "Ann"},
}


def media_update(update_id, user_id, kind, message_id=None, caption=None):
    """A Telegram update with a non-text message (`kind` a key of MEDIA) from `user_id`."""
    update = text_update(update_id, user_id, "", message_id=message_id)
    del update["message"]["text"]
    update["message"][kind] = MEDIA[kind]
    if caption is not None:
        update["message"]["caption"] = caption
    return update


class FakeClock:
    """A clock that moves only when told to; `wall` is the matching wall-clock time."""

    def __init__(self, now=1000.0, wall_offset=1700000000.0):
        self.now = now
        self._wall_offset = wall_offset

    def __call__(self):
        return self.now

    def wall(self):
        return self._wall_offset + self.now

    def advance(self, seconds):
        self.now += seconds


class FakeProcess:
    """A launched Lazarus for the Supervisor: running until `exit(code)` or `stop()`."""

    def __init__(self, pid):
        self.pid = pid
        self.exit_code = None
        self.stopped = False

    def poll(self):
        return self.exit_code

    def exit(self, code):
        self.exit_code = code

    def stop(self):
        self.stopped = True
        if self.exit_code is None:
            self.exit_code = -15
        return self.exit_code


class FakeLauncher:
    """Starts FakeProcesses and records the backend's notes; `fail` makes start() raise OSError."""

    def __init__(self):
        self.processes = []
        self.notes = []
        self.fail = None

    def start(self):
        if self.fail:
            raise OSError(self.fail)
        process = FakeProcess(pid=100 + len(self.processes))
        self.processes.append(process)
        return process

    def note(self, text):
        self.notes.append(text)

    @property
    def running(self):
        """The processes that run right now."""
        return [p for p in self.processes if p.exit_code is None]


def quiet_logs(test):
    """Keep the backend's expected warnings out of the test output for the duration of `test`."""
    logger = logging.getLogger("lazarus_backend")
    handler = logging.NullHandler()
    logger.addHandler(handler)
    test.addCleanup(logger.removeHandler, handler)


class FakeSpeaker:
    """Stands in for the Speaker (no text-to-speech tool runs in tests): records what it says."""

    def __init__(self, ready=True):
        self._ready = ready
        self.said = []

    def ready(self):
        return self._ready

    def speak(self, text):
        if not self._ready:
            return False
        self.said.append(text)
        return True


def local_time(hour, minute, second=0):
    """The time.time() of `hour:minute` local time on a fixed day, for the quiet hours."""
    return time.mktime((2026, 11, 5, hour, minute, second, 0, 0, -1))


NOON = local_time(12, 0)
