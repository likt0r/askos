"""The registry put to work: the one-time migration of work and priv into it,
and the Host configuration of every VM generated from it.

`agentbox regenerate` writes per VM the libvirt network, the proxy and
token-service sockets and services, the Orca port forwarder, the Data disk,
the placeholders for the Claude token and the OpenCode key, the file of its VM
variables (vmenv.py), the Observer directory and the libvirt domain (through virt-install, ADR 0004);
for all VMs together the nftables rules of the VM networks and the storage
pool of their disks, and the journald
namespace of Activity with its retention, the timer of the Maintenance window (maintenance
time; maintenance.py) and of the Feeds (feed interval) with the Feeds service (young-domain
days), the unit of the Builds and the services of the version check (versions.py), all from
the Settings; what is left of the timers the Maintenance window replaced goes. Running it
again changes nothing unless a record or the retention changed. What was written but not applied
when a run failed (firewall rules, units) is applied by the next run.
"""
import datetime
import json
import os
import pwd
import re
import subprocess
import tempfile

from . import activity, hostsetup, lifecycle, paths, registry, settings, state_files, virsh, vmenv
from .errors import die
from .registry import bridge, dom, guest_ip, host_ip, mac

LEGACY = {"work": 1, "priv": 2}      # the fixed VMs before the registry, with their networks
DEFAULT_MEMORY_MIB = 26624           # what the fixed VMs had before the registry
DEFAULT_DATA_DISK_GIB = 300
VCPUS = 24
VMNET = "agentbox-vmnet.service"     # loads VM_RULES (enabled by agentbox setup)
VM_RULES = os.path.join(paths.CONF, "vm-input.nft")
POOL = "agentbox-vms"
JOURNALD = f"systemd-journald@{activity.NAMESPACE}"     # journald instance of the Activity namespace
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SAID = {"network": "network created or started", "units": "units written",
        "sockets": "changed sockets restarted on their new addresses",
        "network-redefined": "network brought to the current definition",
        "network-redefined-running": "network brought to the current definition and restarted; the running "
                                     "VM has no network until its next Restart (libvirt does not reconnect "
                                     "a running VM to a restarted network)",
        "data-disk": "Data disk created", "claude-token": "placeholder for the Claude token created",
        "opencode-key": "placeholder for the OpenCode key created",
        "vm-env": "VM variables written (apply from the next start)",
        "observer": "directory for the Observer created",
        "domain": "VM defined again (applies from the next start)",
        "domain-postponed": "VM definition postponed, the VM is paused"}


def data_disk(name):
    return os.path.join(paths.VMS, f"{name}-data.qcow2")


def legacy_owners_file(name):
    return os.path.join(paths.CONF, "github", f"owners-{name}")


def _remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


# --- migrate -----------------------------------------------------------------

def _memory_mib(name):
    """Memory of the existing domain, or None when libvirt has no such domain."""
    r = virsh.run("dominfo", dom(name))
    if r.returncode != 0:
        return None
    m = re.search(r"^Max memory:\s+(\d+) KiB", r.stdout, re.MULTILINE)
    return int(m.group(1)) // 1024 if m else DEFAULT_MEMORY_MIB


def _disk_gib(path):
    try:
        r = subprocess.run(["qemu-img", "info", "-U", "--output=json", path], capture_output=True, text=True,
                           timeout=30)
        return max(1, json.loads(r.stdout)["virtual-size"] // 2**30)
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        return DEFAULT_DATA_DISK_GIB


def _legacy_owners(name):
    from .credentials import OWNER
    owners = (line.decode("utf-8", "replace").strip() for line in state_files.content_lines(legacy_owners_file(name)))
    return [o for o in owners if OWNER.fullmatch(o)]


def _migrate_one(name, network):
    if os.path.exists(registry.record_file(name)):
        return "unchanged"
    if os.path.exists(delete_marker(name)):
        return "absent"                  # being deleted: its leftovers must not bring it back
    memory = _memory_mib(name)
    if memory is None and not os.path.exists(data_disk(name)) and not os.path.exists(legacy_owners_file(name)):
        return "absent"
    registry.save({
        "name": name, "network": network, "orca_port": registry.ORCA_BASE + network, "image": "base",
        "build": None, "memory_mib": memory or DEFAULT_MEMORY_MIB,
        "data_disk_gib": _disk_gib(data_disk(name)) if os.path.exists(data_disk(name)) else DEFAULT_DATA_DISK_GIB,
        "github_owners": _legacy_owners(name),
        "created": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    return "migrated"


def migrate(args):
    """agentbox migrate [--json]: records for work and priv from the state before
    the registry. Idempotent; disks, secrets and pairings stay where they are."""
    results = []
    for name, network in LEGACY.items():
        with state_files.locked(registry.record_file(name)):
            action = _migrate_one(name, network)
            # from now on the record holds the owners
            _remove(legacy_owners_file(name))
        results.append({"vm": name, "action": action})
        if "--json" not in args:
            print(f"{name}: " + {"migrated": f"taken over into the registry (network {network}, "
                                             f"Orca port {registry.ORCA_BASE + network})",
                                 "unchanged": "in the registry already",
                                 "absent": "does not exist, nothing to take over"}[action])
    if "--json" in args:
        print(json.dumps(results))


# --- regenerate --------------------------------------------------------------

def nft_rules(records):
    """A VM network reaches only the Host's resolver, proxy and token service on it. Only DHCP
    is accepted before the address check (the VM asks before it has an address); everything
    else, the resolver's port 53 included, only to the Host's address on the VM's own network,
    so no other resolver on the Host (one on its LAN address) can be reached. What reaches the
    Host's address on another port is refused at once (a TCP reset, ICMP port unreachable for
    UDP), so a program fails without waiting for a timeout: every name resolves to that address.
    Other addresses, IPv6 and anything but TCP and UDP are dropped. A reject ends the packet like
    a drop, so firewalld's zone libvirt (a later hook) never sees it."""
    return "".join(line + "\n" for line in [
        "table inet agentbox_vm",
        "delete table inet agentbox_vm",
        "table inet agentbox_vm {",
        "  chain input {",
        "    type filter hook input priority -5; policy accept",
        '    iifname "abox-*" jump from_vm',
        "  }",
        "  chain from_vm {",
        "    meta nfproto ipv6 drop",
        "    udp dport 67 accept",
        *(f'    iifname "{bridge(r)}" ip daddr != {host_ip(r)} drop' for r in records),
        "    ct state established,related accept",
        "    udp dport 53 accept",
        "    tcp dport { 53, 443, 9100 } accept",
        "    meta l4proto tcp reject with tcp reset",
        "    meta l4proto udp reject with icmp type port-unreachable",
        "    drop",
        "  }",
        "}",
    ])


def _dnsmasq_options(rec):
    return (f"address=/#/{host_ip(rec)}", "local=/#/", "no-hosts", "no-resolv")


def network_xml(rec, uuid=None):
    """Isolated: no forwarding, DHCP for the one VM only, and libvirt's dnsmasq as the VM's
    resolver, answering every name with the Host's address on this network (ADR 0014): the
    proxy there takes the real destination from the TLS handshake. Every other record type
    gets an empty answer (`local=/#/`), nothing comes from the Host's hosts file, and nothing
    is forwarded (`no-resolv`, which libvirt sets for an isolated network as well). `uuid` is the
    one of an existing network being redefined: libvirt refuses the same name with another."""
    uuid_line = f"  <uuid>{uuid}</uuid>\n" if uuid else ""
    options = "".join(f"    <dnsmasq:option value='{o}'/>\n" for o in _dnsmasq_options(rec))
    return f"""<network xmlns:dnsmasq='http://libvirt.org/schemas/network/dnsmasq/1.0'>
  <name>{dom(rec['name'])}</name>
{uuid_line}  <bridge name='{bridge(rec)}' stp='on' delay='0'/>
  <ip address='{host_ip(rec)}' netmask='255.255.255.0'>
    <dhcp>
      <range start='{guest_ip(rec)}' end='{guest_ip(rec)}'/>
      <host mac='{mac(rec)}' ip='{guest_ip(rec)}'/>
    </dhcp>
  </ip>
  <dnsmasq:options>
{options}  </dnsmasq:options>
</network>
"""


def journal_access(user):
    """Unit lines that let a service running as `user` connect its log stream to the
    Activity namespace, whose sockets only root and activity.JOURNAL_GROUP may write
    (the package's drop-in). The group is the primary group: systemd connects stdout and
    stderr with the unit's user and primary group, before it adds the
    SupplementaryGroups=, where the user's own group stays. Nothing while the group
    does not exist, because a unit naming it would not start."""
    if not activity.group_exists(activity.JOURNAL_GROUP):
        return ""
    return f"Group={activity.JOURNAL_GROUP}\nSupplementaryGroups={user}\n"


def units(rec):
    """systemd units of a VM by file name: proxy and token service on the Host's
    address in the VM network, logging into the Activity namespace, and the Orca
    port, where every connection wakes the VM if needed, with the client's
    address as actor (socat: SELinux keeps systemd-socket-proxyd off the port)."""
    vm, host, port = rec["name"], host_ip(rec), rec["orca_port"]
    orca_listen = hostsetup.orca_listen(hostsetup.load(), port)
    hardening = "NoNewPrivileges=yes\nProtectSystem=strict\nProtectHome=yes\nPrivateTmp=yes\n"
    return {
        f"agentbox-proxy-{vm}.socket": f"""[Unit]
Description=Allowlist proxy for VM {vm}

[Socket]
ListenStream={host}:443
FreeBind=yes

[Install]
WantedBy=sockets.target
""",
        f"agentbox-proxy-{vm}.service": f"""[Unit]
Description=Allowlist proxy for VM {vm}
Requires=agentbox-proxy-{vm}.socket

[Service]
ExecStart={paths.PROXY} {vm}
User={activity.PROXY_USER}
{journal_access(activity.PROXY_USER)}SyslogIdentifier=agentbox-proxy-{vm}
LogNamespace={activity.NAMESPACE}
{hardening}""",
        f"agentbox-ghvm-{vm}.socket": f"""[Unit]
Description=GitHub App tokens for VM {vm}

[Socket]
ListenStream={host}:9100
FreeBind=yes
Accept=yes
MaxConnections=16

[Install]
WantedBy=sockets.target
""",
        f"agentbox-ghvm-{vm}@.service": f"""[Unit]
Description=Issue a GitHub App token for VM {vm}

[Service]
ExecStart={paths.TOKEND} {vm}
User={activity.TOKEN_USER}
{journal_access(activity.TOKEN_USER)}StandardInput=socket
StandardOutput=socket
StandardError=journal
SyslogIdentifier=agentbox-gh-{vm}
LogNamespace={activity.NAMESPACE}
{hardening}""",
        f"agentbox-gui-{vm}.socket": f"""[Unit]
Description=Orca access to VM {vm}

[Socket]
ListenStream={orca_listen}
Accept=yes
MaxConnections=32

[Install]
WantedBy=sockets.target
""",
        f"agentbox-gui-{vm}@.service": f"""[Unit]
Description=Orca connection to VM {vm}

[Service]
ExecStartPre={paths.CLI} "--actor=Orca (${{REMOTE_ADDR}})" wake {vm}
ExecStart=/usr/bin/socat STDIO TCP:{guest_ip(rec)}:{port}
StandardInput=socket
StandardOutput=socket
StandardError=journal
TimeoutStartSec=180
""",
    }


def sockets(rec):
    return [f"agentbox-{kind}-{rec['name']}.socket" for kind in ("proxy", "ghvm", "gui")]


def virt_install_args(rec, uuid):
    """virt-install only prints the domain (--dry-run creates nothing). It insists
    on a size for disks that do not exist yet; size=1 only satisfies that check."""
    vm = rec["name"]
    disk = "path={},size=1,format=qcow2,bus=virtio,target.dev={},cache=none,io=native,discard=unmap"
    return [
        "--connect", "qemu:///system", "--print-xml", "--dry-run", "--check", "all=off",
        "--name", dom(vm), *(["--uuid", uuid] if uuid else []),
        "--osinfo", "linux2024", "--memory", str(rec["memory_mib"]), "--vcpus", str(VCPUS),
        "--cpu", "host-passthrough", "--machine", "q35", "--boot", "hd", "--import", "--noautoconsole",
        "--graphics", "none", "--video", "none", "--sound", "none", "--input", "none",
        "--controller", "usb,model=none",
        "--events", "on_poweroff=destroy,on_reboot=restart,on_crash=destroy",
        "--features", "acpi=on,apic=on",
        # the VM learns its name, Orca port, Claude token, the address devices pair with, its
        # VM variables and its OpenCode key at boot; values that could hold a comma only as
        # files (virt-install splits --sysinfo at commas)
        "--sysinfo", f"type=fwcfg,entry0.name=opt/agentbox/context,entry0={vm},"
                     f"entry1.name=opt/agentbox/orca_port,entry1={rec['orca_port']},"
                     f"entry2.name=opt/agentbox/claude_token,entry2.file={state_files.claude_token_file(vm)},"
                     f"entry3.name=opt/agentbox/pairing_address,"
                     f"entry3={hostsetup.pairing_address(hostsetup.load())},"
                     f"entry4.name=opt/agentbox/env,entry4.file={state_files.vm_env_file(vm)},"
                     f"entry5.name=opt/agentbox/opencode_key,entry5.file={state_files.opencode_key_file(vm)}",
        "--disk", disk.format(lifecycle.system_disk(vm), "vda"),
        "--disk", disk.format(data_disk(vm), "vdb") + ",serial=agentbox-data",
        "--network", f"network={dom(vm)},mac={mac(rec)},model=virtio",
        "--serial", "pty", "--console", "pty",
        "--channel", "unix,target.type=virtio,target.name=org.qemu.guest_agent.0",
        "--memballoon", "model=virtio,freePageReporting=on",
        "--rng", "/dev/urandom",
    ]


def _pending(what):
    """Marker: `what` was written but is not applied yet."""
    return os.path.join(paths.STATE, "pending", what)


def _mark(pending):
    if not os.path.isdir(os.path.dirname(_pending(pending))):
        os.makedirs(os.path.dirname(_pending(pending)))
        os.chmod(os.path.dirname(_pending(pending)), 0o700)
    open(_pending(pending), "w").close()


def _write_if_changed(path, data, mode, pending):
    """Write when different; the pending marker comes first, so a run that dies
    before applying leaves it for the next run."""
    try:
        with open(path, "rb") as f:
            if f.read() == data and os.stat(path).st_mode & 0o7777 == mode:
                return False
    except FileNotFoundError:
        pass
    _mark(pending)
    state_files.write_atomic(path, data, mode)
    return True


def delete_marker(name):
    """Marker of a VM that `agentbox delete` is taking down: its record, where its
    Data disk goes and whether its network is gone. Written before the record is
    removed and removed last, so an interrupted delete is finished by the next one."""
    return _pending(f"delete-{name}.json")


def being_deleted():
    """The markers of VMs being deleted; dies on a broken one."""
    folder = os.path.dirname(_pending(""))
    try:
        files = sorted(os.listdir(folder))
    except FileNotFoundError:
        return []
    markers = []
    for f in files:
        m = re.fullmatch(r"delete-(.+)\.json", f)
        if not m:
            continue
        try:
            with open(os.path.join(folder, f), encoding="utf-8") as fh:
                marker = json.load(fh)
            broken = not isinstance(marker, dict) or registry.problem(marker.get("record"), m.group(1))
        except (OSError, ValueError):
            broken = True
        if broken:
            die(f"{os.path.join(folder, f)}: broken marker", error="action_failed", path=os.path.join(folder, f))
        markers.append(marker)
    return markers


def _apply_pending(what, *systemctl):
    if not os.path.exists(_pending(what)):
        return False
    _systemctl(*systemctl)
    os.remove(_pending(what))
    return True


def _run(argv, what):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=120,
                           env=dict(os.environ, LC_ALL="C"))
    except (OSError, subprocess.TimeoutExpired) as e:
        die(f"{what}: {e}", error="action_failed")
    if r.returncode != 0:
        die(f"{what} fails: " + (r.stderr.strip()[-300:] or f"exit {r.returncode}"), error="action_failed")
    return r.stdout


def _virsh(what, *args):
    r = virsh.run(*args)
    if r.returncode != 0:
        die(f"{what} fails: " + (r.stderr.strip()[-300:] or f"exit {r.returncode}"), error="action_failed")


def _virsh_define(cmd, xml, what):
    with tempfile.NamedTemporaryFile("w", suffix=".xml") as f:
        f.write(xml)
        f.flush()
        _virsh(what, cmd, f.name)


def _qemu():
    try:
        q = pwd.getpwnam("qemu")
    except KeyError:
        die("user qemu is missing (is libvirt installed?)", error="action_failed", user="qemu")
    return q.pw_uid, q.pw_gid


def _network_matches(rec):
    """An existing network must be the one the record describes: network numbers are fixed
    once a VM exists. Its XML as libvirt dumps it."""
    net = dom(rec["name"])
    r = virsh.run("net-dumpxml", net)
    found = {k: re.search(rf"<{tag}\b[^>]*\b{attr}=['\"]([^'\"]+)", r.stdout)
             for k, (tag, attr) in {"bridge": ("bridge", "name"), "address": ("ip", "address"),
                                    "mac": ("host", "mac")}.items()}
    found = {k: m.group(1) if m else None for k, m in found.items()}
    wanted = {"bridge": bridge(rec), "address": host_ip(rec), "mac": mac(rec)}
    if r.returncode != 0 or found != wanted:
        die(f"{rec['name']}: network {net} does not match the record "
            f"({', '.join(f'{k} {found[k]} instead of {wanted[k]}' for k in wanted if found[k] != wanted[k])})",
            error="action_failed", vm=rec["name"])
    return r.stdout


def _resolver_current(rec, xml):
    """Whether a network's dumped XML has the resolver network_xml() gives it: every dnsmasq
    option, DNS not switched off, and nothing that sends names or packets on (a `server=`
    option, a `<forwarder>`, a `<forward>`)."""
    return (not re.search(r"<dns\b[^>]*\benable=['\"]no['\"]", xml)
            and not re.search(r"<dnsmasq:option\s+value=['\"]server=|<forwarder\b|<forward\b", xml)
            and all(re.search(rf"<dnsmasq:option\s+value=['\"]{re.escape(o)}['\"]", xml)
                    for o in _dnsmasq_options(rec)))


def _vm_live(rec):
    """Whether the VM has a QEMU process (running or paused in memory): its network interface
    stays with it until the process ends."""
    r = virsh.run("domstate", dom(rec["name"]))
    return r.returncode == 0 and r.stdout.strip() not in ("shut off", "crashed", "")


def _network(rec):
    """The VM's network created, brought to the current definition, autostarted and started:
    True or the key of what it said, else False. libvirt's dnsmasq reads its options only when
    the network starts, so an active network whose resolver is not the current one is
    stopped and started again."""
    net = dom(rec["name"])
    r = virsh.run("net-info", net)
    changed = False
    if r.returncode == 0:
        xml = _network_matches(rec)
        info = dict(line.split(":", 1) for line in r.stdout.splitlines() if ":" in line)
        info = {k.strip(): v.strip() for k, v in info.items()}
        if not _resolver_current(rec, xml):
            current = re.search(r"<uuid>([^<]+)</uuid>", xml)
            _virsh_define("net-define", network_xml(rec, current.group(1) if current else None),
                          f"{rec['name']}: define network")
            changed = "network-redefined"
            if info.get("Active") == "yes":
                live = _vm_live(rec)
                _virsh(f"{rec['name']}: stop network", "net-destroy", net)
                _virsh(f"{rec['name']}: start network", "net-start", net)
                changed = "network-redefined-running" if live else changed
    else:
        _virsh_define("net-define", network_xml(rec), f"{rec['name']}: create network")
        info = {}
        changed = True
    if info.get("Autostart") != "yes":
        _virsh(f"{rec['name']}: autostart network", "net-autostart", net)
        changed = changed or True
    if info.get("Active") != "yes":
        _virsh(f"{rec['name']}: start network", "net-start", net)
        changed = changed or True
    return changed


def _units(rec):
    """The VM's units written. A socket unit that existed with other text is marked, before
    it is written, to be listened on again (_rebind)."""
    changed = False
    for name, text in units(rec).items():
        path = os.path.join(paths.SYSTEMD, name)
        if name.endswith(".socket"):
            try:
                with open(path, "rb") as f:
                    if f.read() != text.encode():
                        _mark(f"socket-{name}")
            except FileNotFoundError:
                pass
        changed |= _write_if_changed(path, text.encode(), 0o644, "units")
    return changed


def _rebind(rec):
    """Each of the VM's sockets whose unit text changed, listening where it now says, after
    daemon-reload: systemd keeps an active socket on its old address, and a service that was
    handed the listening socket (Accept=no, the proxy) keeps serving it, so that service is
    stopped first and the socket restarted; the next connection starts the service on the
    new socket. Whether one was restarted."""
    done = False
    for name, text in units(rec).items():
        if not name.endswith(".socket") or not os.path.exists(_pending(f"socket-{name}")):
            continue
        if "Accept=yes" not in text:
            _systemctl("stop", name[:-len(".socket")] + ".service")
        _systemctl("restart", name)
        os.remove(_pending(f"socket-{name}"))
        done = True
    return done


def _storage_pool():
    """virt-install makes up a storage pool for any disk directory without one;
    agentbox defines it instead, once. Any pool on that directory will do."""
    r = virsh.run("pool-list", "--all", "--name")
    for name in r.stdout.split() if r.returncode == 0 else []:
        m = re.search(r"<path>([^<]+)</path>", virsh.run("pool-dumpxml", name).stdout)
        if m and m.group(1) == paths.VMS:
            return False
    xml = f"<pool type='dir'>\n  <name>{POOL}</name>\n  <target>\n    <path>{paths.VMS}</path>\n  </target>\n</pool>\n"
    _virsh_define("pool-define", xml, "create storage pool")
    _virsh("autostart storage pool", "pool-autostart", POOL)
    _virsh("start storage pool", "pool-start", POOL)
    return True


def _data_disk(rec):
    """Created once and kept: repositories, Orca's identity and pairings live there."""
    path = data_disk(rec["name"])
    if os.path.exists(path):
        return False
    _run(["qemu-img", "create", "-q", "-f", "qcow2", path, f"{rec['data_disk_gib']}G"],
         f"{rec['name']}: create Data disk")
    if os.geteuid() == 0:
        os.chown(path, *_qemu())
    return True


def _secret_placeholder(path):
    """QEMU refuses empty fw_cfg files, so a VM without token or key gets "none"."""
    secrets = os.path.dirname(path)
    os.makedirs(secrets, exist_ok=True)
    os.chmod(secrets, 0o750)
    gid = _qemu()[1] if os.geteuid() == 0 else -1
    if os.geteuid() == 0:
        os.chown(secrets, 0, gid)
    if os.path.exists(path):
        return False
    state_files.write_atomic(path, b"none\n", 0o640, gid)
    return True


def _claude_token(rec):
    return _secret_placeholder(state_files.claude_token_file(rec["name"]))


def _opencode_key(rec):
    return _secret_placeholder(state_files.opencode_key_file(rec["name"]))


def fw_cfg_file(path, data):
    """Write a file QEMU passes into a VM as an fw_cfg entry, when its content or mode differ:
    owner root, group qemu, mode 0640, in a directory of the same group with mode 0750 (the
    Claude token's way). Whether it was written."""
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    os.chmod(folder, 0o750)
    gid = _qemu()[1] if os.geteuid() == 0 else -1
    if os.geteuid() == 0:
        os.chown(folder, 0, gid)
    try:
        with open(path, "rb") as f:
            st = os.fstat(f.fileno())
            if f.read() == data and st.st_mode & 0o7777 == 0o640 and gid in (-1, st.st_gid):
                return False
    except FileNotFoundError:
        pass
    state_files.write_atomic(path, data, 0o640, gid)
    return True


def vm_env(rec):
    """The file of the VM's VM variables (vmenv.py), from its record; a VM picks a change up at
    its next start. Written before the domain is defined, which names it."""
    return fw_cfg_file(state_files.vm_env_file(rec["name"]), vmenv.fw_cfg(rec.get("env", {})))


def _observer(rec):
    path = lifecycle.observer_dir(rec["name"])
    if os.path.isdir(path):
        return False
    os.makedirs(path)
    os.chmod(path, 0o700)
    return True


def domain(rec):
    """Defined again whenever virt-install's output changed; a running VM picks
    it up at its next start (libvirt keeps the new definition for then). Not while
    paused: its saved memory belongs to the old definition. "domain",
    "domain-postponed" or None (unchanged). Callers hold state_files.locked(paths.REGISTRY)."""
    vm = rec["name"]
    # QEMU refuses to start with an fw_cfg file missing: set-memory and set-env define the VM
    # again on a Host updated from a Release without the OpenCode key, where no regenerate ran
    _claude_token(rec)
    _opencode_key(rec)
    r = virsh.run("domuuid", dom(vm))
    uuid = r.stdout.strip() if r.returncode == 0 and UUID.fullmatch(r.stdout.strip()) else None
    xml = _run(["virt-install", *virt_install_args(rec, uuid)], f"{vm}: virt-install")
    saved = os.path.join(paths.STATE, "domains", f"{vm}.xml")
    try:
        with open(saved) as f:
            if uuid and f.read() == xml:
                return None
    except FileNotFoundError:
        pass
    if uuid and lifecycle.has_save(vm):
        return "domain-postponed"
    _virsh_define("define", xml, f"{vm}: define VM")
    if not os.path.isdir(os.path.dirname(saved)):
        os.makedirs(os.path.dirname(saved))
        os.chmod(os.path.dirname(saved), 0o700)
    state_files.write_atomic(saved, xml.encode(), 0o600)
    return "domain"


def _systemctl(*args):
    _run(["systemctl", *args], "systemctl " + " ".join(args))


def _activity_journal(days):
    """The journald namespace of Activity, keeping entries for `days` days; its
    journald picks a changed retention up on restart (it keeps the log streams
    of the services meanwhile). Returns the days when they changed, else None."""
    _write_if_changed(activity.JOURNALD_CONF, activity.journald_conf(days).encode(), 0o644, "journald")
    changed = _apply_pending("journald", "try-restart", f"{JOURNALD}.service")
    _systemctl("enable", "--now", f"{JOURNALD}.socket")
    return days if changed else None


FEEDS = "agentbox-feeds"

FEEDS_SERVICE = """\
# Generated by agentbox regenerate (young_domain_days in /etc/agentbox/settings.json)
[Unit]
Description=Fetch the HaGeZi lists for the detection
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=agentbox-feeds
ExecStart={program}
Environment=AGENTBOX_FEEDS_YOUNG_DAYS={young_days}
SyslogIdentifier=agentbox-feeds
# writes only /var/lib/agentbox/feeds (systemd creates it for the user)
StateDirectory=agentbox/feeds
StateDirectoryMode=0755
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK
MemoryMax=1G
TimeoutStartSec=1h
"""

# for a Host that is awake for long; the Maintenance window fetches them every day as well
FEEDS_TIMER = """\
# Generated by agentbox regenerate (feed_interval_hours in /etc/agentbox/settings.json)
[Unit]
Description=Fetch the HaGeZi lists for the detection regularly

[Timer]
OnBootSec=10min
OnUnitActiveSec={hours}h
RandomizedDelaySec=15min

[Install]
WantedBy=timers.target
"""


BUILD_UNIT = "agentbox-build@.service"

# Ein Build als Unit, sein Journal ist sein Protokoll (ADR 0004); agentbox build startet
# sie, agentbox-build@all baut das Base image und danach alle anderen Images
BUILD_SERVICE = f"""\
# Generated by agentbox regenerate
[Unit]
Description=Agent Box: build Image %i from the installed Release (all: every Image; update: each with an update)
Wants=network-online.target
After=network-online.target

[Service]
Type=exec
ExecStart={paths.CLI} build-run %i
SyslogIdentifier=agentbox-build
# podman as root; without User= systemd sets no HOME
Environment=HOME=/root
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
RuntimeMaxSec=6h
TimeoutStopSec=2min
"""


VERSIONS = "agentbox-versions"
VERSIONS_FETCH = "agentbox-versions-fetch"

# Was ein neuer Build braechte (versions.py), in zwei Teilen: die Abfrage bei npm und GitHub
# als agentbox-feeds, ohne Zugriff auf Schluessel, Tokens, VMs und Builds; danach als root nur
# was root braucht (Paketupdates in einem Container des Builds), das die Antwort als
# unsicheren Input liest
VERSIONS_FETCH_SERVICE = """\
# Generated by agentbox regenerate (package_min_age_days and orca_version in
# /etc/agentbox/settings.json)
[Unit]
Description=Agent Box: ask npm and GitHub for the versions available
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=agentbox-feeds
ExecStart={program} fetch-versions
Environment=AGENTBOX_VERSIONS_MIN_AGE_DAYS={days}
Environment=AGENTBOX_VERSIONS_ORCA_VERSION={orca}
SyslogIdentifier=agentbox-versions-fetch
# writes only /var/lib/agentbox/available (systemd creates it for the user)
StateDirectory=agentbox/available
StateDirectoryMode=0755
UMask=0022
# of /var/lib/agentbox only available/ (allowlist); nothing of /etc/agentbox, libvirt, podman
TemporaryFileSystem=/var/lib/agentbox:ro
BindPaths=/var/lib/agentbox/available
InaccessiblePaths=-/etc/agentbox -/var/lib/libvirt -/var/lib/containers -/run/libvirt -/run/podman
# the internet yes; not link-local, multicast and the VM networks (the VMs' proxy and token listeners)
IPAddressDeny=169.254.0.0/16 224.0.0.0/4 10.77.0.0/16 fe80::/10 ff00::/8
NoNewPrivileges=yes
CapabilityBoundingSet=
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
PrivateIPC=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
ProtectProc=invisible
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK
MemoryMax=1G
TimeoutStartSec=15min
"""

# the version check: the second step of the Maintenance window, or at once by hand
VERSIONS_SERVICE = f"""\
# Generated by agentbox regenerate
[Unit]
Description=Agent Box: check the versions available of the tools in the Images
Wants=network-online.target {VERSIONS_FETCH}.service
After=network-online.target {VERSIONS_FETCH}.service

[Service]
Type=oneshot
ExecStart={paths.CLI} --actor=timer check-versions
SyslogIdentifier=agentbox-versions
# podman as root; without User= systemd sets no HOME
Environment=HOME=/root
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
TimeoutStartSec=1h
"""


MAINTENANCE = "agentbox-maintenance"
MAINTENANCE_STAMP = paths.host(f"/var/lib/systemd/timers/stamp-{MAINTENANCE}.timer")   # its last run

# Das Wartungsfenster (#49): Feeds, Versionspruefung, woechentlicher Neubau und Reset-Pruefung
# nacheinander, jeder Schritt wartet auf den vorigen (maintenance.py)
MAINTENANCE_SERVICE = f"""\
# Generated by agentbox regenerate
[Unit]
Description=Agent Box: Maintenance window (Feeds, version check, weekly rebuild, Reset check)
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
# the actor in Activity
ExecStart={paths.CLI} --actor=timer maintenance
SyslogIdentifier=agentbox-maintenance
# it waits for the Builds it started, which end on their own (RuntimeMaxSec of their unit)
TimeoutStartSec=infinity
"""

# Persistent: a window missed while the Host was off runs once after boot. Waking a sleeping
# Host is the Host's business (no WakeSystem=)
MAINTENANCE_TIMER = """\
# Generated by agentbox regenerate (maintenance_time in /etc/agentbox/settings.json)
[Unit]
Description=Agent Box: Maintenance window daily

[Timer]
OnCalendar=*-*-* {time}
Persistent=true

[Install]
WantedBy=timers.target
"""

# the timers of before the Maintenance window (#49) and the services only they started; the
# package removes them on update (rpm/agentbox.spec), regenerate whatever is left of them
RETIRED_TIMERS = ("agentbox-weekly.timer", "agentbox-rebuild.timer", "agentbox-versions.timer")
RETIRED_SERVICES = ("agentbox-weekly.service", "agentbox-rebuild.service")


def _feeds(values):
    """The units of the Feeds, from the Settings. A changed unit is loaded and its timer
    restarted, so the new interval applies at once (a restarted timer fetches the lists at
    once). Returns whether something changed."""
    units = {f"{FEEDS}.service": FEEDS_SERVICE.format(program=paths.FEEDS_PROGRAM,
                                                      young_days=values["young_domain_days"]),
             f"{FEEDS}.timer": FEEDS_TIMER.format(hours=values["feed_interval_hours"])}
    for name, text in units.items():
        _write_if_changed(os.path.join(paths.SYSTEMD, name), text.encode(), 0o644, "timers")
    changed = os.path.exists(_pending("timers"))
    if changed:
        _systemctl("daemon-reload")
        _systemctl("restart", f"{FEEDS}.timer")
        os.remove(_pending("timers"))
    _systemctl("enable", "--now", f"{FEEDS}.timer")
    return changed


def _builds():
    """The unit of the Builds. Returns whether it changed."""
    if os.path.exists(_pending("rebuild")):        # its marker before the Maintenance window (#49)
        _mark("builds")
        os.remove(_pending("rebuild"))
    _write_if_changed(os.path.join(paths.SYSTEMD, BUILD_UNIT), BUILD_SERVICE.encode(), 0o644, "builds")
    return _apply_pending("builds", "daemon-reload")


def _versions(values):
    """The version check, which the Maintenance window starts: the fetch as agentbox-feeds (with
    package_min_age_days and orca_version of the Settings), then the root part. A changed
    service is loaded and checks at once (with auto_update, an update may start a Build then).
    Returns whether something changed."""
    services = {f"{VERSIONS_FETCH}.service": VERSIONS_FETCH_SERVICE.format(program=paths.CLI,
                                                                            days=values["package_min_age_days"],
                                                                            orca=values["orca_version"]),
                f"{VERSIONS}.service": VERSIONS_SERVICE}
    for name, text in services.items():
        _write_if_changed(os.path.join(paths.SYSTEMD, name), text.encode(), 0o644, "versions")
    changed = os.path.exists(_pending("versions"))
    if changed:
        _systemctl("daemon-reload")
        _systemctl("start", "--no-block", f"{VERSIONS}.service")
        os.remove(_pending("versions"))
    return changed


def _maintenance(values):
    """The Maintenance window at maintenance_time (#49), and the timers it replaced taken away.
    The timer is Persistent: reloaded or restarted while its last run lies before today's new
    time, systemd would catch that run up at once (tried on titan with the Reset timer before
    it). A changed timer is stopped first and started with its stamp set to now, so it waits for
    its new time. Returns whether something changed."""
    _write_if_changed(os.path.join(paths.SYSTEMD, f"{MAINTENANCE}.service"), MAINTENANCE_SERVICE.encode(), 0o644,
                      "maintenance")
    _write_if_changed(os.path.join(paths.SYSTEMD, f"{MAINTENANCE}.timer"),
                      MAINTENANCE_TIMER.format(time=values["maintenance_time"]).encode(), 0o644, "maintenance-timer")
    timer, service = os.path.exists(_pending("maintenance-timer")), os.path.exists(_pending("maintenance"))
    if timer:
        _systemctl("stop", f"{MAINTENANCE}.timer")
        _systemctl("daemon-reload")
        os.makedirs(os.path.dirname(MAINTENANCE_STAMP), exist_ok=True)
        with open(MAINTENANCE_STAMP, "a"):
            pass
        os.utime(MAINTENANCE_STAMP)
        _systemctl("start", f"{MAINTENANCE}.timer")
        os.remove(_pending("maintenance-timer"))
    elif service:
        _systemctl("daemon-reload")
    if service:
        os.remove(_pending("maintenance"))
    _remove(_pending("versions-timer"))             # a marker of a retired timer
    _systemctl("enable", "--now", f"{MAINTENANCE}.timer")
    return _retire() or timer or service


def _retire():
    """The timers of before the Maintenance window disabled and stopped, they and their services
    deleted, and forgotten by systemd. A run of their service that is under way finishes. Whether
    there were any."""
    timers = [t for t in RETIRED_TIMERS if os.path.exists(os.path.join(paths.SYSTEMD, t))]
    services = [u for u in RETIRED_SERVICES if os.path.exists(os.path.join(paths.SYSTEMD, u))]
    if not timers and not services:
        return False
    if timers:
        _systemctl("disable", "--now", *timers)
    for name in timers + services:
        _remove(os.path.join(paths.SYSTEMD, name))
    _systemctl("daemon-reload")
    return True


# what each VM reaches on the Host's address in its network besides the token service: the
# resolver (libvirt's dnsmasq) and the proxy
VM_SERVICE_PORTS = (("tcp", 53), ("udp", 53), ("tcp", 443))


def _wildcard_listeners():
    """The (protocol, port) of VM_SERVICE_PORTS a socket on all of the Host's addresses
    (0.0.0.0 or ::) holds, from /proc/net: TCP in LISTEN, UDP bound. Agent Box's own resolver
    and proxy bind only the Host's address in each VM network, so such a socket is another
    service's, and the kernel lets neither of them bind its address while it is there."""
    found = set()
    for proto in ("tcp", "udp"):
        for table in (proto, proto + "6"):
            try:
                with open(paths.host(f"/proc/net/{table}")) as f:
                    rows = f.read().splitlines()[1:]
            except FileNotFoundError:
                continue
            for row in rows:
                fields = row.split()
                if len(fields) < 4 or (proto == "tcp" and fields[3] != "0A"):     # 0A: LISTEN
                    continue
                address, _, port = fields[1].rpartition(":")
                try:
                    wildcard, port = int(address, 16) == 0, int(port, 16)
                except ValueError:
                    continue
                if wildcard and (proto, port) in VM_SERVICE_PORTS:
                    found.add((proto, port))
    return found


def ports_free():
    """Dies, before anything changes, when another service holds a port of the VMs' resolver
    or proxy on all of the Host's addresses: libvirt could not start a VM network's dnsmasq,
    nor systemd the proxy's socket."""
    taken = ", ".join(f"{port}/{proto}" for proto, port in sorted(_wildcard_listeners(), key=lambda t: (t[1], t[0])))
    if taken:
        die(f"another service listens on {taken} on all of the Host's addresses: the VMs' resolver and proxy "
            "need these ports on the Host's address in each VM network, and could not start. Stop that "
            "service or bind it only to the addresses it serves.", error="action_failed", ports=taken)


def apply():
    """The Host configuration of every VM from the registry; what changed as
    {"vms": {vm: [step, ...]}, "firewall": bool, "storage": bool, "retention": days or None,
    "timers": bool}.
    Callers hold state_files.locked(paths.REGISTRY), like create and delete, so runs never overlap."""
    records = registry.all_records()     # a broken registry stops everything before any change
    values = settings.load()             # so do broken settings
    ports_free()                         # so does a service that would keep resolver or proxy from starting
    days = values["activity_retention_days"]
    # a VM being deleted stays isolated until its network is gone
    isolated = records + [m["record"] for m in being_deleted() if not m.get("network_removed")]
    _write_if_changed(VM_RULES,
                      nft_rules(sorted(isolated, key=lambda r: r["network"])).encode(), 0o644, "firewall")
    firewall = _apply_pending("firewall", "restart", VMNET)     # before any new bridge comes up
    retention = _activity_journal(days)     # before units that log into it start
    storage = _storage_pool() if records else False
    changes = {}
    for rec in records:
        done = []
        for key, step in (("network", _network), ("units", _units), ("data-disk", _data_disk),
                          ("claude-token", _claude_token), ("opencode-key", _opencode_key), ("vm-env", vm_env),
                          ("observer", _observer)):
            got = step(rec)
            if got:
                done.append(got if isinstance(got, str) else key)
        defined = domain(rec)
        if defined:
            done.append(defined)
        changes[rec["name"]] = done
    timers = _feeds(values)
    timers = _builds() or timers
    timers = _versions(values) or timers
    timers = _maintenance(values) or timers
    _apply_pending("units", "daemon-reload")
    for rec in records:
        if _rebind(rec):
            changes[rec["name"]].append("sockets")
    if records:
        _systemctl("enable", "--now", *[s for rec in records for s in sockets(rec)])
    return {"vms": changes, "firewall": firewall, "storage": storage, "retention": retention,
            "timers": timers}


def report(result):
    """The lines saying what apply() changed (none when nothing changed)."""
    lines = [f"{vm}: {SAID[key]}" for vm, done in result["vms"].items() for key in done]
    if result["firewall"]:
        lines.append("network rules reloaded")
    if result["storage"]:
        lines.append(f"storage pool {POOL} for {paths.VMS} created")
    if result["retention"]:
        lines.append(f"Activity is kept for {result['retention']} days")
    if result["timers"]:
        lines.append("timers and services for the Maintenance window, lists, Builds and versions written again "
                     "and restarted")
    return lines


def regenerate(args):
    """agentbox regenerate [--json]: the Host configuration of every VM from the registry, and
    the ports of the firewalld zone libvirt the VMs reach (as setup opens them), so this
    command alone brings a Host's VMs up to date with an installed Release."""
    from .hoststeps import libvirt_zone
    hostsetup.load()    # a Host setup that makes no sense stops us before anything is written
    with state_files.locked(paths.REGISTRY):
        registry.all_records()      # a broken registry stops everything before any change
        settings.load()             # so do broken settings
        ports_free()                # so does a service holding the resolver's or the proxy's ports
        zone = libvirt_zone()       # before any VM network comes up: the zone rejects what it does not list
        result = apply()
    result["zone"] = zone
    if "--json" in args:
        print(json.dumps(result))
        return
    lines = ([f"firewall zone libvirt: {'; '.join(zone)}"] if zone else []) + report(result)
    print("\n".join(lines) or "everything up to date")


COMMANDS = {"migrate": migrate, "regenerate": regenerate}
