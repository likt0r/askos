#!/usr/bin/env python3
"""Stand-in for `chattr` in tests: records every call and succeeds, unless the scenario's
chattr_fails says the file system does not support the attribute (as anything but btrfs
answers +C).

Environment:
  FAKE_DIR   calls are recorded in calls.jsonl as {"cmd": "chattr", "argv": [...]}
"""
import json
import os
import sys

DIR = os.environ["FAKE_DIR"]
with open(os.path.join(DIR, "calls.jsonl"), "a") as f:
    f.write(json.dumps({"cmd": "chattr", "argv": sys.argv[1:]}) + "\n")
try:
    with open(os.path.join(DIR, "scenario.json")) as f:
        scenario = json.load(f)
except FileNotFoundError:
    scenario = {}
if scenario.get("chattr_fails"):
    sys.exit(f"chattr: Operation not supported while setting flags on {sys.argv[-1]}")
