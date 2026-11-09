#!/usr/bin/env python3
"""Stand-in for `getent`, `usermod` and `gpasswd` in tests: the users and groups of the Host.
Called as `fake_accounts.py <program> <arguments>...`.

Environment:
  FAKE_DIR   calls are recorded in calls.jsonl as {"cmd": <program>, "argv": [...]}; the users
             and groups, once a call changed them, in accounts.json

scenario.json:
  accounts   {"users": {name: {"uid": N, "gid": N, "home": path, "shell": path (default /bin/bash)}},
              "groups": {name: {"gid": N, "members": [name, ...]}}}  before any change

What it understands: getent passwd|group [<name>] (exit 2 when there is no such entry),
usermod -aG <group> <user>, gpasswd -M <user>,... <group>.
"""
import fcntl
import json
import os
import sys
import tempfile

DIR = os.environ["FAKE_DIR"]
program, args = sys.argv[1], sys.argv[2:]
with open(os.path.join(DIR, "calls.jsonl"), "a") as f:
    f.write(json.dumps({"cmd": program, "argv": args}) + "\n")
try:
    with open(os.path.join(DIR, "scenario.json")) as f:
        scenario = json.load(f)
except FileNotFoundError:
    scenario = {}


def save(state):
    fd, tmp = tempfile.mkstemp(dir=DIR, prefix=".accounts.")
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)
    os.replace(tmp, os.path.join(DIR, "accounts.json"))


with open(os.path.join(DIR, "accounts.lock"), "a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        with open(os.path.join(DIR, "accounts.json")) as f:
            state = json.load(f)
    except FileNotFoundError:
        state = scenario.get("accounts", {"users": {}, "groups": {}})
    users, groups = state["users"], state["groups"]

    if program == "getent":
        database, keys = args[0], args[1:]
        if database == "passwd":
            rows = [f"{n}:x:{u['uid']}:{u['gid']}::{u['home']}:{u.get('shell', '/bin/bash')}"
                    for n, u in users.items()
                    if not keys or n in keys]
        else:
            rows = [f"{n}:x:{g['gid']}:{','.join(g['members'])}" for n, g in groups.items() if not keys or n in keys]
        print("\n".join(rows))
        sys.exit(0 if rows or not keys else 2)
    if program == "usermod" and args[0] == "-aG":
        group, user = args[1], args[2]
        if user not in users or group not in groups:
            sys.exit(f"usermod: user '{user}' or group '{group}' does not exist")
        if user not in groups[group]["members"]:
            groups[group]["members"].append(user)
    elif program == "gpasswd" and args[0] == "-M":
        members, group = [m for m in args[1].split(",") if m], args[2]
        if group not in groups:
            sys.exit(f"gpasswd: group '{group}' does not exist in /etc/group")
        groups[group]["members"] = members
    else:
        sys.exit(f"fake_accounts.py: {program} {args} not understood")
    save(state)
