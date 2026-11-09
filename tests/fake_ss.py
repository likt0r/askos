#!/usr/bin/env python3
"""Stand-in for `ss` in tests: established TCP connections, driven by scenario.json.

Environment:
  FAKE_DIR   directory holding scenario.json; calls are recorded in calls.jsonl
             as {"cmd": "ss", "argv": [...]}

scenario.json:
  connections   {"<ip>:<port>": count} of established connections per destination
  ss_fails      ss exits 1 with an error (default false)

  ss -Htn state established "( dst <ip>:<port> )"   one line per connection
"""
import json
import os
import re
import sys

DIR = os.environ["FAKE_DIR"]


def main(args):
    with open(os.path.join(DIR, "calls.jsonl"), "a") as f:
        f.write(json.dumps({"cmd": "ss", "argv": args}) + "\n")
    try:
        with open(os.path.join(DIR, "scenario.json")) as f:
            scenario = json.load(f)
    except FileNotFoundError:
        scenario = {}
    if scenario.get("ss_fails"):
        print("ss (fake): Cannot open netlink socket", file=sys.stderr)
        return 1
    m = re.fullmatch(r"\( dst (\S+) \)", args[-1]) if args[:3] == ["-Htn", "state", "established"] else None
    if not m:
        print("ss (fake): unexpected arguments", file=sys.stderr)
        return 1
    dst = m.group(1)
    for i in range((scenario.get("connections") or {}).get(dst, 0)):
        print(f"0      0      192.0.2.{20 + i}:{50000 + i}      {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
