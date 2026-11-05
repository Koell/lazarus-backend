# lazarus-backend

The bottle around Lazarus. It runs on the phone, outside Lazarus's reach (root-owned, read-only for the phone user), and holds all secrets.

- **Telegram gateway:** it's the only process that talks to Telegram. It handles control commands itself and forwards everything else to lazarus-ai.
- **Outgoing:** lazarus-ai hands messages to it on `127.0.0.1`. It enforces the message budget (urgent / normal max 5 per day / low → evening digest) and queues while offline.
- **Watchdog:** it starts lazarus-ai, restarts it after crashes, and rolls back to the last stable `live` commit after 3 crashes in 10 min.
- **Repo:** nightly commit and push of lazarus-ai to the protected `live` branch. Lazarus itself never sees a token.

Commands: `/rollback [sha] [memory]`, `/restart`, `/stop`, `/log`, `/status`, `/mute`, `/unmute`.

## Current state

`ai_bot.py` and `start-ai.sh` are the pre-design prototype: a single Telegram bot that also calls llama-server and whisper.cpp itself. Milestone 1 splits it. The Telegram part stays here, and the LLM and whisper calls move to lazarus-ai.
