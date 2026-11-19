"""SSH access to a VM (ADR 0020): `agentbox ssh`, `set-ssh`, `add-ssh-key` and `remove-ssh-key`.

The record holds whether the VM has SSH access and its SSH keys (registry.py, sshkeys.py);
the Host keeps the VM's SSH host key, made once (generate.ssh_host_key). A change is saved
in the record, then the file QEMU passes into the VM is written (generate.vm_ssh), the VM is
defined again when its definition changed (a domain from before SSH access lacks the fw_cfg
entry), and the units of its SSH port follow its SSH access (generate.ssh_port): the port
listens while it is on and is gone while it is off. The keys stay when SSH access goes off.

What reaches the VM (SSH access and the keys) applies at once. A running VM gets it through the
fifth fixed guest-agent wrapper of the Image, agentbox-ssh, which checks the keys again,
replaces the key file whole and starts or stops sshd; only its small JSON answer comes back and
must be exactly what the Host sent. A VM that is off reads the fw_cfg file at its next start.
Any other VM (paused, or running but not taking the change) has a pending marker
(footprint.ssh_pending_file), set before the change is saved, so that a marker that cannot be
written leaves the change unsaved: `start` and the `wake` of a connection to the SSH port hand
the change over before that connection is forwarded (apply_pending), and such a connection is
refused while that fails; a connection to the Orca port leaves it alone. A Build without the
wrapper keeps the saved change for its next start, and the command fails with ssh_missing. A
change that is saved but not applied yet is recorded in Activity as that change, failed with
ssh_pending (the running VM did not take it) or ssh_not_applied (a step on the Host failed),
as set-env records env_not_applied.

Removing a key and turning SSH access off end every open SSH session of the VM: the Host cannot
tell which session used which key, so every running connection unit of its SSH port stops.
Turning it off closes the port first and then ends the sessions, removing a key ends them
first, both right after the change is saved and before any later step that can fail. The same
call again ends them also when the record does not change any more: `set-ssh off` always,
`remove-ssh-key` of a key no longer in the record while the VM has a pending change, which it
then hands over. A connection that comes in meanwhile waits in `wake` for the VM's lifecycle
lock, which the change holds, and so meets the new key file, or the marker.

Each command that changes something holds the registry lock, the record's lock and the VM's
lifecycle lock, as set-env does, and records its own Activity entry (ssh.enabled,
ssh.disabled, ssh.key-added and ssh.key-removed with the key's fingerprint and name),
failures included: also a wrong argument, an unknown VM or a broken record (set-ssh with a
word other than "off" counts as turning it on).
"""
import json
import os
import re
import sys
import time

from . import activity, footprint, generate, guest, hostsetup, options, paths, programs, registry, sshkeys
from . import state_files, states, times
from .commands import declare
from .errors import Fail, die
from .sanitize import clean

WRAPPER = "/usr/libexec/qemu-ga/fsfreeze-hook.d/agentbox-ssh"
LIMIT = 75                # seconds for the wrapper: it waits up to 30 s for guest-setup, then for systemctl
STARTING = 75             # the wrapper's exit code: guest-setup has not finished
MAX_ANSWER = 200          # characters of the wrapper's answer taken at all
FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")


# --- the VM ------------------------------------------------------------------

def _in_vm(rec):
    """What of the record reaches the VM: nothing without SSH access, else its keys."""
    return tuple(k["key"] for k in registry.ssh_keys(rec)) if registry.ssh_access(rec) else None


def pending(name):
    """Whether the VM has a change of its SSH access it has not taken yet."""
    return os.path.exists(footprint.ssh_pending_file(name))


def _mark(name):
    path = footprint.ssh_pending_file(name)
    state_files.directory(os.path.dirname(path), 0o700, only_new=True)
    open(path, "w").close()


def drop_pending(name):
    """Nothing is pending: the VM took the change, or reads it from fw_cfg at its next start.
    The caller holds the VM's lifecycle lock."""
    state_files.remove(footprint.ssh_pending_file(name))


def _end_sessions(name):
    """Every running connection unit of the VM's SSH port stops, and with it its SSH session."""
    programs.systemctl("stop", footprint.unit_name(name, "ssh@.service").replace("@.", "@*."))


def _missing(name):
    die(f"{name}: the change is saved, but the VM's Build cannot apply SSH access while it runs (it has no "
        f"agentbox-ssh), so the VM keeps the SSH keys it started with until a Reset onto a newer Build "
        f"(agentbox build <image> --reset {name})", error="ssh_missing", vm=name)


def _apply(name, rec):
    """Hand the record's SSH access to the running VM through agentbox-ssh. Its answer comes
    from the VM, untrusted: only {"keys", "sshd"} exactly as the Host sent them is taken. Dies
    with ssh_missing without the wrapper, else with why the VM did not take it."""
    access, keys = registry.ssh_access(rec), registry.ssh_keys(rec)
    data = sshkeys.fw_cfg(access, keys, generate.host_key(name) or b"")
    limit = paths.tunable("AGENTBOX_SSH_APPLY_SECONDS", LIMIT)
    code, out, _, _ = guest.execute(name, WRAPPER, ["apply"], limit, missing=lambda: _missing(name),
                                    answer=MAX_ANSWER, stdin=data)
    if code == STARTING:
        die(f"{name}: the VM is still starting", error="action_failed", vm=name)
    if code == 124:
        die(f"{name}: agentbox-ssh in the VM did not end within {limit} s", error="vm_query_timeout", vm=name,
            seconds=limit)
    if code != 0:
        die(f"{name}: agentbox-ssh in the VM failed ({f'exit {code}' if type(code) is int else 'no exit code'})",
            error="action_failed", vm=name)
    try:
        got = json.loads(out)
    except (TypeError, ValueError, RecursionError):
        got = None
    want = {"keys": len(keys) if access else 0, "sshd": access and bool(keys)}
    if not isinstance(got, dict) or got != want or type(got["keys"]) is not int or type(got["sshd"]) is not bool:
        die(f"{name}: agentbox-ssh in the VM answered something other than what the Host sent",
            error="action_failed", vm=name)


def apply_pending(name):
    """A change the VM has not taken yet, handed to it now (start, wake); the caller holds the
    VM's lifecycle lock, and the VM runs. Whether there was one. Dies when the VM does not take
    it; the marker stays, except without the wrapper (ssh_missing), which never takes it."""
    if not pending(name):
        return False
    try:
        _apply(name, registry.load(name))
    except Fail as f:
        if f.error == "ssh_missing":
            drop_pending(name)
        raise
    drop_pending(name)
    return True


def _live(name, rec):
    """The running VM takes its SSH access now, and the marker goes. When it does not, the
    marker stays, so that the next connection to its SSH port hands it over first: the command
    fails with ssh_pending, whatever the VM's reason, which the message names."""
    try:
        _apply(name, rec)
    except Fail as f:
        if f.error == "ssh_missing":
            drop_pending(name)
            raise
        why = f.message.strip().removeprefix(f"{name}: ")
        die(f"{name}: the change is saved, but the VM did not take it ({why}); each connection to its SSH port "
            "hands it over first and is refused while that fails", error="ssh_pending", vm=name)
    drop_pending(name)


# --- the change --------------------------------------------------------------

def _each(*steps):
    """Every step, also after one failed; then the first failure is raised."""
    failure = None
    for step in steps:
        try:
            step()
        except (Fail, OSError) as e:
            failure = failure or e
    if failure:
        raise failure


def _change(name, change, ends_sessions=False, again=None, pending_only=False):
    """Apply change(record) -> record. When what reaches the VM changes and the VM is not off,
    the pending marker is set first: a VM without its marker could resume with a removed key, so
    when the marker cannot be written nothing changes. Then the record is saved when it differs;
    then, each also when the one before failed, the port closed without SSH access and every SSH
    session of the VM ended with ends_sessions (also when the record did not change, so that the
    same call again ends what an earlier one could not); then the host key made (with SSH access
    or a key), the file the VM reads written, the VM defined again if its definition changed,
    the SSH port set up, and a running VM given the change. So the port is closed and the
    sessions have ended whatever fails later. A failure after the save is ssh_not_applied with
    what finishes it (its parameter finish): again, the same call as the user gives it (with
    pending_only only while the VM has its marker), otherwise agentbox regenerate; a running VM
    that does not take the change is ssh_pending (_live). Returns (before, after, state, applies,
    defined), applies: now (the VM runs and has it), on_resume (any other VM that is not off), next_start (off),
    None when nothing changed."""
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
                state = states.status(name)
                if after != before:
                    if _in_vm(after) != _in_vm(before) and not states.starts_anew(state):
                        try:
                            _mark(name)
                        except OSError as e:
                            die(f"{name}: nothing changed: the VM's pending marker could not be written "
                                f"({e.filename}: {e.strerror})", error="action_failed", vm=name)
                    registry.save(after)
                on = registry.ssh_access(after)
                closed = []
                try:
                    _each(lambda: closed.append(not on and generate.ssh_port(after)),
                          lambda: ends_sessions and _end_sessions(name))
                    port = closed[0]
                    made = (on or bool(registry.ssh_keys(after))) and generate.ssh_host_key(after)
                    written = generate.vm_ssh(after)
                    defined = generate.domain(after)
                    port = generate.ssh_port(after) if on else port
                except (Fail, OSError) as e:
                    why = e.message.strip() if isinstance(e, Fail) else f"{e.filename}: {e.strerror}"
                    finish = again if again and (not pending_only or pending(name)) else "agentbox regenerate"
                    if after == before:
                        die(f"{name}: nothing changed, but applying the VM's SSH access failed: {why}; {finish} "
                            "finishes it", error="action_failed", vm=name)
                    die(f"{name}: the change is saved, but not applied: {why}; {finish} finishes it",
                        error="ssh_not_applied", vm=name, finish=finish)
                changed = after != before or made or written or defined == "domain" or port
                if states.starts_anew(state):
                    drop_pending(name)
                    applies = "next_start"
                elif state == states.RUNNING:
                    if pending(name):
                        _live(name, after)
                    applies = "now"
                else:
                    applies = "on_resume"
    return before, after, state, applies if changed else None, defined


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
    elif action == "ssh.key-removed":
        activity.record("action", "ssh.key-removed", text, vm, **fields)
    else:
        activity.record("action", "ssh.key-added", text, vm, **fields)


def _shown(key):
    """A key for a message: its fingerprint, with its name in brackets when it has one."""
    return key["fingerprint"] + (f" ({key['name']})" if key["name"] else "")


def _when(name, applies, defined):
    """When the VM gets what changed, for a message."""
    if applies == "now":
        return "it applies in the VM now"
    if applies == "next_start":
        return "the VM gets it at its next start"
    if defined == "domain-postponed":
        return (f"the VM gets it when it resumes; it is defined again only once it no longer has a Pause "
                f"(agentbox start {name}, then agentbox regenerate)")
    return "the VM gets it when it resumes"


def _connect(rec):
    port = registry.ssh_port(rec)
    return f"ssh -p {port} agent@{hostsetup.LOOPBACK}"


# --- set-ssh -----------------------------------------------------------------

def set_ssh(args):
    """agentbox set-ssh <vm> on|off [--json]: turn the VM's SSH access on or off; its SSH keys
    stay either way. Off closes the port and ends every SSH session of the VM."""
    positional, opts = options.parse(args)
    on = positional[1:2] != ["off"]
    action, what = ("ssh.enabled", "Turn SSH access on") if on else ("ssh.disabled", "Turn SSH access off")
    try:
        if len(positional) != 2 or positional[1] not in ("on", "off"):
            die("set-ssh <vm> on|off [--json]", error="invalid_argument")
        name = registry.check(positional[0])
        before, after, state, applies, defined = _change(
            name, lambda rec: rec if registry.ssh_access(rec) == on else {**rec, "ssh_access": on},
            ends_sessions=not on, again=None if on else f"agentbox set-ssh {name} off")
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
        message = f"{name}: SSH access off, port {port} closed and every SSH session ended, its SSH keys stay"
        if applies == "on_resume" and registry.ssh_access(before):
            message += "; sshd in the VM stops when it resumes"
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
        _record("ssh.key-added", activity.known_vm(positional), f"Add the SSH key {_shown(key)}" if key
                else "Add an SSH key", f, {"fingerprint": key["fingerprint"], "name": key["name"]} if key else None)
        raise
    shown = _shown(key)
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


# --- remove-ssh-key ----------------------------------------------------------

def remove_ssh_key(args):
    """agentbox remove-ssh-key <vm> <fingerprint> [--json]: remove an SSH key by its SHA256
    fingerprint; every SSH session of the VM ends. A key no longer in the record while the VM
    has a pending change (an earlier removal the VM did not take) is no error: the same call
    again hands that change over and ends every SSH session."""
    positional, opts = options.parse(args)
    given = positional[1] if len(positional) == 2 and FINGERPRINT.fullmatch(positional[1]) else None
    removed, retried = {}, []

    def without(rec):
        keys = registry.ssh_keys(rec)
        found = [k for k in keys if k["fingerprint"] == positional[1]]
        if not found and given and pending(name):
            retried.append(True)
            return rec
        if not found:
            die(f"{name}: no SSH key {clean(positional[1], False)[:60]} (agentbox ssh {name} lists them)",
                error="ssh_key_not_found", vm=name)
        removed.update(found[0])
        return {**rec, "ssh_keys": [k for k in keys if k is not found[0]]}

    try:
        if len(positional) != 2:
            die("remove-ssh-key <vm> <fingerprint> [--json]", error="invalid_argument")
        name = registry.check(positional[0])
        _, after, state, applies, defined = _change(name, without, ends_sessions=True,
                                                    again=f"agentbox remove-ssh-key {name} {given}", pending_only=True)
    except Fail as f:
        _record("ssh.key-removed", activity.known_vm(positional),
                f"Remove the SSH key {_shown(removed) if removed else given}" if removed or given
                else "Remove an SSH key", f,
                {"fingerprint": removed["fingerprint"], "name": removed["name"]} if removed
                else {"fingerprint": given} if given else None)
        raise
    if retried:
        applies = applies or ("now" if state == states.RUNNING else "next_start" if states.starts_anew(state)
                              else "on_resume")
        _record("ssh.key-removed", name, f"Remove the SSH key {given}: hand the pending change over",
                params={"fingerprint": given})
        message = (f"{name}: no SSH key {given} in the record any more; every SSH session ended and the VM's "
                   "pending SSH change " + ("is handed over; it applies in the VM now" if applies == "now"
                                            else f"waits; {_when(name, applies, defined)}"))
        removed = {"fingerprint": given, "name": None, "key": None}
    else:
        shown = _shown(removed)
        _record("ssh.key-removed", name, f"Remove the SSH key {shown}",
                params={"fingerprint": removed["fingerprint"], "name": removed["name"]})
        message = f"{name}: SSH key {shown} removed" + (
            f" and every SSH session ended; {_when(name, applies, defined)}" if registry.ssh_access(after)
            else "; SSH access is off")
    if "json" in opts:
        print(json.dumps({"vm": name, "fingerprint": removed["fingerprint"], "name": removed["name"],
                          "type": removed["key"] and sshkeys.key_type(removed["key"]),
                          "enabled": registry.ssh_access(after), "keys": len(registry.ssh_keys(after)),
                          "state": state, "applies": applies, "message": message}, ensure_ascii=False))
    else:
        print(message)


# --- ssh ---------------------------------------------------------------------

def ssh(args):
    """agentbox ssh <vm> [--json]: the VM's SSH access, its SSH port, the fingerprint of its host
    key with a known_hosts line, its SSH keys, and whether the VM has yet to take a change."""
    positional, opts = options.parse(args)
    if len(positional) != 1:
        die("ssh <vm> [--json]", error="invalid_argument")
    name = registry.check(positional[0])
    rec = registry.load(name)
    port, on = registry.ssh_port(rec), registry.ssh_access(rec)
    host = sshkeys.host_public(generate.host_key(name) or b"")
    known = sshkeys.known_hosts_line(name, host) if host else None     # under its alias, as ssh-config gives it
    keys = [{"name": k["name"], "type": sshkeys.key_type(k["key"]), "fingerprint": k["fingerprint"],
             "added": k["added"], "added_by": k["added_by"]} for k in registry.ssh_keys(rec)]
    waiting = pending(name)
    if "json" in opts:
        print(json.dumps({"vm": name, "enabled": on, "port": port,
                          "host_key_fingerprint": sshkeys.fingerprint(host) if host else None,
                          "known_hosts": known, "keys": keys, "pending": waiting}, ensure_ascii=False))
        return
    lines = [f"{name}: SSH access {'on' if on else 'off'}, port {port}"
             + (f" ({_connect(rec)})" if on else f" (agentbox set-ssh {name} on)")]
    if waiting:
        lines.append("pending: the VM has not taken the last change yet; the next connection hands it over first")
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
    "remove-ssh-key": declare(remove_ssh_key, "agentbox remove-ssh-key <vm> <fingerprint> [--json]",
                              options={"json": False}, own_entries=True, option_exit=1),
}
