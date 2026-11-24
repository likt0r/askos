#!/usr/bin/env python3
"""Stand-in for `mkfs.ext4`, `mount` and `umount` on the Host in tests (a build VM's output
filesystem, agentbox/build_vm_run.py): records each call and succeeds; nothing is made or
mounted, so the output directory stays a plain directory of the test root.

argv: the program it plays, then that program's arguments.

Environment: FAKE_DIR (calls in calls.jsonl as {"cmd": <program>, "argv": [...]}, scenario.json).

scenario.json:
  mount_fails   programs (e.g. ["mkfs.ext4"]) that fail
"""
import json
import os
import sys


def main(program, argv):
    with open(os.path.join(os.environ["FAKE_DIR"], "calls.jsonl"), "a") as f:
        f.write(json.dumps({"cmd": program, "argv": argv}) + "\n")
    try:
        with open(os.path.join(os.environ["FAKE_DIR"], "scenario.json")) as f:
            fails = json.load(f).get("mount_fails") or []
    except FileNotFoundError:
        fails = []
    if program in fails:
        print(f"{program}: failed (fake)", file=sys.stderr)
        return 32
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2:]))
