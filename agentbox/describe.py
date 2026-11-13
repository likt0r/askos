"""What the Control Room shows about a VM (#13): `agentbox describe [<vm>] [--json]`.

One object per VM, shared with `agentbox watch` (watch.py), which sends the same
objects whenever one changes: the registry record, the state, agents, whether the VM
answers and its failed wakes, the System disk's age and the Build it was created from,
the Image with its pin and current Build, Open web, the Claude token's age and the
devices connected to the VM's Orca port. Memory in use is not part of it; the Control
Room reads it without privileges from the VM's QEMU process (host readings).

A VM libvirt reports as running is `starting`, `running` or `not_responding`
(responding.py, #54): describe asks its Orca once, with short time limits, and takes what
the wakes and watch found in the VM's start from its wakes file, so it judges as watch does.

`status` stays as it is (its text and JSON are older contracts).
"""
import json
import os
import re
import subprocess
import sys
import time

from . import images, lifecycle, paths, registry, responding, settings, state_files, times, virsh, vmenv
from .errors import die

# virsh domstate -> state in the Control Room; "paused" there is a Pause (managed save)
STATES = {"running": "running", "shut off": "stopped", "paused": "suspended", "in shutdown": "shutting_down",
          "crashed": "crashed", "pmsuspended": "suspended", "idle": "running", "blocked": "running"}
# A connection to a VM's Orca port is an instance of agentbox-gui-<vm>@.service, named by
# systemd after the socket: <n>-[<cookie>-]<local address>:<port>-<remote address>:<port>
GUI_INSTANCE = re.compile(r"agentbox-gui-(" + registry.NAME.pattern + r")@(.+)\.service")
# States of an Orca connection's unit that count as connected. A deactivating one is being
# closed, so it is left out; images._active counts it, because a Build stopping still runs.
ACTIVE = ("active", "activating", "reloading")


def iso(epoch):
    """times.iso, with None for 0 as well (never)."""
    return times.iso(epoch) if epoch else None


def list_units(patterns):
    """[(unit, load, active, sub, description)] of the loaded units matching these
    patterns (systemctl list-units --all), or None when systemctl fails."""
    try:
        r = subprocess.run(["systemctl", "list-units", "--all", "--plain", "--no-legend", "--no-pager",
                            "--full", *patterns], capture_output=True, text=True, errors="replace",
                           timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    rows = []
    for line in r.stdout.splitlines():
        f = line.split(None, 4)
        if len(f) >= 4 and f[0].isprintable():
            rows.append((f[0], f[1], f[2], f[3], f[4].strip() if len(f) > 4 else ""))
    return rows


def devices(units):
    """{vm: [{"address", "connections"}]} of the open connections to each VM's Orca port,
    from the running agentbox-gui-<vm>@ instances; the remote address of each."""
    out = {}
    for unit, _, active, _, _ in units:
        m = GUI_INSTANCE.fullmatch(unit)
        if not m or active not in ACTIVE:
            continue
        # IPv4 and IPv6 addresses contain no "-": the last two parts are local and remote
        parts = m.group(2).split("-")
        if len(parts) < 3:
            continue
        address = parts[-1].rpartition(":")[0]
        if not address:
            continue
        per_vm = out.setdefault(m.group(1), {})
        per_vm[address] = per_vm.get(address, 0) + 1
    return {vm: [{"address": a, "connections": n} for a, n in sorted(found.items())] for vm, found in out.items()}


def triggered_by(names):
    """{unit: [the timers and sockets that start it]}, systemd's TriggeredBy of these units
    (systemctl show), or None when systemctl fails. A unit systemctl did not answer for is
    missing from it (unknown, not "none"). A short timeout: the poll waits for it."""
    if not names:
        return {}
    try:
        r = subprocess.run(["systemctl", "show", "--no-pager", "--property=Id,TriggeredBy", "--", *names],
                           capture_output=True, text=True, errors="replace", timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    found = {}
    for block in r.stdout.split("\n\n"):        # one block per unit, the properties in systemd's order
        props = dict(line.partition("=")[::2] for line in block.splitlines())
        if props.get("Id"):
            found[props["Id"]] = sorted(props.get("TriggeredBy", "").split())
    return found


class Triggers:
    """triggered_by(), asked again only when the list of units changed (or it failed): our
    timers and sockets match UNIT_PATTERNS, so one that comes or goes changes the list."""

    def __init__(self):
        self.names, self.found = None, None

    def __call__(self, names):
        if names != self.names or self.found is None:
            self.names, self.found = list(names), triggered_by(names)
        return self.found


def services(units, triggers=triggered_by):
    """The Host's agentbox units and Ollama, without the per-connection instances; each with
    `triggered_by`, the timers and sockets that start it (triggers(names), None: unknown)."""
    rows = []
    for unit, load, active, sub, description in units:
        if GUI_INSTANCE.fullmatch(unit) or re.fullmatch(r"agentbox-ghvm-.+@.+\.service", unit):
            continue
        rows.append({"unit": unit, "load": load, "active": active, "sub": sub, "description": description})
    rows.sort(key=lambda r: r["unit"])
    found = triggers([r["unit"] for r in rows])
    for r in rows:
        r["triggered_by"] = None if found is None else found.get(r["unit"])
    return rows


UNIT_PATTERNS = ("agentbox-*", "ollama.service")


def state_of(raw, paused):
    if paused:
        return "paused"
    return STATES.get(raw, "unknown")


class Backing:
    """The Build a System disk was created from, read again only when the disk changed."""

    def __init__(self):
        self.cache = {}

    def __call__(self, name):
        disk = lifecycle.system_disk(name)
        try:
            st = os.stat(disk)
        except OSError:
            return None, None
        key = (st.st_mtime_ns, st.st_size, st.st_ino)
        if self.cache.get(name, (None,))[0] != key:
            backing = images._backing(disk)
            self.cache[name] = (key, backing)
        return int(st.st_mtime), self.cache[name][1]


def vm(rec, units, observed, backing, now=None, current=None, starts=None):
    """The object for one VM. units: list_units() rows or None (devices unknown);
    observed(name): (agents, seen) of a VM libvirt reports as running, agents {state: count},
    {} without agents, None when unknown, seen the last look at it (responding.look; watch's
    with the start it looked at), None before one; backing: a Backing; current: {image:
    current Build}, starts: responding.starts() (both computed once per round when given)."""
    now = time.time() if now is None else now
    name = rec["name"]
    paused = lifecycle.has_save(name)
    raw = virsh.state(name)
    state = state_of(raw, paused)
    start = (starts if starts is not None else responding.starts()).get(name)
    if start is None and state == "running":
        start = responding.start(name)         # started after this round read the starts
    # what the wakes and watch found in this start; a VM that does not run shows what the
    # wakes of its last start found
    wakes = responding.wakes(name, start, any_start=start is None and state != "running")
    agents, answers = {}, None
    if state == "running":
        agents, seen = observed(name)
        if seen is not None and seen.get("start") not in (None, start and start.id):
            agents, seen = None, None          # watch's last look was at an earlier start
        state, answers = responding.judge(start and start.at, seen, wakes)
        if answers is not None:
            answers = {**answers, "since": iso(responding.wall(answers["since"]))}
    mtime, file = backing(name)
    m = images.DISK.fullmatch(file or "")
    image_build = (current or {}).get(rec["image"]) if current is not None else images.current(rec["image"])
    next_build = rec["build"] or image_build
    disk_build = m.group(2) if m else None
    open_until = state_files.open_until(name)
    token_since = state_files.claude_token_since(name)
    values = settings.load()
    found = devices(units).get(name, []) if units is not None else None
    cleanup = registry.cleanup(rec)
    return {
        "name": name,
        "state": state,
        "libvirt_state": None if raw == virsh.MISSING else raw,
        "network": rec["network"],
        "orca_port": rec["orca_port"],
        "address": registry.guest_ip(rec),
        "memory_mib": rec["memory_mib"],
        "data_disk_gib": rec["data_disk_gib"],
        "container_image_cleanup": cleanup[0],
        "container_image_cleanup_days": cleanup[1],
        "github_owners": list(rec["github_owners"]),
        # the VM variables in the record; the VM has them from its next start (not secret),
        # except those whose name is refused since they were set (never passed on)
        "env": dict(rec.get("env", {})),
        "env_refused": vmenv.refused(rec.get("env", {})),
        "created": rec["created"],
        "image": rec["image"],
        "build_pin": rec["build"],
        "image_build": image_build,
        "next_build": next_build,
        "system_disk": {
            "since": iso(mtime),
            "age_days": int((now - mtime) // 86400) if mtime else None,
            "file": file or None,
            "image": m.group(1) if m else None,
            "build": disk_build,
        },
        # the next Reset brings a different Build (a new one, another Image or a pin)
        "reset_changes_build": bool(next_build and m and (m.group(1), disk_build) != (rec["image"], next_build)),
        # the daily Reset check would reset it (when idle): its System disk is old, or with
        # auto_update its Image has a newer Build it takes (#114)
        "reset_due": bool(mtime and ((now - mtime) // 86400 >= values["reset_age_days"]
                                     or values["auto_update"] and images.behind(rec, file, image_build))),
        "agents": None if agents is None else {k: int(v) for k, v in agents.items()},
        # what answers while libvirt reports the VM as running; None otherwise or before a look
        "responding": answers,
        # wakes that gave up since the VM's last start
        "failed_wakes": {"count": wakes["count"], "last": iso(wakes["last"]), "address": wakes["address"]},
        "open_web_until": iso(open_until),
        "claude_token_since": iso(token_since),
        "devices": found,
    }


def current_builds(records):
    return {image: images.current(image) for image in {r["image"] for r in records}}


def snapshot(records, problems, units, observed, backing, now=None):
    current = current_builds(records)
    starts = responding.starts()
    return {"vms": [vm(r, units, observed, backing, now, current, starts) for r in records],
            "problems": [{"vm": n, "problem": p} for n, p in problems]}


def look(name):
    """(agents, seen) of a running VM, asked now: whether its Orca answers (then its
    agents), else whether its guest agent does."""
    seen = responding.look(name)
    return (lifecycle.agent_states(name) if seen["orca"] else None), seen


def describe(args):
    """agentbox describe [<vm>] [--json]: every VM (or one) as the Control Room shows it."""
    as_json = "--json" in args
    rest = [a for a in args if a != "--json"]
    if len(rest) > 1 or any(a.startswith("-") for a in rest):
        die("describe [<vm>] [--json]", error="invalid_argument")
    if rest:
        records, problems = [registry.load(registry.check(rest[0]))], []
    else:
        records, problems = registry.scan()
    units = list_units(["agentbox-gui-*@*.service"])
    result = snapshot(records, problems, units, look, Backing())
    if as_json:
        print(json.dumps(result if not rest else result["vms"][0], ensure_ascii=False))
        return
    for v in result["vms"]:
        disk = v["system_disk"]
        agents = "?" if v["agents"] is None else " ".join(f"{k}={n}" for k, n in sorted(v["agents"].items()))
        devs = "?" if v["devices"] is None else ", ".join(f"{d['address']} ({d['connections']})"
                                                          for d in v["devices"])
        answers, wakes = v["responding"], v["failed_wakes"]
        print(f"{v['name']}: {v['state']}")
        if answers and v["state"] != "running":
            guest_agent = {True: "answers", False: "does not answer"}.get(answers["guest_agent"], "?")
            print("  Answers:      Orca does not answer" + (f" (since {answers['since']})" if answers["since"] else "")
                  + f", guest agent {guest_agent}")
        if wakes["count"]:
            print(f"  Wakes:        {wakes['count']} failed, last {wakes['last'] or '?'}"
                  + (f" ({wakes['address']})" if wakes["address"] else ""))
        print(f"  Image:        {v['image']}" + (f", pinned to {v['build_pin']}" if v["build_pin"] else "")
              + f" (current Build: {v['image_build'] or '-'})")
        print(f"  System disk:  {disk['file'] or '-'}"
              + (f", {disk['age_days']} days old" if disk["age_days"] is not None else "")
              + (", the next Reset brings " + v["next_build"] if v["reset_changes_build"] else ""))
        print(f"  Memory:       {v['memory_mib']} MiB, Data disk {v['data_disk_gib']} GiB")
        cleanup = (f"after {v['container_image_cleanup_days']} days" if v["container_image_cleanup"]
                   else f"off ({v['container_image_cleanup_days']} days)")
        print(f"  Container image cleanup: {cleanup}")
        if v["env"]:
            print(f"  VM variables: {', '.join(v['env'])} (agentbox env {v['name']})"
                  + (f"; {', '.join(v['env_refused'])} not passed on" if v["env_refused"] else ""))
        print(f"  Agents:       {agents or 'none'}")
        print(f"  Devices:      {devs or 'none'}")
    for p in result["problems"]:
        print(f"agentbox: {p['problem']}", file=sys.stderr)


COMMANDS = {"describe": describe}
