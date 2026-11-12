#!/usr/bin/env python3
"""Stand-in for `systemctl` in tests: records every call and succeeds.

Environment:
  FAKE_DIR   calls are recorded in calls.jsonl as {"cmd": "systemctl", "argv": [...]}

scenario.json:
  systemctl_fails   verbs (e.g. ["restart"]) that fail
  systemctl_active  units that `systemctl is-active` reports active (all others: inactive, exit 3)
  systemctl_enabled units that `systemctl is-enabled` reports enabled before any change; `enable`
                    and `disable` change that (in systemctl.json), all others: disabled, exit 1
  systemctl_units   rows [unit, load, active, sub, description] that `systemctl list-units`
                    prints (plain, without legend) when they match one of its patterns
  systemctl_triggered_by  {unit: [timers and sockets]}: the TriggeredBy that `systemctl show`
                    prints for a unit (all others: none)
  systemctl_run     {unit: argv}: what `systemctl start <unit>` runs, as systemd runs a unit's
                    ExecStart: to its end, exiting with its exit code (a oneshot service), or
                    with --no-block in the background, while `systemctl is-active` reports the
                    unit active (running/<unit>.pid in FAKE_DIR)
  systemctl_queue   {unit: N}: a unit started with --no-block is queued first, as a start job that
                    waits: the first N `systemctl is-active` calls for it say inactive and the Nth
                    makes it run (and report active from then on); until then it has run nothing
"""
import fcntl
import fnmatch
import json
import os
import subprocess
import sys
import tempfile

DIR = os.environ["FAKE_DIR"]
with open(os.path.join(DIR, "calls.jsonl"), "a") as f:
    f.write(json.dumps({"cmd": "systemctl", "argv": sys.argv[1:]}) + "\n")
try:
    with open(os.path.join(DIR, "scenario.json")) as f:
        scenario = json.load(f)
except FileNotFoundError:
    scenario = {}
if sys.argv[1] in scenario.get("systemctl_fails", []):
    sys.exit(f"Job for {sys.argv[-1]} failed.")
if sys.argv[1] in ("enable", "disable", "is-enabled"):
    with open(os.path.join(DIR, "systemctl.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(os.path.join(DIR, "systemctl.json")) as f:
                enabled = set(json.load(f)["enabled"])
        except FileNotFoundError:
            enabled = set(scenario.get("systemctl_enabled", []))
        units = {a for a in sys.argv[2:] if not a.startswith("-")}
        if sys.argv[1] == "is-enabled":
            print("enabled" if units <= enabled else "disabled")
            sys.exit(0 if units <= enabled else 1)
        enabled = enabled | units if sys.argv[1] == "enable" else enabled - units
        fd, tmp = tempfile.mkstemp(dir=DIR, prefix=".systemctl.")
        with os.fdopen(fd, "w") as f:
            json.dump({"enabled": sorted(enabled)}, f)
        os.replace(tmp, os.path.join(DIR, "systemctl.json"))


def running(unit):
    """Whether what `start --no-block` started for the unit still runs (not ended, no zombie)."""
    try:
        with open(os.path.join(DIR, "running", unit + ".pid")) as f:
            pid = int(f.read())
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, ValueError, IndexError):
        return False


if sys.argv[1] == "start":
    runs = scenario.get("systemctl_run", {})
    for unit in (a for a in sys.argv[2:] if not a.startswith("-") and a in runs):
        if "--no-block" in sys.argv and scenario.get("systemctl_queue", {}).get(unit):
            os.makedirs(os.path.join(DIR, "running"), exist_ok=True)
            with open(os.path.join(DIR, "running", unit + ".queued"), "w") as f:
                f.write(str(scenario["systemctl_queue"][unit]))
        elif "--no-block" in sys.argv:
            os.makedirs(os.path.join(DIR, "running"), exist_ok=True)
            p = subprocess.Popen(runs[unit], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
            with open(os.path.join(DIR, "running", unit + ".pid"), "w") as f:
                f.write(str(p.pid))
        else:
            code = subprocess.run(runs[unit], stdin=subprocess.DEVNULL).returncode
            if code:
                sys.exit(f"Job for {unit} failed because the control process exited with error code.")
def dequeue(unit):
    """One is-active call for a queued unit: the Nth of them starts what it runs."""
    queued = os.path.join(DIR, "running", unit + ".queued")
    try:
        with open(queued) as f:
            left = int(f.read()) - 1
    except (OSError, ValueError):
        return
    if left > 0:
        with open(queued, "w") as f:
            f.write(str(left))
        return
    os.remove(queued)
    p = subprocess.Popen(scenario["systemctl_run"][unit], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    with open(os.path.join(DIR, "running", unit + ".pid"), "w") as f:
        f.write(str(p.pid))


if sys.argv[1] == "is-active":
    dequeue(sys.argv[-1])
    active = sys.argv[-1] in scenario.get("systemctl_active", []) or running(sys.argv[-1])
    print("active" if active else "inactive")
    sys.exit(0 if active else 3)
if sys.argv[1] == "list-units":
    patterns = [a for a in sys.argv[2:] if not a.startswith("-")]
    for row in scenario.get("systemctl_units", []):
        if not patterns or any(fnmatch.fnmatchcase(row[0], p) for p in patterns):
            print("  ".join(row))
if sys.argv[1] == "show":
    names = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else \
        [a for a in sys.argv[2:] if not a.startswith("-")]
    triggers = scenario.get("systemctl_triggered_by", {})
    # one block per unit, with the empty TriggeredBy= systemd prints too
    print("\n\n".join(f"Id={u}\nTriggeredBy={' '.join(triggers.get(u, []))}" for u in names))
