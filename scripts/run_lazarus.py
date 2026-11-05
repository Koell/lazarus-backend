"""Runs Lazarus: llama-server (optional) and lazarus-ai's Inbox, as one unit that lives and dies together.

The backend's Supervisor starts this script as a process group and treats its exit as a crash:

    python scripts/run_lazarus.py --lazarus-dir ../lazarus-ai [-- llama-server -m model.gguf --port 8080 ...]

With a llama-server command after `--`, it starts llama-server first and waits until its `/health`
answers 200 (the model is loaded), then starts `python -m lazarus_ai` in `--lazarus-dir`. Without one
it starts only lazarus-ai (llama-server is then started by hand). When either process
exits, the other is ended and this script exits too, with a non-zero code.

Output of both goes to this script's stdout/stderr, which the backend captures into its log, unless
`--llama-log` sends llama-server's (chatty) output into a file of its own.

Standard library only; it runs with whatever Python lazarus-ai uses (inside the Libertine container
on the phone), so it must not import anything from lazarus_backend.
"""

import argparse
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

POLL_SECONDS = 0.5


def say(text):
    sys.stderr.write("run_lazarus: {}\n".format(text))
    sys.stderr.flush()


def parse_args(argv):
    if "--" in argv:
        split = argv.index("--")
        argv, llama = argv[:split], argv[split + 1:]
    else:
        llama = []
    parser = argparse.ArgumentParser(prog="run_lazarus.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--lazarus-dir", required=True, help="lazarus-ai's folder (holds lazarus_ai/)")
    parser.add_argument("--python", default=sys.executable, help="Python for lazarus-ai (default: this one)")
    parser.add_argument("--llama-health", default="http://127.0.0.1:8080/health",
                        help="llama-server's health URL, polled until it answers 200")
    parser.add_argument("--lazarus-config", help="lazarus-ai's config file, relative to --lazarus-dir "
                                                 "(default: its tracked config.json)")
    parser.add_argument("--llama-log", help="file for llama-server's output (default: this script's output)")
    args = parser.parse_args(argv)
    args.llama = llama
    return args


def healthy(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def end(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    children = []

    def on_signal(signum, frame):
        say("got signal {}, ending".format(signum))
        raise SystemExit(128 + signum)  # the finally below ends the children

    signal.signal(signal.SIGTERM, on_signal)
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    llama_log = None
    try:
        if args.llama:
            if args.llama_log:
                os.makedirs(os.path.dirname(os.path.abspath(args.llama_log)), exist_ok=True)
                llama_log = open(args.llama_log, "ab")
            say("starting llama-server: " + " ".join(args.llama))
            llama = subprocess.Popen(args.llama, stdin=subprocess.DEVNULL, stdout=llama_log,
                                     stderr=subprocess.STDOUT if llama_log else None)
            children.append(llama)
            while not healthy(args.llama_health):
                if llama.poll() is not None:
                    say("llama-server exited with code {} before it was ready".format(llama.returncode))
                    return 1
                time.sleep(POLL_SECONDS)
            say("llama-server is ready")

        say("starting lazarus-ai in " + args.lazarus_dir)
        lazarus_command = [args.python, "-m", "lazarus_ai"]
        if args.lazarus_config:
            lazarus_command += ["--config", args.lazarus_config]
        lazarus = subprocess.Popen(lazarus_command, cwd=args.lazarus_dir, env=env, stdin=subprocess.DEVNULL)
        children.append(lazarus)
        while True:
            for name, child in zip(("llama-server", "lazarus-ai") if args.llama else ("lazarus-ai",), children):
                if child.poll() is not None:
                    say("{} exited with code {}".format(name, child.returncode))
                    return child.returncode or 1
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        return 130
    finally:
        for child in reversed(children):
            end(child)
        if llama_log is not None:
            llama_log.close()


if __name__ == "__main__":
    sys.exit(main())
