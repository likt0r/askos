#!/usr/bin/env python3
"""Stand-in for `setpriv` in tests: records the call and runs the program as the test's own
user, with FAKE_SETPRIV_USER set to the user it was to run as, so the program's fake can tell
it was started through setpriv and as whom.

Environment:
  FAKE_DIR   calls are recorded in calls.jsonl as {"cmd": "setpriv", "argv": [...]}

  setpriv --reuid=<user> --regid=<group> --clear-groups --no-new-privs -- <program> <args>...
"""
import json
import os
import sys

DIR = os.environ["FAKE_DIR"]
OPTIONS = ("--reuid=", "--regid=", "--clear-groups", "--no-new-privs")


def main(argv):
    with open(os.path.join(DIR, "calls.jsonl"), "a") as f:
        f.write(json.dumps({"cmd": "setpriv", "argv": argv}) + "\n")
    if "--" not in argv:
        print("setpriv (fake): agentbox must end the options with --", file=sys.stderr)
        return 1
    options, command = argv[:argv.index("--")], argv[argv.index("--") + 1:]
    if any(not o.startswith(OPTIONS) for o in options) or not command:
        print("setpriv (fake): unexpected arguments", file=sys.stderr)
        return 1
    user = next((o.split("=", 1)[1] for o in options if o.startswith("--reuid=")), "")
    os.environ["FAKE_SETPRIV_USER"] = user
    os.execvp(command[0], command)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
