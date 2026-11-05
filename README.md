# lazarus-backend

The bottle around Lazarus. It runs on the phone, outside Lazarus's reach (root-owned, read-only for the phone user), and holds all secrets.

- **Telegram gateway:** it's the only process that talks to Telegram. It handles control commands itself and forwards everything else to lazarus-ai.
- **Outgoing:** lazarus-ai hands messages to it on `127.0.0.1`. It enforces the message budget (urgent / normal max 5 per day / low → evening digest) and queues while offline.
- **Watchdog:** it starts lazarus-ai, restarts it after crashes, and rolls back to the last stable `live` commit after 3 crashes in 10 min.
- **Repo:** nightly commit and push of lazarus-ai to the protected `live` branch. Lazarus itself never sees a token.

Commands: `/rollback [sha] [memory]`, `/restart`, `/stop`, `/log`, `/status`, `/mute`, `/unmute`, `/help` (also `/start`). A bot-name suffix (`/status@yourbot`) works too; an unknown command is answered with a pointer to `/help` and never reaches Lazarus.

## Code

`lazarus_backend/` (Python 3.8+, standard library only):

- `telegram_client.py` — TelegramClient: long polling (messages and button presses), sending (split at Telegram's 4096-character limit, at line breaks where possible), inline buttons, editing a message, answering a button press, the typing indicator. Telegram's IPv4 addresses are tried before its IPv6 ones, each gets 5 s to accept a connection before the next is tried, a long poll waits 25 s (`poll_timeout`) and counts as stalled 5 s later, and calls slower than expected are logged as "slow Telegram call".
- `update_router.py` — UpdateRouter: pure, maps one Telegram update to one action (ignore, reply, backend command, rollback button press, forward text to Lazarus). Button presses count only from the owner and only with the data of a rollback button. The chat is text only: voice, audio, stickers, photos, files and every other non-text message from the owner are ignored without a reply (logged at info level).
- `commands.py` — the `/help` text and the replies of the backend's own commands.
- `status_probe.py` — StatusProbe for `/status`: uptime, RAM, temperature, battery from `/proc` and `/sys/class`, and whether lazarus-ai answers `health`; the running commit and the last push; whether Lazarus speaks aloud (muted, quiet hours, no text-to-speech tool); anything unreadable shows `?`.
- `local_api.py` — LocalAPI on `127.0.0.1:8700` (`POST /send`, `GET /health`) and the client for lazarus-ai's Inbox.
- `outbox.py` — Outbox: a persistent queue of outgoing messages in SQLite (`data/outbox.db`, git-ignored, config key `outbox_db`). It delivers them in order and retries while Telegram is unreachable (backoff 5 s doubling up to 5 min, and right away once polling gets through again), so nothing is lost offline or across a reboot. Priority is stored but not enforced yet. Speech items go to the Speaker, or to Telegram as text while muted, in quiet hours or without a text-to-speech tool (see "Speaking" below); the `/mute` flag is kept in the same database.
- `speaker.py` — Speaker: says speech items aloud with the root-owned text-to-speech tool (`tts_bin`, default `espeak-ng`); QuietHours.
- `typing_indicator.py` — keeps "typing…" visible from forwarding until the reply with matching `in_reply_to` arrives, at most 10 min.
- `supervisor.py` — Supervisor: starts lazarus-ai, restarts it after a crash, honours `/stop`, holds or stops Lazarus after an incident (see "Keeping Lazarus alive" and "Automatic rollback" below). SupervisorState (`data/supervisor.db`, git-ignored, config key `supervisor_db`) holds the stop flag (and whether an incident set it), every crash with its time, and the automatic rollback's budget.
- `auto_rollback.py` — AutoRollback: pure decision after every crash: no incident, roll back to the latest stable commit, or stop (see "Automatic rollback" below).
- `launcher.py` — ProcessLauncher: runs `lazarus_command` as its own process group, its stdout and stderr captured into `logs/lazarus.log` (git-ignored, config key `lazarus_log`), and ends the whole group on stop.
- `inbound_queue.py` — InboundQueue: the owner's text messages wait in SQLite (`data/inbound.db`, git-ignored, config key `inbound_db`) while Lazarus is down or stopped, and are delivered in order once it answers `health` again.
- `repo_manager.py` — RepoManager: the nightly snapshot of Lazarus's folder and memory to `live`, and the restore and commit list behind `/rollback` (see below).
- `snapshot_scheduler.py` — runs the snapshot at `snapshot_time` (default 03:00, local time) and pushes commits that wait: at start, every 10 min, and right away once Telegram is reachable again.
- `stability_ledger.py` — StabilityLedger (`data/ledger.db`, git-ignored, config key `ledger_db`): which commit Lazarus runs since when (a deployment is recorded when a snapshot's code differs from the running commit's, and for every rollback), every incident with the commit that ran, and the last successful push; it answers "the latest stable commit other than X". It exists also with snapshots off.
- `config.py` — Config: `config.json` over built-in defaults; the token from a separate file.
- `app.py` — wiring and `main`.

## Run it on the PC

The backend starts llama-server and lazarus-ai itself (`lazarus_command` `"auto"`, see "Keeping Lazarus alive"); nothing else has to be started by hand.

1. Get a llama.cpp build with `llama-server` and a GGUF model (Lazarus uses `qwen2.5-1.5b-instruct-q4_k_m.gguf`). lazarus-ai is expected in `../lazarus-ai` (`lazarus_folder`).
2. Copy `config.example.json` to `config.json` (both ignored by git except the example). Set `telegram_user_id` to your Telegram user id, or leave it at `null` to find it first (step 5). Set `llama_server_bin` to the llama-server binary (default `llama-server`, found on `PATH`; e.g. `"C:/llama.cpp/llama-server.exe"` or `"~/llama.cpp/build/bin/llama-server"`) and `llama_model` to the model file (default `~/models/qwen2.5-1.5b-instruct-q4_k_m.gguf`). If either can't be found, the backend stops at the start and says which key to fix.
3. Put the bot token from @BotFather into `secrets/telegram.token`, or set the environment variable `LAZARUS_TELEGRAM_TOKEN`.
4. Run from this folder:

   ```
   python -m lazarus_backend            # or: python -m lazarus_backend --config path/to/config.json
   ```

5. Message the bot once. The log shows `ignored message from unknown user id <id>`; put that id into `telegram_user_id` and restart.

This repo is public: `config.json`, `secrets/`, databases and logs are git-ignored and must stay out of it.

## Nightly snapshot

Every night at `snapshot_time` the backend copies Lazarus's folder (`lazarus_folder`) into its own clone of the Lazarus repo (`data/repo`, git-ignored), writes the memory database (`lazarus_memory_db` inside Lazarus's folder) as a text SQL dump `memory.sql` with Python's sqlite3, commits that to `live` and pushes it with the deploy key. Left out: `snapshot_excludes` (the database files, caches, logs, the PC's git-ignored `config.local.json`; Lazarus's own `config.json` is tracked and committed like code), files over `snapshot_size_limit` (5 MB) and symbolic links, which are listed in the commit message. If the push fails, the commit stays in the clone and is pushed later. If `live` moved on GitHub meanwhile, a merge commit joins it; history is never rewritten, so the rulesets never have to reject a push. `/status` shows the running commit and the last push. A failed snapshot is reported in Telegram.

Setup on the phone (root):

1. Put the deploy key's private key into `secrets/deploy_key` (`chmod 600`).
2. Create `secrets/known_hosts` with GitHub's host keys, `ssh-keyscan github.com > secrets/known_hosts`, and check the fingerprints (`ssh-keygen -lf secrets/known_hosts`) against the ones GitHub publishes. ssh refuses any other host key.
3. Set `git_bin` and `ssh_bin` to root-owned binaries if `git` and `ssh` on root's `PATH` aren't.

On a PC without a deploy key, set `repo_remote_url` to `null` to switch snapshots and `/rollback` off.

The backend runs as root and never runs or reads anything Lazarus can write: git runs with an empty hooks folder, without system, global or user configuration, and with the clone's folders given explicitly; commits are made with plumbing commands, which run no hooks. Files are read without following symbolic links, and on Linux only files owned by the owner of Lazarus's folder are read; a root-owned Lazarus folder is refused. Every `.git` in Lazarus's folder is skipped. Lazarus's `.gitignore` and `.gitattributes` are committed as files but have no effect in the clone: files are added with `--force`, and the clone's `info/attributes` switches off filters, line-ending conversion and diff/merge drivers.

## Rollback

`/rollback <sha>` deploys the code of any pushed commit of `main` or `live`; `/rollback <sha> memory` its memory too. `/rollback` alone lists the latest 5 commits of each branch as buttons (short sha, branch, date, message); pressing one asks for confirmation, and confirming deploys that commit's code. A rollback:

1. fetches and checks that the sha is a commit of `main` or `live` on GitHub. An unknown, mistyped or unpushed sha (or no connection) is refused, and nothing changes;
2. takes a snapshot of the current state (the pre-rollback snapshot);
3. stops Lazarus, writes the commit's files into Lazarus's folder and removes the files the snapshot had but the commit hasn't. Excluded files (databases, `config.local.json`, logs, caches) and untracked ones such as files over the size limit stay as they are; Lazarus's `config.json` comes back with the code. With `memory`, the memory database is rebuilt from the commit's `memory.sql`; without, it stays as it is;
4. commits a new commit on `live` whose tree is the restored commit's (code only: with the current `memory.sql`), restarts Lazarus, records the deployment and pushes. `live` only grows: `/rollback <the pre-rollback snapshot>` undoes a rollback.

The owner gets a confirmation naming the restored commit, whether memory was included, and the sha that undoes it. The files are written as root without ever following a link Lazarus planted: folder by folder through descriptors opened with `O_NOFOLLOW`, every file as a new file renamed over the old name, new files and folders owned by the owner of Lazarus's folder. The memory database is rebuilt with Python's sqlite3 in the backend's own folder, with an authorizer that refuses `ATTACH` and everything else a dump doesn't need.

## Keeping Lazarus alive

The Supervisor runs `lazarus_command` (in this folder; `{python}` is the backend's own Python) and checks it every 2 s. The process exiting, or no healthy `health` answer from lazarus-ai for `lazarus_hang_seconds` (60 s; after a start the first answer may take `lazarus_start_seconds`, 180 s, while llama-server loads the model), counts as a crash: the whole process group is killed, the crash is recorded with its time, and Lazarus is started again after 1 s, doubling with every crash in the last 10 min up to 30 s.

- `/stop` ends Lazarus and sets the persisted "stopped by owner" flag, so it stays stopped across a backend restart or reboot. `/restart` clears the flag and (re)starts it right away.
- `/log` shows the last 30 lines of `logs/lazarus.log`: lazarus-ai's (and, unless redirected, llama-server's) output plus the backend's own lines (start, crash, stop). A log over 5 MB is moved to `lazarus.log.1` at the next start.
- `/status` shows Lazarus as `starting` (from a start until the first healthy `health` answer, up to `lazarus_start_seconds`), `up`, `down (restarting)` (after a crash, while the backoff runs, and during an automatic rollback; once the process is launched again it shows `starting`), `stopped by owner` or `stopped after repeated crashes` (an incident stopped it, see below).
- While Lazarus is down or stopped, the owner's text messages wait in the inbound queue and reach Lazarus in order once it's up again. The first message of such an outage is answered with "Lazarus is down, message queued"; the rest queue silently.

`scripts/run_lazarus.py` is the launcher the default command uses. It runs lazarus-ai (`python -m lazarus_ai` in `--lazarus-dir`) and, given a llama-server command after `--`, llama-server first, waiting until its `/health` answers 200. When either exits, it ends the other and exits, which the Supervisor sees as a crash. `--llama-log FILE` keeps llama-server's chatty output out of `/log`.

### Automatic rollback

`incident_crashes` (3) crashes within `incident_window_seconds` (600 s) make an **incident**. It is recorded in the ledger with the running commit, and then:

- **Rollback:** the Supervisor holds Lazarus stopped, and the backend restores the latest **stable** commit other than the running one, code only, exactly like `/rollback <sha>`: the pre-rollback snapshot saves the broken state as a commit first, and the restore is a new commit on `live`. Then Lazarus starts again, and the owner gets an urgent note naming the broken commit, the restored one, the commit that holds the broken state (`/rollback` to it brings it back) and the last 10 lines of `logs/lazarus.log`.
- **Stop:** instead, Lazarus is stopped with the persisted stop flag and the owner gets an urgent alert (with the same log lines) when there's no stable commit, when snapshots are off (`repo_remote_url` null), when the restore fails (e.g. GitHub unreachable), or when the restored version produces an incident too. It never restarts in a loop; `/status` shows `stopped after repeated crashes` until `/restart` or `/rollback`.

A commit is **stable** once it has run `stable_seconds` (86400 s, 24 h) without an incident: until the next deployment, or until now for the running one. An incident during those 24 h disqualifies it; one later doesn't.

**One automatic rollback per incident:** an incident within 24 h after an automatic rollback stops Lazarus instead of rolling back again. The owner's `/restart` or `/rollback` gives a fresh start: earlier crashes no longer count, and the next incident may roll back again. The budget also comes back once the restored version has run 24 h without an incident. Crashes that made an incident never count towards the next one. The decision is persisted when it's made, so a reboot in the middle of a rollback can't lead to a second one.

### The default command on the PC

`lazarus_command` `"auto"` (the default) builds this from the config:

```
{python} scripts/run_lazarus.py --lazarus-dir <lazarus_folder> [--lazarus-config <lazarus_config>]
    --llama-health http://127.0.0.1:<llama_port>/health --llama-log <llama_log>
    -- <llama_server_bin> -m <llama_model> --host 127.0.0.1 --port <llama_port> -c <llama_context> -t <llama_threads> <llama_args...>
```

| Key | Default | |
| --- | --- | --- |
| `llama_server_bin` | `llama-server` | a name on `PATH` or a path; `null`: llama-server is started by hand |
| `llama_model` | `~/models/qwen2.5-1.5b-instruct-q4_k_m.gguf` | the GGUF model; `null`: llama-server is started by hand |
| `llama_port` | `8080` | must match `llm_url` in lazarus-ai's config |
| `llama_context` | `4096` | context size in tokens; `null`: llama-server's default |
| `llama_threads` | `4` | CPU threads; `null`: llama-server's default |
| `llama_args` | `[]` | further llama-server arguments, e.g. `["-ngl", "99"]` for a GPU build |
| `llama_log` | `logs/llama-server.log` | llama-server's output, kept out of `/log`; `null`: into `logs/lazarus.log` |
| `lazarus_config` | `null` | lazarus-ai's config file, relative to `lazarus_folder`; `null`: its tracked `config.json`. Set `"config.local.json"` on the PC only; on the phone Lazarus never runs from an untracked config |

Paths are relative to the config file's folder, and `~` is the user's home. With `llama_server_bin` or `llama_model` set to `null`, the command starts only lazarus-ai and llama-server must already run on `llama_port`. lazarus-ai runs with its tracked `config.json` (whose `llm_url` is `http://127.0.0.1:8080`) unless `lazarus_config` names a local override. An explicit list in `lazarus_command` replaces the whole command, as on the phone.

### On the phone

On the phone the command starts the launcher inside the Libertine container as the phone user. The exact form is settled when installing (issues 01 and 12); it will look like:

```json
"lazarus_command": ["sudo", "-u", "phablet", "libertine-launch", "-i", "ai",
                    "python3", "/userdata/lazarus-backend/scripts/run_lazarus.py",
                    "--lazarus-dir", "/home/phablet/lazarus",
                    "--llama-log", "/home/phablet/ai/logs/llama-server.log",
                    "--", "/home/phablet/ai/llama.cpp/build/bin/llama-server",
                    "-m", "/home/phablet/ai/models/qwen2.5-1.5b-instruct-q4_k_m.gguf",
                    "-t", "4", "-c", "4096", "--host", "127.0.0.1", "--port", "8080"]
```

To check there: the container must see `scripts/run_lazarus.py`, and stopping must reach llama-server and lazarus-ai through `sudo` and `libertine-launch` (the Supervisor signals the process group). The backend's systemd unit should keep the default `KillMode=control-group`, so a restart of the backend doesn't leave an old Lazarus holding its ports.

## Speaking

lazarus-ai chooses per message whether it goes to Telegram or is said aloud: `POST /send` with `"channel": "speech"`. Nothing in lazarus-ai sends speech items in milestone 1; to try it:

```
curl -X POST http://127.0.0.1:8700/send -H "Content-Type: application/json" -d '{"text": "Good morning.", "channel": "speech"}'
```

- The Speaker runs `tts_bin` with `tts_args` (default `espeak-ng -v en --stdin`, English voice) and passes the text on stdin. `tts_bin` is a name on the backend's `PATH` or a path (relative to the config file's folder); it must be root-owned, and a tool inside Lazarus's folder (`lazarus_folder`) is never run.
- `/mute` sends everything Lazarus would say aloud to Telegram as text instead, until `/unmute`. The flag survives restarts (it's kept in `outbox_db`). `/status` shows it.
- In quiet hours (`quiet_hours`, default `["22:00", "07:00"]`, local time, start included, end excluded, may cross midnight; `null` for none) speech items go to Telegram as text too. What counts is the time of delivery, not when the item was sent.
- If the tool can't be found (usual on a PC; the log warns once) or fails, speech items go to Telegram as text as well. `tts_bin: null` switches speaking off for good.

## Tests

```
python -m unittest discover
```

They need no network, no Telegram and no text-to-speech tool (the Speaker's subprocess is faked): fake Telegram and lazarus-ai servers run on `127.0.0.1`. The RepoManager tests need `git` on `PATH`; they work on temporary repositories with a local bare remote that refuses non-fast-forward pushes and deletions, like GitHub's rulesets. GitHub Actions runs them on Ubuntu and Windows with Python 3.8 and the current 3.x (`.github/workflows/tests.yml`).
