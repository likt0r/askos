#!/usr/bin/env python3
"""Stand-in for `virsh` in tests: the VMs and their guest agent, driven by a scenario file.

Environment:
  FAKE_VIRSH_DIR   directory holding scenario.json; calls are recorded in calls.jsonl,
                   the VMs' changing state in domains.json
  FAKE_VIRSH_HOLD  "<command>:<directory>", set for one program (a wake, a Restart, watch):
                   each of its calls of that command takes the next number n (from 1, the
                   file <directory>/<n>) and goes on once <directory>/<n>.go or
                   <directory>/go exists. So a test sees that the program has come to that
                   call and lets it go on when something else has happened, however long
                   either takes.

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
                start_waits   `start` of the stopped VM waits until another call asks for
                              its state (domstate), and that call's answer waits for the
                              start: whoever polls the VMs sees the start as it happens,
                              not a poll later (default false; only where something polls)
                pinned_backing  the backing file the VM runs on after `start` (default: the
                              one in its System disk's header then, as libvirt fixes it)
                defined       the domain exists in libvirt (default true)
                disks         disks attached before any change, by target:
                              {"vdc": {"source": "/x.tar", "serial": "agentbox-inbox"}} (default none)
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
              Without responses, guest-exec of agentbox-inbox runs the real wrapper of
              vm/files in the VM's own root, FAKE_VIRSH_DIR/guest-<vm> (its ~/code is
              var/home/agent/code there), with the disk attached with the serial
              agentbox-inbox as its device; guest-exec of agentbox-container-images runs
              the real wrapper there too, with the agent's Podman a fake one
              (fake_guest_podman.py, its Container images in podman.json of that root).
  still_running  guest-exec-status never says the program ended (default false)
  missing_wrappers  paths guest-exec cannot run: the guest agent fails as qemu-ga does
              for a program the Image lacks
  attach_fails, detach_fails  attach-disk or detach-disk fails with libvirt's error
  attach_signals_parent  a successful attach-disk sends SIGTERM to the program that called virsh

`attach-disk <domain> <file> <target> ... --serial S` and `detach-disk <domain> <target>`
change a running VM's disks (domains.json), recorded with their arguments; a copy of each
attached file is kept in FAKE_VIRSH_DIR/attached/, so a test sees what the VM got.

`domblklist <domain>` lists the targets and sources of the System disk and the attached disks.

`dumpxml` shows the System disk (under AGENTBOX_ROOT) and, while running, the
backing file fixed at `start`. `start` adds the VM's QEMU process, started now, with
libvirt's pid file to the /proc and /run under AGENTBOX_ROOT (fake_proc.py); a stop
removes both. The guest agent answers
`guest-ping` unless agent_down.

Calls run at the same time (watch, wakes, a Restart): one call at a time reads and
changes the VMs, networks and pools (changing), and their files are replaced whole
(save), so no call reads half a file or undoes another's change. A test that changes
them while virsh may be called does the same (with changing(dir) and save(name, data, dir)).

Recorded: guest-exec, guest-ping (with the time it was asked; a held one when it goes on),
attach-disk, detach-disk and the commands that change a VM (start, shutdown, destroy, managedsave,
managedsave-remove), each with the domain; define,
net-define and pool-define with the XML they were given; net-autostart,
net-start, net-destroy, net-undefine, pool-autostart and pool-start; undefine
with its flags.
"""
import contextlib
import fcntl
import json
import os
import re
import sys
import time

import fake_proc

# watch asks virsh several times per poll, and a test waits for what it finds: the modules
# only some commands need (xml.sax.saxutils alone takes longer to import than all the rest)
# are imported where they are used

DIR = os.environ.get("FAKE_VIRSH_DIR", "")      # unset where a test imports this module
ROOT = os.environ.get("AGENTBOX_ROOT", "")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INBOX = "/usr/libexec/qemu-ga/fsfreeze-hook.d/agentbox-inbox"
INBOX_DEVICE = "/dev/disk/by-id/virtio-agentbox-inbox"
CONTAINER_IMAGES = "/usr/libexec/qemu-ga/fsfreeze-hook.d/agentbox-container-images"


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
    tmp = os.path.join(d, f".{name}.{os.getpid()}")
    with open(tmp, "w") as f:
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
               "disks": conf.get("disks") or {}}
    st = {**initial, **load("domains.json", {}).get(domain, {})}
    if "uuid" not in st:
        st["uuid"] = conf.get("uuid") or derived_uuid(domain)
    return st


def derived_uuid(name):
    import uuid
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, name))


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


def net_uuid(net, name):
    m = re.search(r"<uuid>([^<]+)</uuid>", net.get("xml", ""))
    return net.get("uuid") or (m.group(1) if m else derived_uuid(name))


def network(scenario, cmd, arg):
    """net-info, net-dumpxml, net-define, net-autostart, net-start."""
    nets = networks(scenario)
    if cmd == "net-define":
        with open(arg) as f:
            xml = f.read()
        name = re.search(r"<name>([^<]+)</name>", xml).group(1)
        record({"cmd": cmd, "net": name, "xml": xml})
        given = re.search(r"<uuid>([^<]+)</uuid>", xml)
        if name in nets:  # like libvirt: an existing name keeps its UUID
            have = net_uuid(nets[name], name)
            if not given or given.group(1) != have:
                print(f"error: Failed to define network from {arg}", file=sys.stderr)
                print(f"error: operation failed: network '{name}' already exists with uuid {have}",
                      file=sys.stderr)
                return 1
        nets[name] = {"active": False, "autostart": False, **nets.get(name, {}), "xml": xml,
                      "uuid": given.group(1) if given else net_uuid(nets.get(name, {}), name)}
        print(f"Network {name} defined from {arg}")
    elif arg not in nets:
        print(f"error: failed to get network '{arg}'", file=sys.stderr)
        print(f"error: Network not found: no network with matching name '{arg}'", file=sys.stderr)
        return 1
    elif cmd == "net-dumpxml":
        xml = nets[arg].get("xml", f"<network>\n  <name>{arg}</name>\n</network>")
        if "<uuid>" not in xml:  # libvirt always dumps the UUID
            xml = xml.replace("</name>", f"</name>\n  <uuid>{net_uuid(nets[arg], arg)}</uuid>", 1)
        print(xml, end="")
        return 0
    elif cmd == "net-info":
        n = nets[arg]
        yes = lambda v: "yes" if v else "no"
        print(f"Name:           {arg}\nUUID:           {net_uuid(n, arg)}\n"
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
    from xml.sax.saxutils import quoteattr
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


def hold(cmd):
    """FAKE_VIRSH_HOLD: a call of its command takes the next number and waits for its turn."""
    command, _, directory = os.environ.get("FAKE_VIRSH_HOLD", "").partition(":")
    if command != cmd:
        return
    n = 1
    while True:
        try:
            os.close(os.open(os.path.join(directory, str(n)), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            n += 1
    # (a directory that is gone: the test is over)
    while os.path.isdir(directory) and not any(os.path.exists(os.path.join(directory, go))
                                               for go in (f"{n}.go", "go")):
        time.sleep(0.01)


def meeting(scenario, cmd, domain):
    """start_waits: `start` and a `domstate` of the stopped VM meet. The start leaves a
    note that it waits, a domstate one that it asks; under the lock of changing(), the start
    goes ahead once someone asks, and who asks answers only when no start waits. So the
    start is over before the answer, and nothing but the answer lies between the two."""
    waits = os.path.join(DIR, f"start-waits-{domain}")
    asks = os.path.join(DIR, f"asks-{domain}.{os.getpid()}")
    if cmd == "domstate":
        open(asks, "w").close()
    while True:
        with changing():
            if cmd == "domstate" and not os.path.exists(waits):
                os.unlink(asks)
                return libvirt(scenario, cmd, [domain])
            if cmd == "start":
                asked = any(f.startswith(f"asks-{domain}.") for f in os.listdir(DIR))
                if asked or domain_state(scenario, domain)["state"] == "running":
                    if os.path.exists(waits):
                        os.unlink(waits)
                    return libvirt(scenario, cmd, [domain])
                open(waits, "w").close()
        time.sleep(0.005)


def main(args):
    while args and args[0] in ("-q", "-c"):
        args = args[2:] if args[0] == "-c" else args[1:]
    cmd, rest = args[0], args[1:]
    scenario = load("scenario.json", {})
    hold(cmd)
    if cmd == "qemu-agent-command":
        return guest_agent(scenario, rest)       # may hang (agent_hangs): not one at a time
    if cmd in ("start", "domstate") and vm_config(scenario, rest[0]).get("start_waits"):
        return meeting(scenario, cmd, rest[0])
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
    if cmd in ("attach-disk", "detach-disk"):
        return disks(scenario, cmd, rest)
    if cmd == "domblklist":
        st = domain_state(scenario, rest[0])
        if not st["defined"]:
            return no_domain(rest[0])
        print(f" vda      {system_disk(rest[0])}")
        for target, disk in (st.get("disks") or {}).items():
            print(f" {target}      {disk['source']}")
        return 0
    return 1


def disks(scenario, cmd, rest):
    """attach-disk <domain> <source> <target> [options], detach-disk <domain> <target> [options]:
    only live, on a running VM."""
    domain = rest[0]
    st = domain_state(scenario, domain)
    if not st["defined"]:
        return no_domain(domain)
    attached = dict(st.get("disks") or {})
    if cmd == "attach-disk":
        source, target, options = rest[1], rest[2], rest[3:]
        record({"cmd": cmd, "dom": domain, "source": source, "target": target, "options": options})
        if st["state"] != "running":
            return not_running("attach disk to", domain)
        if scenario.get("attach_fails"):
            print("error: Failed to attach disk\nerror: internal error: unable to execute QEMU command "
                  "'blockdev-add': Could not open '%s': Permission denied" % source, file=sys.stderr)
            return 1
        if target in attached or not os.path.isfile(source):
            print(f"error: Failed to attach disk\nerror: target {target} already exists" if target in attached
                  else f"error: Failed to attach disk\nerror: Cannot access storage file '{source}': "
                       "No such file or directory", file=sys.stderr)
            return 1
        import shutil
        serial = options[options.index("--serial") + 1] if "--serial" in options else None
        os.makedirs(os.path.join(DIR, "attached"), exist_ok=True)
        shutil.copyfile(source, os.path.join(DIR, "attached", os.path.basename(source)))
        attached[target] = {"source": source, "serial": serial}
        save_state(domain, disks=attached)
        print("Disk attached successfully")
        sys.stdout.flush()
        if scenario.get("attach_signals_parent"):
            import signal
            os.kill(os.getppid(), signal.SIGTERM)
            time.sleep(0.5)
        return 0
    target, options = rest[1], rest[2:]
    record({"cmd": cmd, "dom": domain, "target": target, "options": options})
    if scenario.get("detach_fails") or target not in attached:
        print(f"error: Failed to detach disk\nerror: No disk found whose source path or target is {target}",
              file=sys.stderr)
        return 1
    del attached[target]
    save_state(domain, disks=attached)
    print("Disk detached successfully")
    return 0


def guest_root(domain):
    return os.path.join(DIR, "guest-" + domain.removeprefix("agentbox-"))


def run_inbox(scenario, domain, argv):
    """The real agentbox-inbox in the VM's own root, its device the disk attached as agentbox-inbox."""
    import subprocess
    root = guest_root(domain)
    device = root + INBOX_DEVICE
    os.makedirs(os.path.dirname(device), exist_ok=True)
    os.makedirs(os.path.join(root, "var/home/agent/code"), exist_ok=True)
    if os.path.lexists(device):
        os.remove(device)
    with changing():
        attached = domain_state(scenario, domain).get("disks") or {}
    for disk in attached.values():
        if disk.get("serial") == "agentbox-inbox":
            os.symlink(disk["source"], device)
    r = subprocess.run([sys.executable, "-I", os.path.join(REPO, "vm", "files", INBOX.lstrip("/")), *argv],
                       capture_output=True, text=True, env=dict(os.environ, AGENTBOX_INBOX_ROOT=root))
    return {"stdout": r.stdout, "stderr": r.stderr, "exitcode": r.returncode}


def run_container_images(domain, argv):
    """The real agentbox-container-images in the VM's own root, with the fake podman there."""
    import subprocess
    root = guest_root(domain)
    podman = os.path.join(root, "usr", "bin", "podman")
    os.makedirs(os.path.dirname(podman), exist_ok=True)
    with open(podman, "w") as f:
        f.write(f'#!/bin/sh\nFAKE_GUEST_ROOT="{root}" exec python3 "{os.path.join(REPO, "tests", "fake_guest_podman.py")}" '
                '"$@"\n')
    os.chmod(podman, 0o755)
    r = subprocess.run([sys.executable, "-I", os.path.join(REPO, "vm", "files", CONTAINER_IMAGES.lstrip("/")), *argv],
                       capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "TZ": os.environ.get("TZ", "UTC"),
                            "AGENTBOX_CONTAINER_IMAGES_ROOT": root})
    return {"stdout": r.stdout, "stderr": r.stderr, "exitcode": r.returncode}


def guest_agent(scenario, rest):
    request = json.loads(rest[1])
    if request["execute"] == "guest-ping":
        # "domain", not "dom": not a change to the VM
        record({"cmd": "guest-ping", "domain": rest[0], "argv": rest[2:], "time": time.time()})
    time.sleep(scenario.get("agent_hangs", 0))
    if scenario.get("agent_down"):
        print("error: Guest agent is not responding: QEMU guest agent is not connected", file=sys.stderr)
        return 1
    if request["execute"] == "guest-ping":
        print(json.dumps({"return": {}}))
        return 0
    if request["execute"] == "guest-exec":
        calls = [c for c in load_calls() if c["cmd"] == "guest-exec"]
        path, argv = request["arguments"]["path"], request["arguments"]["arg"]
        record({"cmd": "guest-exec", "path": path, "arg": argv})
        if path in (scenario.get("missing_wrappers") or ()):
            print("error: internal error: unable to execute QEMU agent command 'guest-exec': Failed to execute "
                  f"child process \u201c{path}\u201d (No such file or directory)", file=sys.stderr)
            return 1
        pid = 4000 + len(calls)
        if path == INBOX and not scenario.get("responses"):
            with open(os.path.join(DIR, f"exec-{pid}.json"), "w") as f:
                json.dump(run_inbox(scenario, rest[0], argv), f)
        if path == CONTAINER_IMAGES and not scenario.get("responses") and not scenario.get("still_running"):
            with open(os.path.join(DIR, f"exec-{pid}.json"), "w") as f:
                json.dump(run_container_images(rest[0], argv), f)
        print(json.dumps({"return": {"pid": pid}}))
        return 0
    if request["execute"] == "guest-exec-status":
        import base64
        if scenario.get("still_running"):
            print(json.dumps({"return": {"exited": False}}))
            return 0
        n = request["arguments"]["pid"] - 4000
        responses = scenario.get("responses") or [{"entries": []}]
        r = responses[min(n, len(responses) - 1)]
        ran = os.path.join(DIR, f"exec-{request['arguments']['pid']}.json")
        if os.path.exists(ran):
            with open(ran) as f:
                r = json.load(f)
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
