#!/usr/bin/env python3
"""Stand-in for `mkfs.ext4`, `mount` and `umount` on the Host in tests (a build VM's output
filesystem, agentbox/build_vm_run.py): records each call and succeeds; nothing is made or
mounted, so the output directory stays a plain directory of the test root.

argv: the program it plays, then that program's arguments.

Environment: FAKE_DIR (calls in calls.jsonl as {"cmd": <program>, "argv": [...]}, scenario.json).

scenario.json:
  mount_fails   programs (e.g. ["mkfs.ext4"]) that fail

A call of mkfs.ext4 also records "file": the size, mode and allocated bytes of its image file.
"""
import json
import os
import sys


def main(program, argv):
    call = {"cmd": program, "argv": argv}
    if program == "mkfs.ext4" and argv and os.path.isfile(argv[-1]):
        st = os.stat(argv[-1])       # the image file as root made it: it is gone after the Build
        call["file"] = {"size": st.st_size, "mode": st.st_mode & 0o777, "allocated": st.st_blocks * 512}
    with open(os.path.join(os.environ["FAKE_DIR"], "calls.jsonl"), "a") as f:
        f.write(json.dumps(call) + "\n")
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
