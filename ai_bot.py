#!/usr/bin/env python3
"""Telegram bot that answers with the phone's local LLM (llama-server) and
transcribes voice messages with whisper.cpp. Standard library only.

Config (environment variables, all optional except the token file):
  BOT_TOKEN_FILE   file holding the bot token          (~/ai/telegram.token)
  BOT_ALLOWED_IDS  comma-separated Telegram user ids    (empty = nobody; ids of rejected users are logged)
  LLM_URL          llama-server base URL               (http://127.0.0.1:8080)
  WHISPER_BIN      whisper-cli path                    (~/ai/whisper.cpp/build/bin/whisper-cli)
  WHISPER_MODEL    whisper model path                  (~/ai/whisper.cpp/models/ggml-base.bin)
  BOT_HISTORY      messages of history kept per chat    (6)
  BOT_MAX_TOKENS   max tokens per answer                (400)
"""

import json
import logging
import os
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

HOME = os.path.expanduser("~")
TOKEN_FILE = os.environ.get("BOT_TOKEN_FILE", f"{HOME}/ai/telegram.token")
ALLOWED_IDS = {int(x) for x in os.environ.get("BOT_ALLOWED_IDS", "").split(",") if x.strip()}
LLM_URL = os.environ.get("LLM_URL", "http://127.0.0.1:8080")
WHISPER_BIN = os.environ.get("WHISPER_BIN", f"{HOME}/ai/whisper.cpp/build/bin/whisper-cli")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", f"{HOME}/ai/whisper.cpp/models/ggml-base.bin")
HISTORY = int(os.environ.get("BOT_HISTORY", "6"))
MAX_TOKENS = int(os.environ.get("BOT_MAX_TOKENS", "400"))

SYSTEM_PROMPT = (
    "You are a helpful assistant running offline on a small phone. "
    "Answer briefly and in the language the user writes in."
)
TELEGRAM_LIMIT = 4096
STARTED = time.time()

log = logging.getLogger("ai_bot")


class Telegram:
    def __init__(self, token):
        self._api = f"https://api.telegram.org/bot{token}/"
        self._files = f"https://api.telegram.org/file/bot{token}/"

    def call(self, method, http_timeout=60, **params):
        req = urllib.request.Request(
            self._api + method,
            data=json.dumps(params).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=http_timeout) as resp:
            body = json.load(resp)
        if not body.get("ok"):
            raise RuntimeError(f"{method} failed: {body.get('description')}")
        return body["result"]

    def download(self, file_id, dest):
        path = self.call("getFile", file_id=file_id)["file_path"]
        with urllib.request.urlopen(self._files + path, timeout=60) as resp, open(dest, "wb") as out:
            out.write(resp.read())

    def send(self, chat_id, text):
        for part in split_message(text or "(empty answer)"):
            self.call("sendMessage", chat_id=chat_id, text=part)


def split_message(text, limit=TELEGRAM_LIMIT):
    """Split text into Telegram-sized parts, preferring line breaks."""
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    parts.append(text)
    return parts


def ask_llm(messages):
    req = urllib.request.Request(
        LLM_URL + "/v1/chat/completions",
        data=json.dumps({"messages": messages, "max_tokens": MAX_TOKENS, "temperature": 0.7}).encode(),
        headers={"Content-Type": "application/json"},
    )
    # A 1.5B model on the N100 does ~4-8 tok/s, so a long answer takes minutes.
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.load(resp)["choices"][0]["message"]["content"].strip()


def transcribe(ogg_path):
    wav_path = ogg_path + ".wav"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-i", ogg_path, "-ar", "16000", "-ac", "1", wav_path],
        check=True,
    )
    try:
        result = subprocess.run(
            [WHISPER_BIN, "-m", WHISPER_MODEL, "-f", wav_path, "-l", "auto", "-t", "4", "-nt", "-np"],
            check=True, capture_output=True, text=True, timeout=300,
        )
    finally:
        os.remove(wav_path)
    return " ".join(result.stdout.split())


def read_first(path, default="?"):
    try:
        with open(path) as f:
            return f.read().split("\n")[0].strip()
    except OSError:
        return default


def status_text():
    meminfo = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                key, value = line.split(":", 1)
                meminfo[key] = int(value.split()[0]) // 1024
    except OSError:
        pass
    temp = read_first("/sys/class/thermal/thermal_zone0/temp")
    temp = f"{int(temp) / 1000:.1f} °C" if temp.isdigit() else "?"
    try:
        with urllib.request.urlopen(LLM_URL + "/health", timeout=5) as resp:
            llm = "up" if resp.status == 200 else f"HTTP {resp.status}"
    except (urllib.error.URLError, OSError):
        llm = "down"
    return (
        f"uptime: {int(time.time() - STARTED) // 60} min\n"
        f"RAM free: {meminfo.get('MemAvailable', '?')} / {meminfo.get('MemTotal', '?')} MB\n"
        f"temp: {temp}\n"
        f"battery: {read_first('/sys/class/power_supply/battery/capacity')} %\n"
        f"llama-server: {llm}"
    )


class KeepTyping:
    """Shows "typing…" in the chat until the block ends (Telegram clears it after ~5 s)."""

    def __init__(self, tg, chat_id):
        self._tg, self._chat_id = tg, chat_id
        self._stop = threading.Event()

    def __enter__(self):
        threading.Thread(target=self._run, daemon=True).start()

    def __exit__(self, *exc):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                self._tg.call("sendChatAction", http_timeout=10, chat_id=self._chat_id, action="typing")
            except Exception:
                pass
            self._stop.wait(4)


class Bot:
    def __init__(self, tg):
        self.tg = tg
        self.history = {}

    def handle(self, message):
        chat_id = message["chat"]["id"]
        user_id = message.get("from", {}).get("id")
        if user_id not in ALLOWED_IDS:
            log.warning("ignored message from user id %s (add it to BOT_ALLOWED_IDS)", user_id)
            return

        text = message.get("text", "")
        if text.startswith("/"):
            self.command(chat_id, text.split()[0].split("@")[0])
            return

        if "voice" in message or "audio" in message:
            media = message.get("voice") or message.get("audio")
            with KeepTyping(self.tg, chat_id), tempfile.TemporaryDirectory() as tmp:
                ogg = os.path.join(tmp, "in.ogg")
                self.tg.download(media["file_id"], ogg)
                text = transcribe(ogg)
            if not text:
                self.tg.send(chat_id, "Couldn't understand the voice message.")
                return
            self.tg.send(chat_id, f"🎤 {text}")

        if not text:
            self.tg.send(chat_id, "Send text or a voice message.")
            return

        turns = self.history.setdefault(chat_id, [])
        turns.append({"role": "user", "content": text})
        del turns[:-HISTORY]
        started = time.time()
        try:
            with KeepTyping(self.tg, chat_id):
                answer = ask_llm([{"role": "system", "content": SYSTEM_PROMPT}] + turns)
        except (urllib.error.URLError, OSError) as e:
            turns.pop()
            log.error("llama-server request failed: %s", e)
            self.tg.send(chat_id, "The AI isn't reachable (is llama-server running?). Try /status.")
            return
        turns.append({"role": "assistant", "content": answer})
        del turns[:-HISTORY]
        log.info("answered chat %s in %.1f s", chat_id, time.time() - started)
        self.tg.send(chat_id, answer)

    def command(self, chat_id, cmd):
        if cmd == "/reset":
            self.history.pop(chat_id, None)
            self.tg.send(chat_id, "Conversation cleared.")
        elif cmd == "/status":
            self.tg.send(chat_id, status_text())
        else:
            self.tg.send(chat_id, "Offline AI on the N100.\nText or voice → answer.\n/reset  forget the conversation\n/status  phone + AI health")

    def run(self):
        offset = None
        backoff = 5
        log.info("bot started, allowed ids: %s", sorted(ALLOWED_IDS) or "none")
        while True:
            try:
                updates = self.tg.call("getUpdates", http_timeout=70, offset=offset, timeout=50,
                                       allowed_updates=["message"])
                backoff = 5
            except (urllib.error.URLError, OSError, RuntimeError) as e:
                # Phone offline or Telegram unreachable: Telegram keeps the messages for 24 h.
                log.info("no connection (%s), retrying in %d s", e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                if "message" in update:
                    try:
                        self.handle(update["message"])
                    except Exception:
                        log.exception("failed to handle update %s", update["update_id"])


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with open(TOKEN_FILE) as f:
        token = f.read().strip()
    Bot(Telegram(token)).run()


if __name__ == "__main__":
    main()
