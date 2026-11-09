#!/usr/bin/env python3
"""Stand-in for `firewall-cmd` in tests: records every call and keeps the zones as it changes them.

Environment:
  FAKE_DIR   calls are recorded in calls.jsonl as {"cmd": "firewall-cmd", "argv": [...]}; the
             zones, once a call changed them, in firewall.json

scenario.json (the firewall before any change, permanent and running alike):
  firewall_zone   the default zone, what --get-default-zone prints (default "public")
  firewall_rules  rich rules of the default zone
  firewall_zones  other zones by name: {"ports": [...], "services": [...], "rich_rules": [...]};
                  without it the zone libvirt has Fedora's services (dhcp, dhcpv6, dns, ssh, tftp)
  firewall_down   every call fails as when firewalld does not run (exit 252)

A call does what its options say, in order. With --permanent it changes and asks the permanent
configuration, else the running firewall; --reload makes the running firewall the permanent one.
--query-* prints yes (exit 0) or no (exit 1); a zone that does not exist fails (exit 112).
"""
import copy
import fcntl
import json
import os
import sys
import tempfile

DIR = os.environ["FAKE_DIR"]
KINDS = {"rich-rule": "rich_rules", "port": "ports", "service": "services"}
FEDORA_LIBVIRT = {"services": ["dhcp", "dhcpv6", "dns", "ssh", "tftp"]}

with open(os.path.join(DIR, "calls.jsonl"), "a") as f:
    f.write(json.dumps({"cmd": "firewall-cmd", "argv": sys.argv[1:]}) + "\n")
try:
    with open(os.path.join(DIR, "scenario.json")) as f:
        scenario = json.load(f)
except FileNotFoundError:
    scenario = {}
if scenario.get("firewall_down"):
    print("FirewallD is not running", file=sys.stderr)
    sys.exit(252)


def initial():
    default = scenario.get("firewall_zone", "public")
    zones = {"libvirt": FEDORA_LIBVIRT, default: {"rich_rules": scenario.get("firewall_rules", [])},
             **scenario.get("firewall_zones", {})}
    zones = {name: {key: list(zone.get(key, [])) for key in KINDS.values()} for name, zone in zones.items()}
    return {"default": default, "permanent": zones, "runtime": copy.deepcopy(zones)}


def save(state):
    fd, tmp = tempfile.mkstemp(dir=DIR, prefix=".firewall.")
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)
    os.replace(tmp, os.path.join(DIR, "firewall.json"))


with open(os.path.join(DIR, "firewall.lock"), "a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        with open(os.path.join(DIR, "firewall.json")) as f:
            state = json.load(f)
    except FileNotFoundError:
        state = initial()
    side = state["permanent" if "--permanent" in sys.argv else "runtime"]
    zone = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--zone=")), state["default"])
    changed = False
    for arg in sys.argv[1:]:
        if arg == "--get-default-zone":
            print(state["default"])
        elif arg == "--reload":
            state["runtime"] = copy.deepcopy(state["permanent"])
            changed = True
        elif arg.startswith("--") and "=" in arg and not arg.startswith("--zone="):
            verb, _, kind = arg[2:].split("=", 1)[0].partition("-")
            value = arg.split("=", 1)[1]
            if zone not in side:
                print(f"Error: INVALID_ZONE: {zone}", file=sys.stderr)
                sys.exit(112)
            items = side[zone][KINDS[kind]]
            if verb == "query":
                print("yes" if value in items else "no")
                sys.exit(0 if value in items else 1)
            if verb == "add" and value not in items:
                items.append(value)
            elif verb == "remove" and value in items:
                items.remove(value)
            changed = True
    if changed:
        save(state)
