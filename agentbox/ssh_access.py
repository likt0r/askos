"""SSH access to a VM (ADR 0020): `agentbox ssh`, `set-ssh` and `add-ssh-key`.

The record holds whether the VM has SSH access and its SSH keys (registry.py, sshkeys.py);
the Host keeps the VM's SSH host key, made once (generate.ssh_host_key). A change is saved
in the record, then the file QEMU passes into the VM is written (generate.vm_ssh), the VM is
defined again when its definition changed (a domain from before SSH access lacks the fw_cfg
entry), and the units of its SSH port follow its SSH access (generate.ssh_port): the port
listens while it is on and is gone while it is off. Turning it off closes the port first, so
a later step that fails leaves it closed. The VM gets its keys and starts sshd at
its next start, so a running or paused VM at its next Restart or Reset; the keys stay when SSH
access goes off.

Each command that changes something holds the registry lock, the record's lock and the VM's
lifecycle lock, as set-env does, and records its own Activity entry (ssh.enabled,
ssh.disabled, ssh.key-added with the key's fingerprint and name), failures included: also a
wrong argument, an unknown VM or a broken record (set-ssh with a word other than "off" counts
as turning it on).
"""
import json
import sys
import time

from . import activity, footprint, generate, hostsetup, options, paths, registry, sshkeys, state_files, states, times
from .commands import declare
from .errors import Fail, die
from .sanitize import clean


def _change(name, change):
    """Apply change(record) -> record: saved when it differs, then the host key made (with SSH
    access or a key), the file the VM reads written, the VM defined again if its definition
    changed, and the SSH port set up or taken away: without SSH access first, so that it is
    closed even when a later step fails. Returns (before, after, state, applies, defined),
    applies as for VM variables: next_start (off), next_restart (running or paused), None
    when nothing changed."""
    with state_files.locked(paths.REGISTRY):
        with state_files.locked(footprint.record_file(name)):
            with state_files.vm_lock(name):
                before = registry.load(name)
                after = change(before)
                if registry.ssh_access(after) and not registry.ssh_access(before) and \
                        generate.listens_on(registry.ssh_port(after)):
                    where = "" if hostsetup.load()["mode"] == hostsetup.NETWORK else f" on {hostsetup.LOOPBACK}"
                    die(f"{name}: port {registry.ssh_port(after)}{where} is taken: another service listens there, "
                        "so the VM's SSH port could not open; stop that service first", error="action_failed", vm=name)
                if after != before:
                    registry.save(after)
                on = registry.ssh_access(after)
                try:
                    port = not on and generate.ssh_port(after)
                    made = (on or bool(registry.ssh_keys(after))) and generate.ssh_host_key(after)
                    written = generate.vm_ssh(after)
                    defined = generate.domain(after)
                    port = generate.ssh_port(after) if on else port
                except (Fail, OSError) as e:
                    why = e.message.strip() if isinstance(e, Fail) else f"{e.filename}: {e.strerror}"
                    said = "the change is saved, but not applied" if after != before else \
                        "nothing changed, but applying the VM's SSH access failed"
                    die(f"{name}: {said}: {why}; agentbox regenerate finishes it", error="action_failed", vm=name)
                changed = after != before or made or written or defined == "domain" or port
                state = states.status(name)
                applies = None if not changed else "next_start" if states.starts_anew(state) else "next_restart"
    return before, after, state, applies, defined


def _record(action, vm, description, failure=None, params=None):
    """The command's Activity entry. Constant actions, so the Control Room's contract test sees
    each (tests/test_activity.py)."""
    fields = {"outcome": "failed" if failure else "ok", "params": params or {},
              "error": failure.error if failure else None, "error_params": failure.params if failure else None}
    text = f"{description}: {failure.message.strip()}" if failure else description
    if action == "ssh.enabled":
        activity.record("action", "ssh.enabled", text, vm, **fields)
    elif action == "ssh.disabled":
        activity.record("action", "ssh.disabled", text, vm, **fields)
    else:
        activity.record("action", "ssh.key-added", text, vm, **fields)


def _when(name, applies, defined):
    """When the VM gets what changed, for a message."""
    if defined == "domain-postponed":
        return (f"the VM is paused, so it is defined again only once it no longer has a Pause (agentbox start "
                f"{name}, then agentbox regenerate), and SSH access applies from the next Restart or Reset after that")
    if applies == "next_start":
        return "the VM gets it at its next start"
    return f"the VM gets it from its next Restart (agentbox restart {name}) or Reset"


def _connect(rec):
    port = registry.ssh_port(rec)
    return f"ssh -p {port} agent@{hostsetup.LOOPBACK}"


# --- set-ssh -----------------------------------------------------------------

def set_ssh(args):
    """agentbox set-ssh <vm> on|off [--json]: turn the VM's SSH access on or off; its SSH keys
    stay either way."""
    positional, opts = options.parse(args)
    on = positional[1:2] != ["off"]
    action, what = ("ssh.enabled", "Turn SSH access on") if on else ("ssh.disabled", "Turn SSH access off")
    try:
        if len(positional) != 2 or positional[1] not in ("on", "off"):
            die("set-ssh <vm> on|off [--json]", error="invalid_argument")
        name = registry.check(positional[0])
        before, after, state, applies, defined = _change(
            name, lambda rec: rec if registry.ssh_access(rec) == on else {**rec, "ssh_access": on})
    except Fail as f:
        _record(action, activity.known_vm(positional), what, f)
        raise
    port, keys = registry.ssh_port(after), len(registry.ssh_keys(after))
    if applies is None:
        message = f"{name}: SSH access stays {'on' if on else 'off'}"
    elif on:
        message = f"{name}: SSH access on, port {port} ({_connect(after)}); " + (
            f"no SSH key yet (agentbox add-ssh-key {name} FILE)" if not keys else _when(name, applies, defined))
    else:
        message = f"{name}: SSH access off, port {port} closed, its SSH keys stay"
        if applies == "next_restart" and registry.ssh_access(before):
            message += f"; sshd in the VM runs until its next Restart (agentbox restart {name}) or Reset"
    if applies is not None:                  # a call that changes nothing records nothing
        _record(action, name, what)
    if "json" in opts:
        print(json.dumps({"vm": name, "enabled": on, "port": port, "keys": keys, "changed": applies is not None,
                          "state": state, "applies": applies, "message": message}))
    else:
        print(message)


# --- add-ssh-key -------------------------------------------------------------

def _read_key(path):
    """The key's text from the file, or from stdin without one; at most sshkeys.MAX_INPUT
    bytes. The text never appears in a message."""
    try:
        if path is None:
            data = sys.stdin.buffer.read(sshkeys.MAX_INPUT + 1)
        else:
            with open(path, "rb") as f:
                data = f.read(sshkeys.MAX_INPUT + 1)
    except OSError as e:
        die(f"{clean(path or 'stdin', False)[:200]}: {e.strerror}", error="invalid_argument")
    if len(data) > sshkeys.MAX_INPUT:
        die(f"not a valid SSH public key: longer than {sshkeys.MAX_INPUT} bytes", error="ssh_key_invalid")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        die("not a valid SSH public key: not UTF-8 text", error="ssh_key_invalid")


def add_ssh_key(args):
    """agentbox add-ssh-key <vm> [--name N] [FILE] [--json]: add an SSH key (a line of a .pub
    file) from FILE or stdin; its comment is its name unless --name gives one."""
    positional, opts = options.parse(args)
    key = None
    try:
        if len(positional) not in (1, 2):
            die("add-ssh-key <vm> [--name N] [FILE] [--json]", error="invalid_argument")
        name = registry.check(positional[0])
        key = sshkeys.parse(_read_key(positional[1] if len(positional) == 2 else None), opts.get("name"))
        entry = {"key": key["key"], "name": key["name"], "fingerprint": key["fingerprint"],
                 "added": times.iso(time.time()), "added_by": activity.actor()}
        _, after, state, applies, defined = _change(
            name, lambda rec: {**rec, "ssh_keys": sshkeys.add(registry.ssh_keys(rec), entry)})
    except Fail as f:
        _record("ssh.key-added", activity.known_vm(positional), "Add an SSH key", f,
                {"fingerprint": key["fingerprint"], "name": key["name"]} if key else None)
        raise
    shown = key["fingerprint"] + (f" ({key['name']})" if key["name"] else "")
    _record("ssh.key-added", name, f"Add the SSH key {shown}",
            params={"fingerprint": key["fingerprint"], "name": key["name"]})
    message = f"{name}: SSH key {shown} added; " + (
        _when(name, applies, defined) if registry.ssh_access(after)
        else f"SSH access is off (agentbox set-ssh {name} on)")
    if "json" in opts:
        print(json.dumps({"vm": name, "fingerprint": key["fingerprint"], "name": key["name"], "type": key["type"],
                          "enabled": registry.ssh_access(after), "keys": len(registry.ssh_keys(after)),
                          "state": state, "applies": applies, "message": message}, ensure_ascii=False))
    else:
        print(message)


# --- ssh ---------------------------------------------------------------------

def ssh(args):
    """agentbox ssh <vm> [--json]: the VM's SSH access, its SSH port, the fingerprint of its host
    key with a known_hosts line, and its SSH keys."""
    positional, opts = options.parse(args)
    if len(positional) != 1:
        die("ssh <vm> [--json]", error="invalid_argument")
    name = registry.check(positional[0])
    rec = registry.load(name)
    port, on = registry.ssh_port(rec), registry.ssh_access(rec)
    host = sshkeys.host_public(generate.host_key(name) or b"")
    known = f"[{hostsetup.LOOPBACK}]:{port} {sshkeys.public_line(host)}" if host else None
    keys = [{"name": k["name"], "type": sshkeys.key_type(k["key"]), "fingerprint": k["fingerprint"],
             "added": k["added"], "added_by": k["added_by"]} for k in registry.ssh_keys(rec)]
    if "json" in opts:
        print(json.dumps({"vm": name, "enabled": on, "port": port,
                          "host_key_fingerprint": sshkeys.fingerprint(host) if host else None,
                          "known_hosts": known, "keys": keys}, ensure_ascii=False))
        return
    lines = [f"{name}: SSH access {'on' if on else 'off'}, port {port}"
             + (f" ({_connect(rec)})" if on else f" (agentbox set-ssh {name} on)")]
    if host:
        lines += [f"host key: {sshkeys.fingerprint(host)}", f"known_hosts: {known}"]
    lines += [f"SSH key: {k['type']} {k['fingerprint']} {clean(k['name'], False) or '(no name)'}, added "
              f"{k['added']} by {clean(k['added_by'], False)}" for k in keys] or \
             [f"no SSH key (agentbox add-ssh-key {name} FILE)"]
    print("\n".join(lines))


DECLARATIONS = {
    "ssh": declare(ssh, "agentbox ssh <vm> [--json]", options={"json": False}, read_only=True, option_exit=1),
    "set-ssh": declare(set_ssh, "agentbox set-ssh <vm> on|off [--json]", options={"json": False},
                       own_entries=True, option_exit=1),
    "add-ssh-key": declare(add_ssh_key, "agentbox add-ssh-key <vm> [--name N] [FILE] [--json]",
                           options={"name": True, "json": False}, own_entries=True, option_exit=1),
}
