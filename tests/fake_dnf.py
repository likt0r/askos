#!/usr/bin/env python3
"""Stand-in for `dnf` in tests: only `dnf -q --disablerepo=* needs-restarting`.

Environment:
  FAKE_DIR   calls are recorded in calls.jsonl as {"cmd": "dnf", "argv": [...]}

scenario.json:
  needs_restarting   {"exit": 0|1|..., "stdout": "..."} (default: exit 0, nothing to restart)
"""
import json
import os
import sys

DIR = os.environ["FAKE_DIR"]


def main(args):
    with open(os.path.join(DIR, "calls.jsonl"), "a") as f:
        f.write(json.dumps({"cmd": "dnf", "argv": args}) + "\n")
    try:
        with open(os.path.join(DIR, "scenario.json")) as f:
            scenario = json.load(f)
    except FileNotFoundError:
        scenario = {}
    if "needs-restarting" not in args:
        print("dnf (fake): unexpected arguments", file=sys.stderr)
        return 2
    r = scenario.get("needs_restarting") or {}
    print(r.get("stdout", "No core libraries or services have been updated since boot-up.\n"
                          "Reboot should not be necessary."))
    return r.get("exit", 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
