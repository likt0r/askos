#!/usr/bin/env python3
"""Stand-in for the Orca CLI (orca-ide) in tests, driven by scenario.json.

Environment:
  FAKE_DIR             directory holding scenario.json; calls are recorded in calls.jsonl
                       as {"cmd": "orca", "argv": [...], "user_data": ORCA_USER_DATA_PATH}
  ORCA_USER_DATA_PATH  set by agentbox: the Observer's data directory

scenario.json:
  orca   per environment (VM name), e.g.
           {"work": {"worktrees": [{"agents": [{"state": "working"}]}]}}
         or {"work": {"exit": 1}} (Orca not answering) or {"work": {"stdout": "..."}};
         "add_exit": exit code of `environment add` (default 0)

  orca-ide worktree ps --json --environment <vm>
  orca-ide environment rm --environment <vm>
  orca-ide environment add --name <vm> --pairing-code <code>
"""
import json
import os
import sys

DIR = os.environ["FAKE_DIR"]


def main(args):
    with open(os.path.join(DIR, "calls.jsonl"), "a") as f:
        f.write(json.dumps({"cmd": "orca", "argv": args, "user_data": os.environ.get("ORCA_USER_DATA_PATH")}) + "\n")
    try:
        with open(os.path.join(DIR, "scenario.json")) as f:
            orca = json.load(f).get("orca") or {}
    except FileNotFoundError:
        orca = {}
    if args[:3] == ["worktree", "ps", "--json"]:
        env = orca.get(args[args.index("--environment") + 1], {})
        if env.get("exit"):
            print("Error: environment is not reachable", file=sys.stderr)
            return env["exit"]
        if "stdout" in env:
            print(env["stdout"])
            return 0
        print(json.dumps({"id": "1", "ok": True, "result": {"worktrees": env.get("worktrees", [])}}))
        return 0
    if args[:2] == ["environment", "rm"]:
        print("Removed environment")
        return 0
    if args[:2] == ["environment", "add"]:
        code = orca.get("add_exit", 0)
        if code:
            print("Error: pairing failed: invalid pairing code", file=sys.stderr)
            return code
        print("Added environment")
        return 0
    print("orca (fake): unexpected arguments", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
