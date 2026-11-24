#!/usr/bin/env python3
"""Stand-in for `systemd-run` in tests: records every call and succeeds, starting nothing (a
build VM's virtiofsd, agentbox/build_vm_run.py; fake_build_vm.py finds the directory it would
serve in the recorded call).

Environment:
  FAKE_DIR   calls are recorded in calls.jsonl as {"cmd": "systemd-run", "argv": [...]}

scenario.json:
  systemd_run_fails   true: it fails as systemd does for a unit that exists already
"""
import json
import os
import sys

DIR = os.environ["FAKE_DIR"]
with open(os.path.join(DIR, "calls.jsonl"), "a") as f:
    f.write(json.dumps({"cmd": "systemd-run", "argv": sys.argv[1:]}) + "\n")
try:
    with open(os.path.join(DIR, "scenario.json")) as f:
        scenario = json.load(f)
except FileNotFoundError:
    scenario = {}
if scenario.get("systemd_run_fails"):
    sys.exit("Failed to start transient socket unit: Unit agentbox-buildvm-x.socket was already loaded or has a "
             "fragment file.")
