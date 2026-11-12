#!/usr/bin/env python3
"""What a unit runs in tests, started by fake_systemctl.py (scenario systemctl_run): records its
start and end in calls.jsonl, and as told starts another unit, holds the run lock of the Builds
for a while (a slow Build), makes a Build the current one of its Image or records it as failed
(on the request of an actor, --actor, `timer` unless given), and exits with an exit code. Its run
(when it began, its result) goes to units/NAME.service.json in FAKE_DIR, which fake_systemctl.py's
`show` prints.

  fake_unit.py NAME [--start UNIT] [--take-request TARGET] [--delay SECONDS] [--hold SECONDS]
               [--build IMAGE BUILD | --fail-build IMAGE BUILD] [--target TARGET] [--actor ACTOR]
               [--exit N]

Environment: FAKE_DIR (calls.jsonl), AGENTBOX_ROOT (the run lock, the Builds).
"""
import argparse
import fcntl
import json
import os
import subprocess
import sys
import time

DIR, ROOT = os.environ["FAKE_DIR"], os.environ["AGENTBOX_ROOT"]
STATE = os.path.join(ROOT, "var/lib/agentbox")


def log(*argv):
    with open(os.path.join(DIR, "calls.jsonl"), "a") as f:
        f.write(json.dumps({"cmd": "unit", "argv": list(argv)}) + "\n")


def record(image, build, status, target, actor):
    os.makedirs(os.path.join(STATE, "builds"), exist_ok=True)
    with open(os.path.join(STATE, "builds", f"{image}-{build}.json"), "w") as f:
        json.dump({"image": image, "build": build, "status": status, "started": int(time.time()),
                   "finished": int(time.time()), "unit": f"agentbox-build@{target}.service", "actor": actor}, f)


def ran(result):
    """This run as systemd keeps it: when it left the inactive state, and its result."""
    os.makedirs(os.path.join(DIR, "units"), exist_ok=True)
    path = os.path.join(DIR, "units", a.name + ".service.json")
    with open(path + ".new", "w") as f:
        json.dump({"InactiveExitTimestamp": f"@{BEGAN}", "Result": result}, f)
    os.replace(path + ".new", path)


p = argparse.ArgumentParser()
p.add_argument("name")
p.add_argument("--start")
p.add_argument("--take-request")
p.add_argument("--delay", type=float, default=0)       # before it takes the run lock
p.add_argument("--hold", type=float)
p.add_argument("--build", nargs=2)
p.add_argument("--fail-build", nargs=2)
p.add_argument("--target", default="update")
p.add_argument("--actor", default="timer")      # of the request the Builds it records were made on
p.add_argument("--exit", type=int, default=0)
a = p.parse_args()
BEGAN = int(time.time())
log(a.name, "start")
if a.take_request:
    try:
        os.remove(os.path.join(STATE, "builds", f"request-{a.take_request}.json"))
    except FileNotFoundError:
        pass
if a.start:
    subprocess.run(["systemctl", "start", "--no-block", a.start], check=True)
time.sleep(a.delay)
if a.hold is not None:
    os.makedirs(os.path.join(STATE, "locks"), exist_ok=True)
    with open(os.path.join(STATE, "locks", "build-run.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        time.sleep(a.hold)
        if a.build:
            image, build = a.build
            disk = os.path.join(STATE, "images", f"{image}-{build}.qcow2")
            open(disk, "w").close()
            link = os.path.join(STATE, "images", f"{image}.qcow2")
            os.symlink(os.path.basename(disk), link + ".new")
            os.replace(link + ".new", link)
            record(image, build, "ok", a.target, a.actor)
        if a.fail_build:
            record(*a.fail_build, "failed", a.target, a.actor)
        log(a.name, "end")
else:
    log(a.name, "end")
ran("exit-code" if a.exit else "success")
sys.exit(a.exit)
