"""`agentbox ssh-config`: a ~/.ssh/config block per VM with SSH access, and the known_hosts
lines of their host keys (ADR 0020).

Each VM goes by its alias `<vm>.agentbox` (sshkeys.alias): the block's Host and its
HostKeyAlias, so known_hosts names the alias, not the address and port, and switching the
Host between Local-only and Network mode needs no new known_hosts line. HostName is where the
SSH port is reached for the Host setup's mode: 127.0.0.1 in Local-only, the Pairing address in
Network mode. The block keeps the client from offering the VM what its sshd cannot be trusted
to refuse (ForwardAgent, ForwardX11) and from talking to anything without the host key from
the Host (StrictHostKeyChecking, its own UserKnownHostsFile).

Agent Box never writes into a home: the owner redirects the output into files of their own
and includes the block. Root and the members of libvirt may run it (callers.py); it reads the
records, the Host setup and the public halves of the host keys, which everyone may read.
"""
import base64
import json
import sys

from . import callers, footprint, hostsetup, options, registry, sshkeys
from .commands import declare
from .errors import Fail, die

USER = "agent"
KNOWN_HOSTS_FILE = "~/.ssh/known_hosts.agentbox"


def _host_key(name):
    """The public key blob of the VM's host key, None when there is none (yet)."""
    try:
        with open(footprint.ssh_host_key_public_file(name), encoding="ascii") as f:
            text = f.read(4096)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        die(f"{footprint.ssh_host_key_public_file(name)}: not readable ({getattr(e, 'strerror', None) or e})",
            error="action_failed", vm=name)
    try:
        key = sshkeys.parse(text)
    except Fail:
        die(f"{footprint.ssh_host_key_public_file(name)}: not an SSH public key; agentbox regenerate writes it again",
            error="action_failed", vm=name)
    return base64.b64decode(key["key"].split()[1])


def host(rec, setup):
    """The VM's block as (alias, options in their order, the known_hosts line or None)."""
    name, alias = rec["name"], sshkeys.alias(rec["name"])
    where = hostsetup.LOOPBACK if setup["mode"] == hostsetup.LOCAL else hostsetup.pairing_address(setup)
    settings = {"HostName": where, "Port": registry.ssh_port(rec), "User": USER, "HostKeyAlias": alias,
                "ForwardAgent": "no", "ForwardX11": "no", "StrictHostKeyChecking": "yes",
                "UserKnownHostsFile": KNOWN_HOSTS_FILE}
    blob = _host_key(name)
    return alias, settings, sshkeys.known_hosts_line(name, blob) if blob else None


def block(alias, settings):
    return f"Host {alias}\n" + "".join(f"    {k} {v}\n" for k, v in settings.items())


def _records(names):
    """The records of the VMs named, else of every VM; broken records go to stderr."""
    if names:
        return [registry.load(n) for n in dict.fromkeys(names)]
    records, problems = registry.scan()
    for _, problem in problems:
        print(f"agentbox: {problem}", file=sys.stderr, flush=True)
    return records


def ssh_config(args):
    """agentbox ssh-config [<vm>...] [--known-hosts] [--json]: the ~/.ssh/config block of each
    VM with SSH access (of those named, else of all); with --known-hosts their known_hosts
    lines instead."""
    callers.check("ssh-config")
    positional, opts = options.parse(args)
    setup = hostsetup.load()
    hosts = []
    for rec in _records(positional):
        if not registry.ssh_access(rec):
            if positional:
                print(f"agentbox: {rec['name']} has no SSH access (sudo agentbox set-ssh {rec['name']} on)",
                      file=sys.stderr)
            continue
        alias, settings, known = host(rec, setup)
        if known is None:
            print(f"agentbox: {rec['name']} has no host key yet (sudo agentbox regenerate)", file=sys.stderr)
        hosts.append({"vm": rec["name"], "host": alias, "options": settings, "config": block(alias, settings),
                      "known_hosts": known, "command": f"ssh {alias}"})
    if not hosts and not positional:
        print("agentbox: no VM has SSH access (sudo agentbox set-ssh <vm> on)", file=sys.stderr)
    if "json" in opts:
        print(json.dumps({"mode": setup["mode"], "hosts": hosts}))
    elif "known-hosts" in opts:
        print("".join(f"{h['known_hosts']}\n" for h in hosts if h["known_hosts"]), end="")
    else:
        print("\n".join(h["config"] for h in hosts), end="")


DECLARATIONS = {
    "ssh-config": declare(ssh_config, "agentbox ssh-config [<vm>...] [--known-hosts] [--json]",
                          options={"known-hosts": False, "json": False}, needs_root=False, read_only=True,
                          control_room=True, option_exit=1),
}
