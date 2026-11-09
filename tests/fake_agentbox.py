#!/usr/bin/env python3
"""Stand-in for `agentbox` in tests of the Control Room backend (web/agentbox-web).

Environment:
  FAKE_AGENTBOX_DIR   directory holding response.json; calls are recorded in calls.jsonl

response.json (optional):
  stdout     printed on stdout (default: a canned `agentbox logs --json` answer)
  stderr     printed on stderr (default "")
  exitcode   exit status (default 0)

Calls with --no-prompt (the commands that read a secret) also record what they
got on stdin, one JSON string per call in stdin.jsonl; other calls never read
stdin, which the server does not provide for them.

`agentbox watch` (the backend starts it once, with its own stdin pipe) is not a call:
each start is counted in watch-starts.jsonl instead. It prints the lines of
watch.jsonl that exist (and those added later) and then waits until the server closes
its stdin; watch.json may say {"exit": N} to end at once after printing (a watch that
dies), and {"stderr": "..."} to print on stderr first.
"""
import json
import os
import sys
import threading
import time

DIR = os.environ["FAKE_AGENTBOX_DIR"]

DEFAULT = {"vm": "priv", "entries": [], "cursor": None, "truncated": False}


def watch():
    with open(os.path.join(DIR, "watch-starts.jsonl"), "a") as f:
        f.write(json.dumps({"time": time.time()}) + "\n")
    try:
        with open(os.path.join(DIR, "watch.json")) as f:
            conf = json.load(f)
    except FileNotFoundError:
        conf = {}
    if conf.get("stderr"):
        print(conf["stderr"], file=sys.stderr, flush=True)
    path, sent = os.path.join(DIR, "watch.jsonl"), 0

    def send():
        nonlocal sent
        try:
            with open(path) as f:
                lines = f.read().splitlines()
        except FileNotFoundError:
            return
        for line in lines[sent:]:
            print(line, flush=True)
        sent = len(lines)
    send()
    if "exit" in conf:
        return conf["exit"]
    gone = threading.Event()
    threading.Thread(target=lambda: (sys.stdin.read(), gone.set()), daemon=True).start()
    while not gone.wait(0.05):
        send()
    return 0


def main(args):
    if args == ["watch"]:
        return watch()
    with open(os.path.join(DIR, "calls.jsonl"), "a") as f:
        f.write(json.dumps(args) + "\n")
    if "--no-prompt" in args:
        with open(os.path.join(DIR, "stdin.jsonl"), "a") as f:
            f.write(json.dumps(sys.stdin.read()) + "\n")
    try:
        with open(os.path.join(DIR, "response.json")) as f:
            r = json.load(f)
    except FileNotFoundError:
        r = {}
    out = r["stdout"] if "stdout" in r else json.dumps(DEFAULT)
    if out:
        print(out)
    if r.get("stderr"):
        print(r["stderr"], file=sys.stderr)
    return r.get("exitcode", 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
