#!/usr/bin/env python3
"""Stand-in for `virt-install --print-xml --dry-run` in tests.

Prints a domain XML that carries the name, the UUID and every other argument,
so a test sees what agentbox asked for and agentbox can compare two runs.
Without --uuid it makes one up, different on every call, like the real one.

Environment:
  FAKE_DIR   calls are recorded in calls.jsonl as {"cmd": "virt-install", "argv": [...]}

scenario.json:
  virt_install_fails   domain names for which it fails
"""
import json
import os
import sys
from xml.sax.saxutils import escape

DIR = os.environ["FAKE_DIR"]


def main(args):
    with open(os.path.join(DIR, "calls.jsonl"), "a") as f:
        f.write(json.dumps({"cmd": "virt-install", "argv": args}) + "\n")
    if "--print-xml" not in args or "--dry-run" not in args:
        print("virt-install (fake): only --print-xml --dry-run", file=sys.stderr)
        return 1
    name = args[args.index("--name") + 1]
    try:
        with open(os.path.join(DIR, "scenario.json")) as f:
            if name in json.load(f).get("virt_install_fails", []):
                print("ERROR    Error: --disk: something virt-install did not like", file=sys.stderr)
                return 1
    except FileNotFoundError:
        pass
    if "--uuid" in args:
        i = args.index("--uuid")
        uuid, args = args[i + 1], args[:i] + args[i + 2:]
    else:
        counter = os.path.join(DIR, "uuids")
        with open(counter, "a") as f:
            f.write("x")
        uuid = "00000000-0000-0000-0000-%012d" % os.path.getsize(counter)
    print(f"<domain type='kvm'>\n  <name>{escape(name)}</name>\n  <uuid>{uuid}</uuid>\n"
          f"  <args>{escape(json.dumps(args))}</args>\n</domain>")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
