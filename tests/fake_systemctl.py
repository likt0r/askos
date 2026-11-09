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
"""
import fcntl
import fnmatch
import json
import os
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
if sys.argv[1] == "is-active":
    active = sys.argv[-1] in scenario.get("systemctl_active", [])
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
