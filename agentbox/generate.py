"""The registry put to work: the one-time migration of work and priv into it,
and the Host configuration of every VM generated from it.

`agentbox regenerate` writes per VM the libvirt network, the sockets and services of
the proxy, the token service and its Anthropic route, the Orca port forwarder, the SSH port
forwarder while the VM has SSH access, the Data disk, the Claude placeholder (made once) and
an empty Claude token file, the file of its VM variables (vmenv.py), the file of its secrets (never
from the record: it holds their values, the record only their names), the file of its SSH access
with the SSH host key it makes once (sshkeys.py), the Observer directory and the
libvirt domain (through virt-install, ADR 0004);
for all VMs together the nftables rules of the VM networks and the storage
pool of their disks, and the journald
namespace of Activity with its retention, the timer of the Maintenance window (maintenance
time; maintenance.py) and of the Feeds (feed interval) with the Feeds service (young-domain
days), the unit of the Builds and the services of the version check (versions.py), all from
the Settings; what is left of the timers the Maintenance window replaced goes. In Network
mode it adds the firewall rules of the SSH ports before any SSH port listens on every address
(firewall.py), so an update needs no setup for them. Running it
again changes nothing unless a record or the retention changed. What was written but not applied
when a run failed (firewall rules, units) is applied by the next run.
"""
import base64
import json
import os
import re
import stat
import sys
import tempfile
import time

from . import activity, build_vm_run, firewall, footprint, hostsetup, paths, programs, registry, settings, sshkeys
from . import domain_args, hostfacts, state_files
from . import states, times, versions, virsh, vmenv
from .commands import declare
from .errors import Fail, die
from .registry import bridge, dom, guest_ip, host_ip, mac

LEGACY = {"work": 1, "priv": 2}      # the fixed VMs before the registry, with their networks
DEFAULT_MEMORY_MIB = 26624           # what the fixed VMs had before the registry
DEFAULT_DATA_DISK_GIB = 300
VCPUS = 24
VMNET = "agentbox-vmnet.service"     # loads VM_RULES (enabled by agentbox setup)
VM_RULES = os.path.join(paths.CONF, "vm-input.nft")
# More for the Anthropic route's unit: its process holds every VM's Claude token (ADR 0023), and
# needs only its inherited connection, files, the journal and TCP to api.anthropic.com (glibc's
# resolver may use AF_UNIX and AF_NETLINK). MemoryDenyWriteExecute stays out: not every Python
# build runs with it.
CLAUDE_HARDENING = ("CapabilityBoundingSet=\nAmbientCapabilities=\n"
                    "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK\n"
                    "ProtectKernelTunables=yes\nProtectKernelModules=yes\nProtectKernelLogs=yes\n"
                    "ProtectControlGroups=yes\nRestrictNamespaces=yes\nLockPersonality=yes\nRestrictRealtime=yes\n"
                    "SystemCallFilter=@system-service\nSystemCallArchitectures=native\n")
POOL = "agentbox-vms"
JOURNALD = f"systemd-journald@{activity.NAMESPACE}"     # journald instance of the Activity namespace
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SAID = {"network": "network created or started", "units": "units written",
        "sockets": "changed sockets restarted on their new addresses",
        "network-redefined": "network brought to the current definition",
        "network-redefined-running": "network brought to the current definition and restarted; the running "
                                     "VM has no network until its next Restart (libvirt does not reconnect "
                                     "a running VM to a restarted network)",
        "data-disk": "Data disk created", "claude": "Claude placeholder and token file created",
        "vm-env": "VM variables written (apply from the next start)",
        "vm-secrets": "secrets written (apply from the next start)",
        "ssh-host-key": "SSH host key made, SSH access written (applies from the next start)",
        "vm-ssh": "SSH access written (applies from the next start)",
        "observer": "directory for the Observer created",
        "domain": "VM defined again (applies from the next start)",
        "domain-postponed": "VM definition postponed, the VM is paused"}


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
        return max(1, programs.disk_info(path)["virtual-size"] // 2**30)
    except (KeyError, TypeError):            # no answer (None), no size
        return DEFAULT_DATA_DISK_GIB


def _legacy_owners(name):
    from .credentials import OWNER
    owners = (line.decode("utf-8", "replace").strip() for line in state_files.content_lines(footprint.legacy_owners_file(name)))
    return [o for o in owners if OWNER.fullmatch(o)]


def _migrate_one(name, network):
    if os.path.exists(footprint.record_file(name)):
        return "unchanged"
    if os.path.exists(footprint.delete_marker(name)):
        return "absent"                  # being deleted: its leftovers must not bring it back
    memory = _memory_mib(name)
    if memory is None and not os.path.exists(footprint.data_disk(name)) and not os.path.exists(footprint.legacy_owners_file(name)):
        return "absent"
    registry.save({
        "name": name, "network": network, "orca_port": registry.ORCA_BASE + network, "image": "base",
        "build": None, "memory_mib": memory or DEFAULT_MEMORY_MIB,
        "data_disk_gib": _disk_gib(footprint.data_disk(name)) if os.path.exists(footprint.data_disk(name)) else DEFAULT_DATA_DISK_GIB,
        "github_owners": _legacy_owners(name),
        "created": times.iso(time.time()),
    })
    return "migrated"


def migrate(args):
    """agentbox migrate [--json]: records for work and priv from the state before
    the registry. Idempotent; disks, secrets and pairings stay where they are."""
    results = []
    for name, network in LEGACY.items():
        with state_files.locked(footprint.record_file(name)):
            action = _migrate_one(name, network)
            # from now on the record holds the owners
            state_files.remove(footprint.legacy_owners_file(name), fail=False)
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
    """A VM network reaches only the Host's resolver, proxy and token service (GitHub on 9100, the
    Anthropic route on 9101) on it. Only DHCP
    is accepted before the address check (the VM asks before it has an address); everything
    else, the resolver's port 53 included, only to the Host's address on the VM's own network,
    so no other resolver on the Host (one on its LAN address) can be reached. What reaches the
    Host's address on another port is refused at once (a TCP reset, ICMP port unreachable for
    UDP), so a program fails without waiting for a timeout: every name resolves to that address.
    Other addresses, IPv6 and anything but TCP and UDP are dropped. A reject ends the packet like
    a drop, so firewalld's zone libvirt (a later hook) never sees it.

    The build VMs' network (build_vm_run.rule_lines) has chains of its own: to the Host only
    DHCP and DNS, nothing forwarded to the home network, the VMs or over IPv6."""
    return "".join(line + "\n" for line in [
        "table inet agentbox_vm",
        "delete table inet agentbox_vm",
        "table inet agentbox_vm {",
        "  chain input {",
        "    type filter hook input priority -5; policy accept",
        '    iifname "abox-*" jump from_vm',
        build_vm_run.input_jump(),
        "  }",
        "  chain from_vm {",
        "    meta nfproto ipv6 drop",
        "    udp dport 67 accept",
        *(f'    iifname "{bridge(r)}" ip daddr != {host_ip(r)} drop' for r in records),
        "    ct state established,related accept",
        "    udp dport 53 accept",
        "    tcp dport { 53, 443, 9100, 9101 } accept",
        "    meta l4proto tcp reject with tcp reset",
        "    meta l4proto udp reject with icmp type port-unreachable",
        "    drop",
        "  }",
        *build_vm_run.rule_lines(),
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
    """systemd units of a VM by file name: proxy, token service and Anthropic route on the Host's
    address in the VM network, logging into the Activity namespace, the Orca port
    and, while the VM has SSH access, its SSH port (ADR 0020), both only on this Host in
    Local-only and on every address in Network mode, where every
    connection wakes the VM if needed, with the client's address as actor (socat:
    SELinux keeps systemd-socket-proxyd off the port). wake returns once Orca answers
    and sshd may start a moment later, so the SSH port tries port 22 for up to 10 seconds."""
    vm, host, port = rec["name"], host_ip(rec), rec["orca_port"]
    names = {kind: footprint.unit_name(vm, kind) for kind in footprint.UNIT_KINDS}
    setup = hostsetup.load()
    hardening = "NoNewPrivileges=yes\nProtectSystem=strict\nProtectHome=yes\nPrivateTmp=yes\n"
    return {
        names["proxy.socket"]: f"""[Unit]
Description=Allowlist proxy for VM {vm}

[Socket]
ListenStream={host}:443
FreeBind=yes

[Install]
WantedBy=sockets.target
""",
        names["proxy.service"]: f"""[Unit]
Description=Allowlist proxy for VM {vm}
Requires=agentbox-proxy-{vm}.socket

[Service]
ExecStart={paths.PROXY} {vm}
User={activity.PROXY_USER}
{journal_access(activity.PROXY_USER)}SyslogIdentifier=agentbox-proxy-{vm}
LogNamespace={activity.NAMESPACE}
{hardening}""",
        names["ghvm.socket"]: f"""[Unit]
Description=GitHub App tokens for VM {vm}

[Socket]
ListenStream={host}:9100
FreeBind=yes
Accept=yes
MaxConnections=16

[Install]
WantedBy=sockets.target
""",
        names["ghvm@.service"]: f"""[Unit]
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
        # the route serves at most claude_route SLOTS (64) connections at once; the socket lets one
        # more in, which the route refuses so that Activity shows it (ADR 0023). Each connection is its
        # own instance with its own cgroup: a body of up to 32 MB is held (twice, while it is cut
        # into the request), so 256M each; the route is one thread, 8 tasks are ample
        names["claude.socket"]: f"""[Unit]
Description=Anthropic route for VM {vm}

[Socket]
ListenStream={host}:9101
FreeBind=yes
Accept=yes
MaxConnections=65

[Install]
WantedBy=sockets.target
""",
        names["claude@.service"]: f"""[Unit]
Description=Anthropic request of VM {vm}

[Service]
ExecStart={paths.CLAUDE_ROUTE} {vm}
User={activity.CLAUDE_USER}
{journal_access(activity.CLAUDE_USER)}StandardInput=socket
StandardOutput=socket
StandardError=journal
SyslogIdentifier=agentbox-claude-{vm}
LogNamespace={activity.NAMESPACE}
RuntimeDirectory=agentbox-claude/{vm}
RuntimeDirectoryMode=0700
RuntimeDirectoryPreserve=yes
MemoryMax=256M
TasksMax=8
CPUQuota=50%
{hardening}{CLAUDE_HARDENING}""",
        names["gui.socket"]: f"""[Unit]
Description=Orca access to VM {vm}

[Socket]
ListenStream={hostsetup.port_listen(setup, port)}
Accept=yes
MaxConnections=32

[Install]
WantedBy=sockets.target
""",
        names["gui@.service"]: f"""[Unit]
Description=Orca connection to VM {vm}

[Service]
ExecStartPre={paths.CLI} "--actor=Orca (${{REMOTE_ADDR}})" wake {vm}
ExecStart=/usr/bin/socat STDIO TCP:{guest_ip(rec)}:{port}
StandardInput=socket
StandardOutput=socket
StandardError=journal
TimeoutStartSec=180
""",
        **({names["ssh.socket"]: f"""[Unit]
Description=SSH access to VM {vm}

[Socket]
ListenStream={hostsetup.port_listen(setup, registry.ssh_port(rec))}
Accept=yes
MaxConnections=32

[Install]
WantedBy=sockets.target
""",
            names["ssh@.service"]: f"""[Unit]
Description=SSH connection to VM {vm}

[Service]
ExecStartPre={paths.CLI} "--actor=SSH (${{REMOTE_ADDR}})" wake {vm}
ExecStart=/usr/bin/socat STDIO TCP:{guest_ip(rec)}:22,retry=20,interval=0.5
StandardInput=socket
StandardOutput=socket
StandardError=journal
TimeoutStartSec=180
"""} if registry.ssh_access(rec) else {}),
    }


def virt_install_args(rec, uuid):
    """virt-install only prints the domain (--dry-run creates nothing). It insists
    on a size for disks that do not exist yet; size=1 only satisfies that check."""
    vm = rec["name"]
    return [
        *domain_args.base(dom(vm), rec["memory_mib"], VCPUS, uuid=uuid),
        # the VM learns its name, Orca port, Claude placeholder, the address devices pair with, its
        # VM variables, its SSH access and its secrets at boot; values that could hold a comma
        # only as files (virt-install splits --sysinfo at commas)
        "--sysinfo", f"type=fwcfg,entry0.name=opt/agentbox/context,entry0={vm},"
                     f"entry1.name=opt/agentbox/orca_port,entry1={rec['orca_port']},"
                     f"entry2.name=opt/agentbox/claude_placeholder,"
                     f"entry2.file={footprint.claude_placeholder_fw_cfg_file(vm)},"
                     f"entry3.name=opt/agentbox/pairing_address,"
                     f"entry3={hostsetup.pairing_address(hostsetup.load())},"
                     f"entry4.name=opt/agentbox/env,entry4.file={footprint.vm_env_file(vm)},"
                     f"entry5.name=opt/agentbox/ssh,entry5.file={footprint.vm_ssh_file(vm)},"
                     f"entry6.name=opt/agentbox/secrets,entry6.file={footprint.vm_secrets_file(vm)}",
        "--disk", domain_args.disk(footprint.system_disk(vm), "vda", size=1),
        "--disk", domain_args.disk(footprint.data_disk(vm), "vdb", size=1) + ",serial=agentbox-data",
        "--network", f"network={dom(vm)},mac={mac(rec)},model=virtio",
        "--serial", "pty", "--console", "pty",
        "--channel", "unix,target.type=virtio,target.name=org.qemu.guest_agent.0",
        "--memballoon", "model=virtio,freePageReporting=on",
        "--rng", "/dev/urandom",
    ]


def _pending(what):
    """Marker: `what` was written but is not applied yet."""
    return os.path.join(paths.STATE, "pending", what)


def is_pending(what):
    return os.path.exists(_pending(what))


def mark_pending(pending):
    state_files.directory(os.path.dirname(_pending(pending)), 0o700, only_new=True)
    open(_pending(pending), "w").close()


def _write_if_changed(path, data, mode, pending):
    """Write when different; the pending marker comes first, so a run that dies
    before applying leaves it for the next run."""
    return state_files.write_if_changed(path, data, mode, before=lambda: mark_pending(pending), fail=False)


def apply_pending(what, *systemctl, timeout=120):
    """systemctl with these arguments when `what` is pending, then the marker goes;
    whether it ran."""
    if not os.path.exists(_pending(what)):
        return False
    programs.systemctl(*systemctl, timeout=timeout)
    os.remove(_pending(what))
    return True


def virsh_checked(what, *args):
    r = virsh.run(*args)
    if r.returncode != 0:
        programs.fail(r, what, program="virsh")


def _virsh_define(cmd, xml, what):
    with tempfile.NamedTemporaryFile("w", suffix=".xml") as f:
        f.write(xml)
        f.flush()
        virsh_checked(what, cmd, f.name)


def _qemu():
    """(uid, gid) of QEMU's user as root, else (-1, -1)."""
    return state_files.user_ids(hostfacts.QEMU_USER, "is libvirt installed?")


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


def _network(rec):
    """The VM's network created, brought to the current definition, autostarted and started:
    True or the key of what it said, else False. libvirt's dnsmasq reads its options only when
    the network starts, so an active network whose resolver is not the current one is
    stopped and started again."""
    net = dom(rec["name"])
    try:
        found = states.network(rec["name"])
    except Fail:                # virsh cannot tell: defined as if there were none
        found = None
    changed = False
    if found is not None:
        active, autostart = found
        xml = _network_matches(rec)
        if not _resolver_current(rec, xml):
            current = re.search(r"<uuid>([^<]+)</uuid>", xml)
            _virsh_define("net-define", network_xml(rec, current.group(1) if current else None),
                          f"{rec['name']}: define network")
            changed = "network-redefined"
            if active:
                live = states.has_process(rec["name"])
                virsh_checked(f"{rec['name']}: stop network", "net-destroy", net)
                virsh_checked(f"{rec['name']}: start network", "net-start", net)
                changed = "network-redefined-running" if live else changed
    else:
        _virsh_define("net-define", network_xml(rec), f"{rec['name']}: create network")
        active = autostart = False
        changed = True
    if not autostart:
        virsh_checked(f"{rec['name']}: autostart network", "net-autostart", net)
        changed = changed or True
    if not active:
        virsh_checked(f"{rec['name']}: start network", "net-start", net)
        changed = changed or True
    return changed


def _units(rec):
    """The VM's units written. A socket unit that existed with other text is marked, before
    it is written, to be listened on again (_rebind). Without SSH access the units of the SSH
    port go, its socket stopped first, so the port closes."""
    changed = False
    texts = units(rec)
    for kind in footprint.SSH_UNIT_KINDS:
        name = footprint.unit_name(rec["name"], kind)
        if name in texts or not os.path.exists(footprint.unit_file(name)):
            continue
        if name.endswith(".socket"):
            programs.systemctl("disable", "--now", name)
            state_files.remove(footprint.socket_pending_file(name))
        mark_pending("units")
        state_files.remove(footprint.unit_file(name))
        changed = True
    for name, text in texts.items():
        path = footprint.unit_file(name)
        if name.endswith(".socket"):
            try:
                with open(path, "rb") as f:
                    if f.read() != text.encode():
                        footprint.mark_socket(name)
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
        if not name.endswith(".socket") or not os.path.exists(footprint.socket_pending_file(name)):
            continue
        if "Accept=yes" not in text:
            programs.systemctl("stop", name[:-len(".socket")] + ".service")
        programs.systemctl("restart", name)
        os.remove(footprint.socket_pending_file(name))
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
    virsh_checked("autostart storage pool", "pool-autostart", POOL)
    virsh_checked("start storage pool", "pool-start", POOL)
    return True


def _data_disk(rec):
    """Created once and kept: repositories, Orca's identity and pairings live there."""
    path = footprint.data_disk(rec["name"])
    if os.path.exists(path):
        return False
    programs.run(["qemu-img", "create", "-q", "-f", "qcow2", path, f"{rec['data_disk_gib']}G"], 120,
                 what=f"{rec['name']}: create Data disk", c_locale=True)
    uid, gid = _qemu()
    if uid != -1:
        os.chown(path, uid, gid)
    return True


def _claude_file(path, data, uid, gid):
    """One of the route's files, written when missing, with owner and mode 0400 applied every
    time, its content kept. Whether something was done."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        state_files.write_atomic(path, data, 0o400, gid, uid)
        return True
    except OSError as e:
        die(f"{path}: {e.strerror}", error="action_failed")
    if uid in (-1, st.st_uid) and gid in (-1, st.st_gid) and stat.S_IMODE(st.st_mode) == 0o400:
        return False
    try:
        os.chown(path, uid, gid)      # -1: unchanged (not root)
        os.chmod(path, 0o400)
    except OSError as e:
        die(f"{path}: {e.strerror}", error="action_failed")
    return True


def _claude(rec):
    """What the VM's Anthropic route needs (ADR 0023): its placeholder, made once with 32 random
    bytes and kept until the VM is deleted, and its token file, `none` until a token is stored;
    both owned by the route's user, mode 0400, in a directory of its group with mode 0750. The
    placeholder also goes into the file QEMU passes in, where `guest-setup` finds it; the token
    never does. Whether something was made or written."""
    from .credentials import claude_route_ids
    name = rec["name"]
    uid, gid = claude_route_ids()
    try:
        state_files.directory(footprint.CLAUDE_DIR, 0o750, gid)
    except OSError as e:
        die(f"{footprint.CLAUDE_DIR}: {e.strerror}", error="action_failed", vm=name)
    placeholder = _placeholder(name)
    if placeholder is None:
        placeholder = b"agentbox-claude-" + base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=")
        state_files.write_atomic(footprint.claude_placeholder_file(name), placeholder + b"\n", 0o400, gid, uid)
        made = True
    else:
        made = _claude_file(footprint.claude_placeholder_file(name), b"", uid, gid)
    made = _claude_file(footprint.claude_token_file(name), b"none\n", uid, gid) or made
    return fw_cfg_file(footprint.claude_placeholder_fw_cfg_file(name), placeholder + b"\n") or made


PLACEHOLDER = re.compile(rb"agentbox-claude-[A-Za-z0-9_-]{43}")


def _placeholder(name):
    """The VM's placeholder, None when it has none (yet) or the file holds no placeholder."""
    try:
        with open(footprint.claude_placeholder_file(name), "rb") as f:
            data = f.read(200).strip()
    except FileNotFoundError:
        return None
    except OSError as e:
        die(f"{e.filename}: {e.strerror}", error="action_failed", vm=name)
    return data if PLACEHOLDER.fullmatch(data) else None


def fw_cfg_file(path, data):
    """Write a file QEMU passes into a VM as an fw_cfg entry, when its content or mode differ:
    owner root, group qemu, mode 0640, in a directory of the same group with mode 0750. Whether
    it was written."""
    gid = _qemu()[1]
    state_files.directory(os.path.dirname(path), 0o750, gid)
    return state_files.write_if_changed(path, data, 0o640, gid, fail=False)


def vm_env(rec):
    """The file of the VM's VM variables (vmenv.py), from its record; a VM picks a change up at
    its next start. Written before the domain is defined, which names it."""
    return fw_cfg_file(footprint.vm_env_file(rec["name"]), vmenv.fw_cfg(rec.get("env", {})))


def secrets_file(rec, values):
    """The content of the VM's secrets file: these values of the secrets its record names, in
    their order (vmenv.fw_cfg: "none" without any, never a refused name)."""
    return vmenv.fw_cfg({n: values[n] for n in registry.secrets(rec) if n in values})


def read_secrets(name):
    """The values in the VM's secrets file (vmenv.parse); {} without the file."""
    try:
        with open(footprint.vm_secrets_file(name), "rb") as f:
            return vmenv.parse(f.read())
    except FileNotFoundError:
        return {}


def vm_secrets(rec):
    """The file of the VM's secrets, the only place their values are kept: created as "none"
    when it is missing, else written again only to leave
    out what the record no longer names or a secret may no longer have (and to fix its mode).
    No value ever comes from the record. Written before the domain is defined, which names it."""
    try:
        values = read_secrets(rec["name"])
    except OSError as e:
        die(f"{rec['name']}: {e.filename}: {e.strerror}", error="action_failed", vm=rec["name"])
    return fw_cfg_file(footprint.vm_secrets_file(rec["name"]), secrets_file(rec, values))


# the DER of an Ed25519 key as openssl writes it: PKCS#8 ending in the key's 32-byte secret,
# and the SubjectPublicKeyInfo ending in its 32-byte public key
ED25519_PRIVATE = bytes.fromhex("302e020100300506032b657004220420")
ED25519_PUBLIC = bytes.fromhex("302a300506032b6570032100")


def _der(pem):
    """The DER of the one PEM block openssl wrote; b"" when there is none."""
    lines = pem.strip().splitlines()
    try:
        return base64.b64decode("".join(lines[1:-1]), validate=True)
    except ValueError:
        return b""


def ssh_host_key(rec):
    """The VM's SSH host key, made once and kept (ADR 0020): an Ed25519 key whose secret
    openssl makes, in OpenSSH's own format; owner root, group qemu, mode 0640, beside the
    VM's secrets. Made when SSH access is first turned on or a first SSH key is added;
    regenerate makes one that is missing while the VM has SSH access or SSH keys. Its public
    half goes beside, readable by everyone (footprint.ssh_host_key_public_file), also for a key
    made before. Whether the key was made."""
    path = footprint.ssh_host_key_file(rec["name"])
    made = not os.path.exists(path)
    if made:
        what = f"{rec['name']}: making the SSH host key"
        pem = programs.run(["openssl", "genpkey", "-algorithm", "ed25519"], 30, what=what, c_locale=True).stdout
        public = programs.run(["openssl", "pkey", "-pubout"], 30, what=what, c_locale=True, input=pem).stdout
        secret, public = _der(pem), _der(public)
        if not (len(secret) == 48 and secret.startswith(ED25519_PRIVATE)
                and len(public) == 44 and public.startswith(ED25519_PUBLIC)):
            die(f"{what}: openssl gave no Ed25519 key", error="action_failed", vm=rec["name"])
        gid = _qemu()[1]
        state_files.directory(os.path.dirname(path), 0o750, gid)
        state_files.write_atomic(path, sshkeys.host_key_file(secret[16:], public[12:], sshkeys.alias(rec["name"])),
                                 0o640, gid)
    blob = sshkeys.host_public(host_key(rec["name"]) or b"")
    if blob:
        public_path = footprint.ssh_host_key_public_file(rec["name"])
        state_files.directory(os.path.dirname(public_path), 0o755, only_new=True)
        state_files.write_if_changed(public_path, (sshkeys.public_line(blob) + "\n").encode(), 0o644, fail=False)
    return made


def host_key(name):
    """The VM's SSH host key file as bytes; None when it has none (yet)."""
    try:
        with open(footprint.ssh_host_key_file(name), "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def vm_ssh(rec):
    """The file of the VM's SSH access (sshkeys.fw_cfg) from its record and its host key, which
    is made here when the VM has SSH access or SSH keys and none yet (a VM created with keys
    copied from another); a VM picks a change up at its next start. Written before the domain
    is defined, which names it. False, or what was done: "ssh-host-key" (made, and the file
    written) or "vm-ssh" (the file written)."""
    made = (registry.ssh_access(rec) or bool(registry.ssh_keys(rec))) and ssh_host_key(rec)
    data = sshkeys.fw_cfg(registry.ssh_access(rec), registry.ssh_keys(rec), host_key(rec["name"]) or b"")
    written = fw_cfg_file(footprint.vm_ssh_file(rec["name"]), data)
    return "ssh-host-key" if made else "vm-ssh" if written else False


def ssh_port(rec):
    """The units of the VM's SSH port as its SSH access says (regenerate's step for this VM's
    units): written, loaded and listening while it has SSH access, else stopped and gone.
    In Network mode the firewall rules of the SSH ports are added first where missing
    (firewall.ssh_ports). Callers hold state_files.locked(paths.REGISTRY). Whether something
    changed."""
    if registry.ssh_access(rec):
        firewall.ssh_ports(hostsetup.load())
    changed = _units(rec)
    apply_pending("units", "daemon-reload")
    changed = _rebind(rec) or changed
    if registry.ssh_access(rec):
        programs.systemctl("enable", "--now", footprint.unit_name(rec["name"], "ssh.socket"))
    return changed


def _observer(rec):
    return state_files.directory(footprint.observer_dir(rec["name"]), 0o700, only_new=True)


def domain(rec):
    """Defined again whenever virt-install's output changed; a running VM picks
    it up at its next start (libvirt keeps the new definition for then). Not while
    paused: its saved memory belongs to the old definition. "domain",
    "domain-postponed" or None (unchanged). Callers hold state_files.locked(paths.REGISTRY)."""
    vm = rec["name"]
    r = virsh.run("domuuid", dom(vm))
    uuid = r.stdout.strip() if r.returncode == 0 and UUID.fullmatch(r.stdout.strip()) else None
    xml = programs.run(["virt-install", *virt_install_args(rec, uuid)], 120, what=f"{vm}: virt-install",
                       c_locale=True).stdout
    saved = footprint.domain_file(vm)
    try:
        with open(saved) as f:
            if uuid and f.read() == xml:
                return None
    except FileNotFoundError:
        pass
    if uuid and states.has_pause(vm):
        return "domain-postponed"
    _virsh_define("define", xml, f"{vm}: define VM")
    state_files.directory(os.path.dirname(saved), 0o700, only_new=True)
    state_files.write_atomic(saved, xml.encode(), 0o600)
    return "domain"


def _activity_journal(days):
    """The journald namespace of Activity, keeping entries for `days` days; its
    journald picks a changed retention up on restart (it keeps the log streams
    of the services meanwhile). Returns the days when they changed, else None."""
    _write_if_changed(activity.JOURNALD_CONF, activity.journald_conf(days).encode(), 0o644, "journald")
    changed = apply_pending("journald", "try-restart", f"{JOURNALD}.service")
    programs.systemctl("enable", "--now", f"{JOURNALD}.socket")
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

# A Build as a unit, its journal is its log (ADR 0004); agentbox build starts
# it, agentbox-build@all builds the Base image and then all other Images
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
# the teardown of a running build VM, a minute to spare
TimeoutStopSec={build_vm_run.TEARDOWN_SECONDS + 60}s
"""


VERSIONS = "agentbox-versions"
VERSIONS_FETCH = "agentbox-versions-fetch"

# What a new Build would bring (versions.py), in two parts: the query to npm, GitHub, quay.io
# and fedoraproject.org (with the download of the build VMs' image, build_vm.py) as
# agentbox-feeds, without access to keys, tokens, VMs and Builds; then as root only what root
# needs (package updates in a container of the Build, pulling and pinning the digests a Build
# uses, digests.py, checking the build VMs' image), which reads the answer as untrusted input
VERSIONS_FETCH_SERVICE = """\
# Generated by agentbox regenerate (package_min_age_days, orca_version and
# playwright_version in /etc/agentbox/settings.json)
[Unit]
Description=Agent Box: ask npm, GitHub, quay.io and Fedora for the versions available
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=agentbox-feeds
ExecStart={program} fetch-versions
Environment=AGENTBOX_VERSIONS_MIN_AGE_DAYS={days}
Environment=AGENTBOX_VERSIONS_ORCA_VERSION={orca}
Environment=AGENTBOX_VERSIONS_PLAYWRIGHT_VERSION={playwright}
SyslogIdentifier=agentbox-versions-fetch
# writes only /var/lib/agentbox/available (systemd creates it for the user)
StateDirectory=agentbox/available
StateDirectoryMode=0755
UMask=0022
# of /var/lib/agentbox only available/ (allowlist); nothing of /etc/agentbox, libvirt, podman
TemporaryFileSystem=/var/lib/agentbox:ro
BindPaths=/var/lib/agentbox/available
# which build VM image is in use, so the fetch downloads only a new one (none before the first)
BindReadOnlyPaths=-/var/lib/agentbox/build-vm/image.json
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
# a new build VM image is about 0.6 GB (curl's own limit: 40 minutes)
TimeoutStartSec={timeout}s
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

# The Maintenance window (#49): Feeds, version check, weekly rebuild and Reset check
# one after another, each step waits for the previous one (maintenance.py)
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
        programs.systemctl("daemon-reload")
        programs.systemctl("restart", f"{FEEDS}.timer")
        os.remove(_pending("timers"))
    programs.systemctl("enable", "--now", f"{FEEDS}.timer")
    return changed


def _builds():
    """The unit of the Builds. Returns whether it changed."""
    if os.path.exists(_pending("rebuild")):        # its marker before the Maintenance window (#49)
        mark_pending("builds")
        os.remove(_pending("rebuild"))
    _write_if_changed(os.path.join(paths.SYSTEMD, BUILD_UNIT), BUILD_SERVICE.encode(), 0o644, "builds")
    return apply_pending("builds", "daemon-reload")


def _versions(values):
    """The version check, which the Maintenance window starts: the fetch as agentbox-feeds (with
    package_min_age_days, orca_version and playwright_version of the Settings), then the root
    part. A changed service is loaded and checks at once (with auto_update, an update may start
    a Build then). Returns whether something changed."""
    services = {f"{VERSIONS_FETCH}.service": VERSIONS_FETCH_SERVICE.format(program=paths.CLI,
                                                                            days=values["package_min_age_days"],
                                                                            orca=values["orca_version"],
                                                                            playwright=values["playwright_version"],
                                                                            timeout=versions.FETCH_SECONDS),
                f"{VERSIONS}.service": VERSIONS_SERVICE}
    for name, text in services.items():
        _write_if_changed(os.path.join(paths.SYSTEMD, name), text.encode(), 0o644, "versions")
    changed = os.path.exists(_pending("versions"))
    if changed:
        programs.systemctl("daemon-reload")
        programs.systemctl("start", "--no-block", f"{VERSIONS}.service")
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
        programs.systemctl("stop", f"{MAINTENANCE}.timer")
        programs.systemctl("daemon-reload")
        os.makedirs(os.path.dirname(MAINTENANCE_STAMP), exist_ok=True)
        with open(MAINTENANCE_STAMP, "a"):
            pass
        os.utime(MAINTENANCE_STAMP)
        programs.systemctl("start", f"{MAINTENANCE}.timer")
        os.remove(_pending("maintenance-timer"))
    elif service:
        programs.systemctl("daemon-reload")
    if service:
        os.remove(_pending("maintenance"))
    state_files.remove(_pending("versions-timer"), fail=False)      # a marker of a retired timer
    programs.systemctl("enable", "--now", f"{MAINTENANCE}.timer")
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
        programs.systemctl("disable", "--now", *timers)
    for name in timers + services:
        state_files.remove(os.path.join(paths.SYSTEMD, name), fail=False)
    programs.systemctl("daemon-reload")
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


def listens_on(port):
    """Whether a TCP socket listens on the port at any address of the Host, from /proc/net."""
    for table in ("tcp", "tcp6"):
        try:
            with open(paths.host(f"/proc/net/{table}")) as f:
                rows = f.read().splitlines()[1:]
        except FileNotFoundError:
            continue
        for row in rows:
            fields = row.split()
            if len(fields) >= 4 and fields[3] == "0A":      # 0A: LISTEN
                try:
                    if int(fields[1].rpartition(":")[2], 16) == port:
                        return True
                except ValueError:
                    continue
    return False


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
    {"vms": {vm: [step, ...]}, "firewall": bool, "ssh_rules": int, "storage": bool,
    "retention": days or None, "timers": bool, "build_network_removed": bool}: "firewall" whether
    the VM networks' rules were loaded, "ssh_rules" how many firewall rules of the SSH ports were
    added (Network mode), "build_network_removed" whether the build VMs' network went (build_vm
    off).
    Callers hold state_files.locked(paths.REGISTRY), like create and delete, so runs never overlap."""
    records = registry.all_records()     # a broken registry stops everything before any change
    values = settings.load()             # so do broken settings
    ports_free()                         # so does a service that would keep resolver or proxy from starting
    # in Network mode the SSH ports listen on every address: their firewall rules come first
    ssh_rules = firewall.ssh_ports(hostsetup.load())
    days = values["activity_retention_days"]
    # a VM being deleted stays isolated until its network is gone
    isolated = records + [m["record"] for m in footprint.being_deleted() if not m.get("network_removed")]
    _write_if_changed(VM_RULES,
                      nft_rules(sorted(isolated, key=lambda r: r["network"])).encode(), 0o644, "firewall")
    nft = apply_pending("firewall", "restart", VMNET)     # before any new bridge comes up
    retention = _activity_journal(days)     # before units that log into it start
    storage = _storage_pool() if records else False
    changes = {}
    for rec in records:
        done = []
        for key, step in (("network", _network), ("units", _units), ("data-disk", _data_disk),
                          ("claude", _claude), ("vm-env", vm_env), ("vm-secrets", vm_secrets),
                          ("vm-ssh", vm_ssh),
                          ("observer", _observer)):
            got = step(rec)
            if got:
                done.append(got if isinstance(got, str) else key)
        defined = domain(rec)
        if defined:
            done.append(defined)
        changes[rec["name"]] = done
    # the build VMs' network exists only while build_vm is on; a build VM's start makes it
    # (best effort: libvirt failing here says so and fails nothing; the next apply tries again)
    try:
        build_network = False if values["build_vm"] else build_vm_run.remove_network()
    except Fail as f:
        build_network = False
        print(f"agentbox: the network {build_vm_run.NETWORK} of the build VMs was not removed: {f.message}; "
              "the next regenerate tries again", file=sys.stderr)
    timers = _feeds(values)
    timers = _builds() or timers
    timers = _versions(values) or timers
    timers = _maintenance(values) or timers
    apply_pending("units", "daemon-reload")
    for rec in records:
        if _rebind(rec):
            changes[rec["name"]].append("sockets")
    if records:
        programs.systemctl("enable", "--now", *[s for rec in records for s in footprint.sockets(rec)])
    return {"vms": changes, "firewall": nft, "ssh_rules": ssh_rules, "storage": storage,
            "retention": retention, "timers": timers, "build_network_removed": build_network}


def report(result):
    """The lines saying what apply() changed (none when nothing changed)."""
    lines = [f"{vm}: {SAID[key]}" for vm, done in result["vms"].items() for key in done]
    if result["firewall"]:
        lines.append("network rules reloaded")
    if result["ssh_rules"]:
        lines.append(f"{result['ssh_rules']} firewall rules of the SSH ports added for the home network")
    if result["storage"]:
        lines.append(f"storage pool {POOL} for {paths.VMS} created")
    if result["retention"]:
        lines.append(f"Activity is kept for {result['retention']} days")
    if result["timers"]:
        lines.append("timers and services for the Maintenance window, lists, Builds and versions written again "
                     "and restarted")
    if result.get("build_network_removed"):
        lines.append(f"network {build_vm_run.NETWORK} of the build VMs removed (build_vm is off)")
    return lines


def regenerate(args):
    """agentbox regenerate [--json]: the Host configuration of every VM from the registry, the
    ports of the firewalld zone libvirt the VMs reach (as setup opens them) and, in Network
    mode, the firewall rules of the SSH ports, so this command alone brings a Host's VMs up to
    date with an installed Release."""
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


DECLARATIONS = {
    "migrate": declare(migrate, "agentbox migrate [--json]", options={"json": False},
                       describe=lambda a: activity.said("Take over work and priv into the VM registry")),
    "regenerate": declare(regenerate, "agentbox regenerate [--json]", options={"json": False},
                            describe=lambda a: activity.said("Generate the configuration from the VM registry")),
}
