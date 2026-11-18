"""firewalld, as agentbox setup and regenerate change it: the libvirt zone of the VM networks
and the rules of Network mode (ADR 0009), the SSH ports' among them (ADR 0020).

ensure() makes a zone's permanent configuration hold what it should and reloads only when it
changed something or the running firewall still differs from it, so running setup again calls
firewall-cmd only to ask.

In Network mode rich rules of the default zone accept the home network on the Control Room's
port, the Orca ports and the SSH ports, and reject everything else there, over IPv4 and IPv6.
setup sets them all and, once no port listens on every address any more, takes back those it
added or replaced that the recorded Host setup does not need (setup.py); regenerate (and every
command that writes a VM's units) adds the SSH ports' rules, so an update that brings them
needs no setup. In Local-only there is no rule and nothing is called.
"""
from . import hostsetup, programs

# Orca port = 6800 + network number (1-254). The whole range is let through so a new VM
# (agentbox create) needs no firewall change; only what a VM in the registry listens on
# answers. Everything else on Orca ports, also the desktop runtime (6768-6799, 16768), is
# rejected, like the Control Room's port.
ORCA_ACCEPT = "6801-7054"
ORCA_REJECT = ("6768-7054", "16768")
# SSH port = 2200 + network number (registry.SSH_BASE), the whole range as for the Orca ports:
# turning SSH access on needs no firewall change.
SSH_PORTS = "2201-2454"


def _cmd(*args, ok=(0,)):
    return programs.run(["firewall-cmd", *args], 120, ok=ok)


def default_zone():
    return _cmd("--get-default-zone").stdout.strip() or "public"


def _has(zone, kind, value, permanent):
    """Whether the zone has it, permanently or in the running firewall. firewall-cmd answers
    0 (yes) or 1 (no); anything else (no such zone, firewalld not running) stops."""
    args = [*(["--permanent"] if permanent else []), f"--zone={zone}", f"--query-{kind}={value}"]
    return _cmd(*args, ok=(0, 1)).returncode == 0


def ensure(zone, add=(), remove=()):
    """The zone with every (kind, value) of add and none of remove; kind is rich-rule, port or
    service. Returns what it changed as (verb, kind, value)."""
    changed, behind = [], False
    for wanted, items in ((True, add), (False, remove)):
        for kind, value in items:
            if _has(zone, kind, value, True) != wanted:
                verb = "add" if wanted else "remove"
                args = ["--permanent", f"--zone={zone}", f"--{verb}-{kind}={value}"]
                _cmd(*args)
                changed.append((verb, kind, value))
            elif _has(zone, kind, value, False) != wanted:
                behind = True       # permanent already, but not loaded (an earlier run stopped)
    if changed or behind:
        _cmd("--reload")
    return changed


def _rich(setup, accept, reject):
    net = setup["home_network"]
    return ([f'rule priority="-110" family="ipv4" source address="{net}" port port="{p}" protocol="tcp" accept'
             for p in accept],
            [f'rule priority="-100" family="{fam}" port port="{p}" protocol="tcp" reject'
             for p in reject for fam in ("ipv4", "ipv6")])


def home_network_rules(setup):
    """(accept, reject) rich rules of a Host setup: the Control Room's port, the Orca ports and
    the SSH ports; none in Local-only."""
    if setup["mode"] != hostsetup.NETWORK:
        return [], []
    web = str(setup["web_port"])
    return _rich(setup, (web, ORCA_ACCEPT, SSH_PORTS), (web, *ORCA_REJECT, SSH_PORTS))


def ssh_rules(setup):
    """(accept, reject) rich rules of a Host setup for the SSH ports; none in Local-only."""
    if setup["mode"] != hostsetup.NETWORK:
        return [], []
    return _rich(setup, (SSH_PORTS,), (SSH_PORTS,))


def ssh_ports(setup):
    """The SSH ports' rules of a Host setup in the default zone, before an SSH port may listen
    on every address: in Network mode they are added where missing, in Local-only nothing is
    called. How many it added."""
    accept, reject = ssh_rules(setup)
    if not accept:
        return 0
    changed = ensure(default_zone(), add=[("rich-rule", r) for r in accept + reject])
    return len(changed)
