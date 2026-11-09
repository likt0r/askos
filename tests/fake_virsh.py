#!/usr/bin/env python3
"""Stand-in for `virsh` in tests: the VMs and their guest agent, driven by a scenario file.

Environment:
  FAKE_VIRSH_DIR   directory holding scenario.json; calls are recorded in calls.jsonl,
                   the VMs' changing state in domains.json

scenario.json:
  state       domstate of every VM before any change (default "running")
  vms         per VM (by name, e.g. "work"), overriding the defaults:
                state         domstate before any change
                managed_save  a Pause is saved (default false)
                rss           KiB for `dommemstat` while running (default 2097152)
                shutdown      "stops" (default) or "ignored": the guest ignores the ACPI request
                comes_back    the VM is running again right after every shutdown and destroy,
                              as if something else started it (default false)
                start_fails   `start` fails with libvirt's error (default false); "active":
                              with "domain is already active"
                pinned_backing  the backing file the VM runs on after `start` (default: the
                              one in its System disk's header then, as libvirt fixes it)
                defined       the domain exists in libvirt (default true)
                uuid          its UUID (default: derived from the name)
  networks    libvirt networks that exist before any change, by name:
              {"agentbox-work": {"active": true, "autostart": true, "xml": "<network>..."}}
              (default none)
  pools       storage pools that exist before any change, by name:
              {"vms": {"path": "/var/lib/agentbox/vms", "active": true, "autostart": true}}
  agent_down  guest agent never answers (default false)
  agent_hangs seconds every guest-agent call blocks before answering (default 0)
  responses   one per guest-exec call, the last one repeats:
              {"entries": [...]} or {"stdout": "..."}, plus optional
              "exitcode" (0), "stderr" (""), "truncated" (false),
              "signal" (killed by a signal; replaces exitcode)

`dumpxml` shows the System disk (under AGENTBOX_ROOT) and, while running, the
backing file fixed at `start`. `start` adds the VM's QEMU process, started now, with
libvirt's pid file to the /proc and /run under AGENTBOX_ROOT (fake_proc.py); a stop
removes both. The guest agent answers
`guest-ping` unless agent_down.

Calls run at the same time (watch, wakes, a Restart): one call at a time reads and
changes the VMs, networks and pools (changing), and their files are replaced whole
(save), so no call reads half a file or undoes another's change. A test that changes
them while virsh may be called does the same (with changing(dir) and save(name, data, dir)).

Recorded: guest-exec, guest-ping and the commands that change a VM (start, shutdown,
destroy, managedsave, managedsave-remove), each with the domain; define,
net-define and pool-define with the XML they were given; net-autostart,
net-start, net-destroy, net-undefine, pool-autostart and pool-start; undefine
with its flags.
"""
import base64
import contextlib
import fcntl
import json
import os
import re
import sys
import tempfile
import time
import uuid
from xml.sax.saxutils import quoteattr

import fake_proc

DIR = os.environ.get("FAKE_VIRSH_DIR", "")      # unset where a test imports this module
ROOT = os.environ.get("AGENTBOX_ROOT", "")


def process_started(domain):
    if ROOT:
        fake_proc.qemu(ROOT, domain.removeprefix("agentbox-"), time.time())


def process_gone(domain):
    if ROOT:
        fake_proc.gone(ROOT, domain.removeprefix("agentbox-"))


def load(name, default):
    try:
        with open(os.path.join(DIR, name)) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


@contextlib.contextmanager
def changing(directory=None):
    """One call at a time reads and changes the VMs, networks and pools, as libvirt does:
    a call that changes one writes back what it read (dominfo too), so two at once could
    undo a change."""
    with open(os.path.join(directory or DIR, "virsh.lock"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def save(name, data, directory=None):
    """Replace a state file whole: a call that reads it meanwhile gets the old or the new
    one, not an empty or half-written file."""
    d = directory or DIR
    fd, tmp = tempfile.mkstemp(dir=d, prefix=f".{name}.")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, os.path.join(d, name))


def record(call):
    with open(os.path.join(DIR, "calls.jsonl"), "a") as f:
        f.write(json.dumps(call) + "\n")


def vm_config(scenario, domain):
    return (scenario.get("vms") or {}).get(domain.removeprefix("agentbox-"), {})


def domain_state(scenario, domain):
    """The VM's current state: what changed so far, else the scenario."""
    conf = vm_config(scenario, domain)
    initial = {"state": conf.get("state", scenario.get("state", "running")),
               "managed_save": conf.get("managed_save", False), "defined": conf.get("defined", True),
               "uuid": conf.get("uuid", str(uuid.uuid5(uuid.NAMESPACE_DNS, domain)))}
    return {**initial, **load("domains.json", {}).get(domain, {})}


def no_domain(domain):
    print(f"error: failed to get domain '{domain}'", file=sys.stderr)
    return 1


def define(scenario, path):
    with open(path) as f:
        xml = f.read()
    domain = re.search(r"<name>([^<]+)</name>", xml).group(1)
    record({"cmd": "define", "dom": domain, "xml": xml})
    st = domain_state(scenario, domain)
    if not st["defined"]:
        st["state"] = "shut off"
    m = re.search(r"<uuid>([^<]+)</uuid>", xml)
    save_state(domain, **{**st, "defined": True, "uuid": m.group(1) if m else st["uuid"]})
    print(f"Domain '{domain}' defined from {path}")
    return 0


def networks(scenario):
    current = load("networks.json", None)
    return current if current is not None else dict(scenario.get("networks") or {})


def network(scenario, cmd, arg):
    """net-info, net-dumpxml, net-define, net-autostart, net-start."""
    nets = networks(scenario)
    if cmd == "net-define":
        with open(arg) as f:
            xml = f.read()
        name = re.search(r"<name>([^<]+)</name>", xml).group(1)
        record({"cmd": cmd, "net": name, "xml": xml})
        nets[name] = {"active": False, "autostart": False, **nets.get(name, {}), "xml": xml}
        print(f"Network {name} defined from {arg}")
    elif arg not in nets:
        print(f"error: failed to get network '{arg}'", file=sys.stderr)
        print(f"error: Network not found: no network with matching name '{arg}'", file=sys.stderr)
        return 1
    elif cmd == "net-dumpxml":
        print(nets[arg].get("xml", f"<network>\n  <name>{arg}</name>\n</network>"), end="")
        return 0
    elif cmd == "net-info":
        n = nets[arg]
        yes = lambda v: "yes" if v else "no"
        print(f"Name:           {arg}\nUUID:           {uuid.uuid5(uuid.NAMESPACE_DNS, arg)}\n"
              f"Active:         {yes(n['active'])}\nPersistent:     yes\nAutostart:      {yes(n['autostart'])}\n"
              f"Bridge:         abox")
        return 0
    elif cmd == "net-autostart":
        record({"cmd": cmd, "net": arg})
        nets[arg]["autostart"] = True
        print(f"Network {arg} marked as autostarted")
    elif cmd == "net-start":
        record({"cmd": cmd, "net": arg})
        if nets[arg]["active"]:
            print(f"error: Failed to start network {arg}\nerror: Requested operation is not valid: "
                  "network is already active", file=sys.stderr)
            return 1
        nets[arg]["active"] = True
        print(f"Network {arg} started")
    elif cmd == "net-destroy":
        record({"cmd": cmd, "net": arg})
        if not nets[arg]["active"]:
            print(f"error: Failed to destroy network {arg}\nerror: Requested operation is not valid: "
                  "network is not active", file=sys.stderr)
            return 1
        nets[arg]["active"] = False
        print(f"Network {arg} destroyed")
    elif cmd == "net-undefine":
        record({"cmd": cmd, "net": arg})
        del nets[arg]
        print(f"Network {arg} has been undefined")
    save("networks.json", nets)
    return 0


def save_state(domain, **changes):
    current = load("domains.json", {})
    current[domain] = {**current.get(domain, {}), **changes}
    save("domains.json", current)


def not_running(cmd, domain):
    print(f"error: Failed to {cmd} domain '{domain}'", file=sys.stderr)
    print("error: Requested operation is not valid: domain is not running", file=sys.stderr)
    return 1


def system_disk(domain):
    return os.path.join(os.environ.get("AGENTBOX_ROOT", ""), "var/lib/agentbox/vms",
                        domain.removeprefix("agentbox-") + "-system.qcow2")


def header_backing(domain):
    """The backing file in the System disk's header (a fake disk is JSON, see fake_qemu_img)."""
    try:
        with open(system_disk(domain)) as f:
            return json.load(f).get("backing-filename")
    except (OSError, ValueError, AttributeError):
        return None


def dumpxml(scenario, domain):
    st = domain_state(scenario, domain)
    if not st["defined"]:
        return no_domain(domain)
    backing = st.get("backing") if st["state"] == "running" else None
    chain = (f"      <backingStore type='file' index='3'>\n        <format type='qcow2'/>\n"
             f"        <source file={quoteattr(backing)}/>\n        <backingStore/>\n      </backingStore>\n"
             if backing else "      <backingStore/>\n")
    print(f"<domain type='kvm'>\n  <name>{domain}</name>\n  <devices>\n"
          f"    <disk type='file' device='disk'>\n      <driver name='qemu' type='qcow2'/>\n"
          f"      <source file={quoteattr(system_disk(domain))} index='2'/>\n{chain}"
          f"      <target dev='vda' bus='virtio'/>\n    </disk>\n  </devices>\n</domain>")
    return 0


def lifecycle(scenario, cmd, domain):
    """start, shutdown, destroy, managedsave, managedsave-remove, dominfo, dommemstat."""
    conf = vm_config(scenario, domain)
    st = domain_state(scenario, domain)
    if not st["defined"]:
        return no_domain(domain)
    save_state(domain, **st)
    running = st["state"] == "running"
    if cmd not in ("dominfo", "dommemstat"):
        record({"cmd": cmd, "dom": domain})

    if cmd == "dominfo":
        print(f"Id:             {'1' if running else '-'}\nName:           {domain}\n"
              f"UUID:           00000000-0000-0000-0000-000000000000\nOS Type:        hvm\n"
              f"State:          {st['state']}\nCPU(s):         4\nMax memory:     8388608 KiB\n"
              f"Used memory:    8388608 KiB\nPersistent:     yes\nAutostart:      disable\n"
              f"Managed save:   {'yes' if st['managed_save'] else 'no'}\nSecurity model: selinux\n"
              f"Security DOI:   0")
        return 0
    if cmd == "dommemstat":
        if not running:
            print("error: Failed to get memory statistics for domain " + domain, file=sys.stderr)
            print("error: Requested operation is not valid: domain is not running", file=sys.stderr)
            return 1
        print(f"actual 8388608\nswap_in 0\nswap_out 0\nunused 4194304\navailable 8147096\n"
              f"rss {conf.get('rss', 2097152)}")
        return 0
    if cmd == "start":
        if conf.get("start_fails") == "active":
            print(f"error: Failed to start domain '{domain}'", file=sys.stderr)
            print("error: Requested operation is not valid: domain is already active", file=sys.stderr)
            return 1
        if conf.get("start_fails"):
            print(f"error: Failed to start domain '{domain}'", file=sys.stderr)
            print("error: Cannot access storage file '/var/lib/agentbox/vms/x.qcow2': No such file or directory",
                  file=sys.stderr)
            return 1
        if running:
            print("error: Domain is already active", file=sys.stderr)
            return 1
        process_started(domain)              # libvirt reports a domain running once QEMU runs
        save_state(domain, state="running", managed_save=False,
                   backing=conf.get("pinned_backing") or header_backing(domain))
        print(f"Domain '{domain}' started")
        return 0
    if cmd == "managedsave-remove":
        if st["managed_save"]:
            save_state(domain, managed_save=False)
            print(f"Removed managedsave image for domain '{domain}'")
        else:
            print(f"Domain '{domain}' has no manage save image; removal skipped")
        return 0
    if not running:
        return not_running(cmd, domain)
    if cmd == "shutdown":
        if conf.get("shutdown", "stops") == "stops" and not conf.get("comes_back"):
            process_gone(domain)              # libvirt reports it off once QEMU has ended
            save_state(domain, state="shut off")
        print(f"Domain '{domain}' is being shutdown")
    elif cmd == "destroy":
        if not conf.get("comes_back"):
            process_gone(domain)
            save_state(domain, state="shut off")
        print(f"Domain '{domain}' destroyed")
    elif cmd == "managedsave":
        process_gone(domain)
        save_state(domain, state="shut off", managed_save=True)
        print(f"\nDomain '{domain}' state saved by libvirt")
    return 0


def undefine(scenario, rest):
    """undefine <domain> [--managed-save]: libvirt refuses while a Pause is saved,
    unless --managed-save removes it as well."""
    domain = rest[0]
    st = domain_state(scenario, domain)
    if not st["defined"]:
        return no_domain(domain)
    record({"cmd": "undefine", "dom": domain, "flags": rest[1:]})
    if st["managed_save"] and "--managed-save" not in rest[1:]:
        print(f"error: Failed to undefine domain '{domain}'\nerror: Requested operation is not valid: "
              "Refusing to undefine while domain managed save image exists", file=sys.stderr)
        return 1
    save_state(domain, defined=False, managed_save=False, state="shut off")
    print(f"Domain '{domain}' has been undefined")
    return 0


def pools(scenario):
    current = load("pools.json", None)
    return current if current is not None else dict(scenario.get("pools") or {})


def pool(scenario, cmd, rest):
    """pool-list --all --name, pool-dumpxml, pool-define, pool-autostart, pool-start."""
    ps = pools(scenario)
    if cmd == "pool-list":
        print("".join(n + "\n" for n in ps))
        return 0
    if cmd == "pool-define":
        with open(rest[0]) as f:
            xml = f.read()
        name = re.search(r"<name>([^<]+)</name>", xml).group(1)
        record({"cmd": cmd, "pool": name, "xml": xml})
        ps[name] = {"path": re.search(r"<path>([^<]+)</path>", xml).group(1), "active": False, "autostart": False}
    elif rest[0] not in ps:
        print(f"error: failed to get pool '{rest[0]}'", file=sys.stderr)
        return 1
    elif cmd == "pool-dumpxml":
        p = ps[rest[0]]
        print(f"<pool type='dir'>\n  <name>{rest[0]}</name>\n  <target>\n    <path>{p['path']}</path>\n"
              f"  </target>\n</pool>")
        return 0
    elif cmd == "pool-info":
        yes = lambda v: "yes" if v else "no"
        p = ps[rest[0]]
        print(f"Name:           {rest[0]}\nState:          {'running' if p['active'] else 'inactive'}\n"
              f"Persistent:     yes\nAutostart:      {yes(p['autostart'])}")
        return 0
    elif cmd == "pool-autostart":
        record({"cmd": cmd, "pool": rest[0]})
        ps[rest[0]]["autostart"] = True
    elif cmd == "pool-start":
        record({"cmd": cmd, "pool": rest[0]})
        ps[rest[0]]["active"] = True
    save("pools.json", ps)
    return 0


def main(args):
    while args and args[0] in ("-q", "-c"):
        args = args[2:] if args[0] == "-c" else args[1:]
    cmd, rest = args[0], args[1:]
    scenario = load("scenario.json", {})
    if cmd == "qemu-agent-command":
        return guest_agent(scenario, rest)       # may hang (agent_hangs): not one at a time
    with changing():
        return libvirt(scenario, cmd, rest)


def libvirt(scenario, cmd, rest):
    if cmd in ("domstate", "domuuid"):
        st = domain_state(scenario, rest[0])
        if not st["defined"]:
            return no_domain(rest[0])
        print(st["state"] if cmd == "domstate" else st["uuid"])
        return 0
    if cmd == "define":
        return define(scenario, rest[0])
    if cmd == "dumpxml":
        return dumpxml(scenario, rest[0])
    if cmd in ("net-info", "net-dumpxml", "net-define", "net-autostart", "net-start", "net-destroy",
               "net-undefine"):
        return network(scenario, cmd, rest[0])
    if cmd == "undefine":
        return undefine(scenario, rest)
    if cmd.startswith("pool-"):
        return pool(scenario, cmd, rest)
    if cmd in ("start", "shutdown", "destroy", "managedsave", "managedsave-remove", "dominfo", "dommemstat"):
        return lifecycle(scenario, cmd, rest[0])
    return 1


def guest_agent(scenario, rest):
    request = json.loads(rest[1])
    if request["execute"] == "guest-ping":
        # "domain", not "dom": not a change to the VM
        record({"cmd": "guest-ping", "domain": rest[0], "argv": rest[2:]})
    time.sleep(scenario.get("agent_hangs", 0))
    if scenario.get("agent_down"):
        print("error: Guest agent is not responding: QEMU guest agent is not connected", file=sys.stderr)
        return 1
    if request["execute"] == "guest-ping":
        print(json.dumps({"return": {}}))
        return 0
    if request["execute"] == "guest-exec":
        calls = [c for c in load_calls() if c["cmd"] == "guest-exec"]
        record({"cmd": "guest-exec", "path": request["arguments"]["path"], "arg": request["arguments"]["arg"]})
        print(json.dumps({"return": {"pid": 4000 + len(calls)}}))
        return 0
    if request["execute"] == "guest-exec-status":
        n = request["arguments"]["pid"] - 4000
        responses = scenario.get("responses") or [{"entries": []}]
        r = responses[min(n, len(responses) - 1)]
        out = r["stdout"] if "stdout" in r else "".join(json.dumps(e) + "\n" for e in r.get("entries", []))
        status = {"signal": r["signal"]} if "signal" in r else {"exitcode": r.get("exitcode", 0)}
        print(json.dumps({"return": {
            "exited": True,
            **status,
            "out-data": base64.b64encode(out.encode()).decode(),
            "err-data": base64.b64encode(r.get("stderr", "").encode()).decode(),
            "out-truncated": r.get("truncated", False),
        }}))
        return 0
    return 1


def load_calls():
    try:
        with open(os.path.join(DIR, "calls.jsonl")) as f:
            return [json.loads(line) for line in f]
    except FileNotFoundError:
        return []


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
