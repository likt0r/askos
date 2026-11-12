"""The VM registry: one root-owned record per VM in /etc/agentbox/vms/<name>.json.

Everything else about a VM (network, Orca port, units, disks, its libvirt
definition) is derived from its record by `agentbox regenerate`.
"""
import json
import os
import re
import sys

from . import paths, state_files
from .errors import Fail, die
from .sanitize import clean

# the same pattern guards the token service and the Control Room (a test keeps them equal)
NAME = re.compile(r"[a-z][a-z0-9-]{1,15}")
# Words that stand where a VM name may stand: `agentbox focus off`, "all" for
# Allowlist entries of every VM, the Control Room API paths next to /api/<vm>/...
# (today status, login, logout; then the areas of the new Control Room and its
# API versions v1, v2, ...) and "new" in the UI's /vms/<vm> routes. A VM keeps
# its name for good, so these are reserved before anything uses them.
RESERVED = {"off", "all", "new", "status", "login", "logout", "activity", "allowlist", "watchlist", "images",
            "settings", "system", "host", "events", "version", "vms", "github-app"}
API_VERSION = re.compile(r"v[0-9]+")
IMAGE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
ORCA_BASE = 6800                     # Orca port = ORCA_BASE + network number
# The Container image cleanup of a record: on or off, and its days. Both fields are
# optional: a record without them means the cleanup on and 7 days, and no code rewrites
# an existing record to add them (every reader uses these defaults).
CLEANUP_DAYS = (1, 365)
CLEANUP_DEFAULT = (True, 7)
FIELDS = {"name": str, "network": int, "orca_port": int, "image": str, "build": (str, type(None)),
          "memory_mib": int, "data_disk_gib": int, "github_owners": list, "created": str}


def record_file(name):
    return os.path.join(paths.REGISTRY, f"{name}.json")


def reserved(name):
    return name in RESERVED or bool(API_VERSION.fullmatch(name))


def problem(rec, name):
    """What is wrong with this record, or None."""
    if not isinstance(rec, dict):
        return "not a JSON object"
    for key, kind in FIELDS.items():
        if key not in rec or isinstance(rec[key], bool) or not isinstance(rec[key], kind):
            return f"field {key} is missing or has the wrong type"
    if rec["name"] != name:
        return "the name does not match the file name"
    if reserved(name):
        return f"the name {name} is reserved"
    if not 1 <= rec["network"] <= 254:
        return "network must be 1 to 254"
    if rec["orca_port"] != ORCA_BASE + rec["network"]:
        return f"orca_port must be {ORCA_BASE} + network"
    if not IMAGE.fullmatch(rec["image"]) or rec["build"] is not None and not IMAGE.fullmatch(rec["build"]):
        return "invalid image or build"
    if rec["memory_mib"] < 1024 or rec["data_disk_gib"] < 1:
        return "memory_mib or data_disk_gib too small"
    if not all(isinstance(o, str) for o in rec["github_owners"]):
        return "github_owners must be a list of names"
    if "container_image_cleanup" in rec and not isinstance(rec["container_image_cleanup"], bool):
        return "field container_image_cleanup has the wrong type"
    if "container_image_cleanup_days" in rec:
        days = rec["container_image_cleanup_days"]
        if isinstance(days, bool) or not isinstance(days, int):
            return "field container_image_cleanup_days has the wrong type"
        if not CLEANUP_DAYS[0] <= days <= CLEANUP_DAYS[1]:
            return f"container_image_cleanup_days must be {CLEANUP_DAYS[0]} to {CLEANUP_DAYS[1]}"
    return None


def load(name):
    """The record of this VM; dies if the name is unknown or the record broken."""
    if not name:
        die("no VM given", error="vm_not_found")
    if not NAME.fullmatch(name):
        die("No such VM: " + clean(name, False)[:40], error="vm_not_found", vm=clean(name, False)[:40])
    f = record_file(name)
    try:
        with open(f, encoding="utf-8") as fh:
            rec = json.load(fh)
    except FileNotFoundError:
        die(f"No such VM: {name}", error="vm_not_found", vm=name)
    except OSError as e:
        die(f"{f}: {e.strerror}", error="action_failed", vm=name)
    except ValueError:
        rec = None
    why = problem(rec, name)
    if why:
        die(f"{f}: broken record ({why})", error="action_failed", vm=name)
    return rec


def scan():
    """The usable records ordered by network number, and (name, problem) for
    the others: broken records and VMs sharing a network."""
    try:
        files = sorted(os.listdir(paths.REGISTRY))
    except FileNotFoundError:
        return [], []
    except OSError as e:
        die(f"{paths.REGISTRY}: {e.strerror}", error="action_failed")
    records, problems = [], []
    for name in (f[:-5] for f in files if f.endswith(".json") and NAME.fullmatch(f[:-5])):
        try:
            records.append(load(name))
        except Fail as f:
            problems.append((name, f.message))
    by_network = {}
    for r in records:
        by_network.setdefault(r["network"], []).append(r["name"])
    for network, shared in by_network.items():
        if len(shared) > 1:
            problems += [(n, f"network {network} given twice: {' and '.join(shared)}") for n in shared]
    records = sorted((r for r in records if len(by_network[r["network"]]) == 1), key=lambda r: r["network"])
    return records, problems


def all_records():
    """Every record ordered by network number. Dies on any problem, so nothing
    generates configuration from half a registry."""
    records, problems = scan()
    if problems:
        die(problems[0][1], error="action_failed", vm=problems[0][0])
    return records


def names():
    """The usable VMs; problems with the others go to stderr, so status and
    the like still show what works."""
    records, problems = scan()
    for _, problem in problems:
        print(f"agentbox: {problem}", file=sys.stderr, flush=True)
    return [r["name"] for r in records]


def save(rec):
    """Write a record (callers changing one hold state_files.locked(record_file(name)))."""
    why = problem(rec, rec.get("name"))
    if why:
        die(f"record for {rec.get('name')} invalid ({why})", error="action_failed")
    if not os.path.isdir(paths.REGISTRY):
        os.makedirs(paths.REGISTRY)
        os.chmod(paths.REGISTRY, 0o755)          # the token service reads it as agentbox-gh
    state_files.write_atomic(record_file(rec["name"]), (json.dumps(rec, indent=2) + "\n").encode(), 0o644)


def check(name):
    load(name)
    return name


def cleanup(rec):
    """(on, days) of the VM's Container image cleanup: the record's two fields, else the
    defaults of a record without them (on, 7 days)."""
    return (rec.get("container_image_cleanup", CLEANUP_DEFAULT[0]),
            rec.get("container_image_cleanup_days", CLEANUP_DEFAULT[1]))


def dom(name):
    return f"agentbox-{name}"


def host_ip(rec):
    return f"10.77.{rec['network']}.1"


def guest_ip(rec):
    return f"10.77.{rec['network']}.2"


def bridge(rec):
    """Linux limits interface names to 15 characters; longer names use the
    network number (names start with a letter, so the two never collide)."""
    return f"abox-{rec['name']}" if len(rec["name"]) <= 10 else f"abox-{rec['network']}"


def mac(rec):
    return "52:54:00:77:%02x:02" % rec["network"]


def orca_address(name):
    """Where Orca in the VM listens: (VM address, Orca port)."""
    rec = load(name)
    return guest_ip(rec), rec["orca_port"]
