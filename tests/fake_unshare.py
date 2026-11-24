#!/usr/bin/env python3
"""Stand-in for `unshare` in tests: records the call and runs the program in the test's own
namespaces (a real unshare --net needs root), with FAKE_UNSHARE set to the namespaces it was
to leave, so the program's fake can tell.

Environment:
  FAKE_DIR   calls are recorded in calls.jsonl as {"cmd": "unshare", "argv": [...]}

  unshare --net -- <program> <args>...
"""
import json
import os
import sys

DIR = os.environ["FAKE_DIR"]


def main(argv):
    with open(os.path.join(DIR, "calls.jsonl"), "a") as f:
        f.write(json.dumps({"cmd": "unshare", "argv": argv}) + "\n")
    if "--" not in argv:
        print("unshare (fake): agentbox must end the options with --", file=sys.stderr)
        return 1
    options, command = argv[:argv.index("--")], argv[argv.index("--") + 1:]
    if options != ["--net"] or not command:
        print("unshare (fake): unexpected arguments", file=sys.stderr)
        return 1
    os.environ["FAKE_UNSHARE"] = "net"
    os.execvp(command[0], command)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
