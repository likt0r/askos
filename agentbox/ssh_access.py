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

Several keys go in as one change: `add-ssh-key <vm> --from <other>` copies the other VM's keys
once (no link stays: later changes of either VM stay its own), `create --copy-ssh-keys-from`
does it for a new VM, and `add-ssh-key <vm> --mine` adds the owner's own ~/.ssh/*.pub, read in a
child process with the rights of the user who ran sudo (else the caller's), as `import` packs a
folder, after the owner confirms the keys it shows (--yes skips that). Each key goes through
the rules and limits of one key (sshkeys.parse, sshkeys.add); one they refuse, or one entered
already, is named and left out while the others go in. A copy across VMs is always this
explicit owner action, never a default (ADR 0001).

Each command that changes something holds the registry lock, the record's lock and the VM's
lifecycle lock, as set-env does, and records its own Activity entry (ssh.enabled,
ssh.disabled, ssh.key-added and ssh.key-removed with the key's fingerprint and name; one
ssh.key-added per key of several, none for a key left out), failures included: also a wrong
argument, an unknown VM or a broken record (set-ssh with a word other than "off" counts as
turning it on).
"""
import json
import os
import pwd
import re
import signal
import stat
import sys
import time

from . import activity, callers, footprint, generate, guest, hostsetup, options, paths, programs, registry
from . import sshkeys, state_files, states, times
from .commands import declare
from .errors import Fail, die
from .sanitize import clean

WRAPPER = "/usr/libexec/qemu-ga/fsfreeze-hook.d/agentbox-ssh"
LIMIT = 75                # seconds for the wrapper: it waits up to 30 s for guest-setup, then for systemctl
STARTING = 75             # the wrapper's exit code: guest-setup has not finished
SETUP_FAILED = 76         # the wrapper's exit code: guest-setup has failed
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
    if code == SETUP_FAILED:
        die(f"{name}: guest-setup failed in the VM, so SSH access cannot be applied; a Reset onto the Build "
            f"(agentbox build <image> --reset {name}) or a look at agentbox logs {name} helps",
            error="action_failed", vm=name)
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
                    marked = False
                    if _in_vm(after) != _in_vm(before) and not states.starts_anew(state):
                        marked = not pending(name)
                        try:
                            _mark(name)
                        except OSError as e:
                            die(f"{name}: nothing changed: the VM's pending marker could not be written "
                                f"({e.filename}: {e.strerror})", error="action_failed", vm=name)
                    try:
                        registry.save(after)
                    except BaseException:
                        if marked:
                            drop_pending(name)     # nothing changed: nothing is pending
                        raise
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


USAGE_ADD = "add-ssh-key <vm> [--name N] [FILE] | --from <vm> | --mine [--yes] [--json]"


def add_ssh_key(args):
    """agentbox add-ssh-key <vm> [--name N] [FILE] [--json]: add an SSH key (a line of a .pub
    file) from FILE or stdin; its comment is its name unless --name gives one. With --from <vm>
    the keys of that VM, with --mine the owner's own ~/.ssh/*.pub (_add_many)."""
    positional, opts = options.parse(args)
    if "from" in opts or "mine" in opts:
        return _add_many(positional, opts)
    key = None
    try:
        if len(positional) not in (1, 2) or "yes" in opts:
            die(USAGE_ADD, error="invalid_argument")
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


# --- several keys at once: --from, --mine, create --copy-ssh-keys-from ---------

class _Unchanged(Exception):
    """None of several keys goes in: the record stays as it is and nothing is applied."""


def _parsed(items):
    """Keys as add-ssh-key takes them, each through sshkeys.parse: (found, refused). items:
    {"text", "name"?, "file"?}, or {"file", "error", "message"} for a file that was not read."""
    found, refused = [], []
    for item in items:
        where = {"file": item["file"]} if "file" in item else {}
        if "error" in item:
            refused.append({**where, "error": item["error"], "message": item["message"]})
            continue
        try:
            found.append({**sshkeys.parse(item["text"], item.get("name")), **where})
        except Fail as f:
            refused.append({**where, "error": f.error, "message": f.message})
    return found, refused


def keys_of(rec):
    """The SSH keys of a VM's record as add-ssh-key takes them, to copy into another VM: (found,
    refused), each through the rules of one key again."""
    return _parsed([{"text": k["key"], "name": k["name"]} for k in registry.ssh_keys(rec)])


def copy_keys(keys, found, by):
    """keys with each found key that sshkeys.add takes, in order, added now by `by`: (keys,
    added, refused). A key it refuses (entered for this VM already, beyond 16 keys, beyond what
    the file the VM reads holds) is left out with why; the others still go in."""
    added, refused, now = [], [], times.iso(time.time())
    for k in found:
        try:
            keys = sshkeys.add(keys, {"key": k["key"], "name": k["name"], "fingerprint": k["fingerprint"],
                                      "added": now, "added_by": by})
        except Fail as f:
            refused.append({**k, "error": f.error, "message": f.message})
        else:
            added.append(k)
    return keys, added, refused


def _public(k):
    """What a message or --json shows of a key: never the key itself."""
    return {f: k[f] for f in ("file", "type", "fingerprint", "name") if f in k}


def record_copied(name, source, added, failure=None):
    """An Activity entry ssh.key-added per key added (copied from the VM source, or one of the
    owner's own), as add-ssh-key records one key."""
    for k in added:
        how = f", copied from {source}" if source else f", from {k['file']}" if "file" in k else ""
        _record("ssh.key-added", name, f"Add the SSH key {_shown(k)}{how}", failure,
                {"fingerprint": k["fingerprint"], "name": k["name"]})


def label(k):
    """A key left out, for a message: its file and, once known, its fingerprint and name."""
    if "fingerprint" in k:
        return (f"{k['file']} " if "file" in k else "") + _shown(k)
    return k.get("file", "an SSH key")


def _add_many(positional, opts):
    """add-ssh-key <vm> --from <other> | --mine [--yes]: several keys in one change, each through
    the rules and limits of one key; a key refused or entered already is named and left out,
    the others go in. Without a key to add, nothing changes."""
    source, mine, as_json = opts.get("from"), "mine" in opts, "json" in opts
    what = f"Copy the SSH keys of {clean(source, False)[:40]}" if source is not None else "Add your own SSH keys"
    added, refused, folder = [], [], None
    try:
        if len(positional) != 1 or (source is not None) == mine or "name" in opts or ("yes" in opts and not mine):
            die(USAGE_ADD, error="invalid_argument")
        name = registry.check(positional[0])
        candidates = None
        if source is not None:
            source = registry.check(source)
            if source == name:
                die(f"{name}: SSH keys are copied from another VM", error="invalid_argument", option="--from")
        else:
            if "yes" not in opts and (as_json or not sys.stdin.isatty()):
                die("--mine adds your keys only after you confirm them at a terminal, or with --yes",
                    error="confirmation_required", vm=name)
            folder, files = _own_keys()
            candidates = _parsed(files)
            if "yes" not in opts:
                _confirm(name, folder, *candidates)
        by = activity.actor()

        def change(rec):
            found, bad = candidates or keys_of(registry.load(source))
            keys, got, left = copy_keys(registry.ssh_keys(rec), found, by)
            added[:], refused[:] = got, bad + left
            if not got:
                raise _Unchanged
            return {**rec, "ssh_keys": keys}
        try:
            _, after, state, applies, defined = _change(name, change)
        except _Unchanged:
            after, state, applies, defined = registry.load(name), states.status(name), None, None
    except Fail as f:
        if added:
            record_copied(activity.known_vm(positional), source, added, f)
        else:
            _record("ssh.key-added", activity.known_vm(positional), what, f)
        raise
    record_copied(name, source, added)
    verb = f"copied from {source}" if source else "added"
    if added:
        lines = [f"{name}: {len(added)} SSH key{'s' if len(added) > 1 else ''} {verb}: "
                 + ", ".join(_shown(k) for k in added) + "; " + (
                     _when(name, applies, defined) if registry.ssh_access(after)
                     else f"SSH access is off (agentbox set-ssh {name} on)")]
    else:
        lines = [f"{name}: no SSH key {verb}" + (f", {source} has none" if source and not refused else "")]
    lines += [f"{name}: {label(k)} not {'copied' if source else 'added'}: {k['message']}" for k in refused]
    message = "\n".join(lines)
    if as_json:
        print(json.dumps({"vm": name, "from": source, "folder": folder, "added": [_public(k) for k in added],
                          "skipped": [{**_public(k), "error": k["error"], "message": k["message"]} for k in refused],
                          "enabled": registry.ssh_access(after), "keys": len(registry.ssh_keys(after)),
                          "state": state, "applies": applies, "message": message}, ensure_ascii=False))
    else:
        print(message)


def _confirm(name, folder, found, refused):
    """Show the keys --mine found and ask before any goes in; dies unless the answer is yes.
    Whether each would go in is checked against the record as it is now; the change checks
    again under its locks."""
    _, would, left = copy_keys(registry.ssh_keys(registry.load(name)), found, activity.actor())
    lines = [f"SSH keys in {clean(folder, False)}:"]
    lines += [f"  {k['file']}: {k['type']} {_shown(k)}" for k in would]
    lines += [f"  {label(k)}: left out, {k['message']}" for k in refused + left]
    print("\n".join(lines))
    if not would:
        return
    print(f"Add {len(would)} SSH key{'s' if len(would) > 1 else ''} to {name}? [y/N] ", end="", flush=True)
    if sys.stdin.readline().strip().lower() not in ("y", "yes"):
        die(f"{name}: no SSH key added", error="confirmation_required", vm=name)


MAX_OWN_FILES = 32           # ~/.ssh/*.pub read at most, by name; the others are named and left out


def _own_keys():
    """The owner's own SSH public keys: ~/.ssh/*.pub of the user who ran sudo, else of the caller,
    read in a child process with that user's rights, as `import` packs a folder (callers.py),
    so root reads nothing for it that this user could not read: (folder, [{"file", "text"} or
    {"file", "error", "message"}]). Dies when the folder cannot be read or holds no such file."""
    r, w = os.pipe()
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:                                   # the child: never returns
        status = 1
        try:
            os.close(r)
            who = callers.sudo_user()
            if os.geteuid() == 0 and who:
                callers.become(*who)
            result, status = _read_own(), 0
        except Fail as f:
            result = {"fail": f.message, "error": f.error}
        except BaseException as e:                 # the child reports every failure
            result = {"fail": f"reading ~/.ssh fails ({type(e).__name__}: {e})", "error": "action_failed"}
        try:
            with os.fdopen(w, "w") as pipe:
                json.dump(result, pipe)
        finally:
            os._exit(status)
    os.close(w)
    try:
        with os.fdopen(r) as pipe:
            data = pipe.read()
    except BaseException:                          # an interrupted parent leaves no child behind
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        raise
    os.waitpid(pid, 0)
    # what the child sends is taken as untrusted: the user it ran as decides what it read
    try:
        result = json.loads(data)
    except (ValueError, RecursionError):
        result = None
    if isinstance(result, dict) and isinstance(result.get("fail"), str) and isinstance(result.get("error"), str):
        raise Fail(clean(result["fail"], False)[:500], error=clean(result["error"], False)[:64])
    files = result.get("files") if isinstance(result, dict) else None
    if not (isinstance(files, list) and isinstance(result.get("folder"), str)
            and all(isinstance(f, dict) and isinstance(f.get("file"), str)
                    and all(isinstance(v, str) for v in f.values()) for f in files)):
        die("reading ~/.ssh fails", error="action_failed")
    cleaned = {"file": 200, "message": 500, "error": 64}
    return clean(result["folder"], False), [
        {**f, **{k: clean(f[k], False)[:n] for k, n in cleaned.items() if k in f}} for f in files]


def _read_own():
    """In the child, with the user's rights: their ~/.ssh/*.pub. Their home is HOME, which
    become() sets from the passwd entry; else (no HOME) the passwd entry of the caller."""
    home = os.environ.get("HOME", "")
    if not os.path.isabs(home):
        home = pwd.getpwuid(os.geteuid()).pw_dir
    folder = os.path.join(home, ".ssh")
    try:
        names = sorted(n for n in os.listdir(folder) if n.endswith(".pub") and not n.startswith("."))
    except OSError as e:
        die(f"no SSH key found: {clean(folder, False)}: {e.strerror}", error="ssh_key_invalid")
    if not names:
        die(f"no SSH key found: {clean(folder, False)} has no *.pub file", error="ssh_key_invalid")
    files = [_read_own_file(folder, n) for n in names[:MAX_OWN_FILES]]
    files += [{"file": clean(n, False)[:200], "error": "invalid_argument",
               "message": f"not read: only the first {MAX_OWN_FILES} *.pub files by name are"}
              for n in names[MAX_OWN_FILES:]]
    return {"folder": folder, "files": files}


def _read_own_file(folder, n):
    """One .pub file: {"file", "text"}, or {"file", "error", "message"} when it is no regular
    file, cannot be read, is too long or no UTF-8 text. Its text never appears in a message."""
    shown = clean(n, False)[:200]
    try:
        fd = os.open(os.path.join(folder, n), os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as e:
        return {"file": shown, "error": "invalid_argument", "message": e.strerror}
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        return {"file": shown, "error": "invalid_argument", "message": "not a regular file"}
    with os.fdopen(fd, "rb") as f:
        try:
            data = f.read(sshkeys.MAX_INPUT + 1)
        except OSError as e:
            return {"file": shown, "error": "invalid_argument", "message": e.strerror}
    if len(data) > sshkeys.MAX_INPUT:
        return {"file": shown, "error": "ssh_key_invalid",
                "message": f"not a valid SSH public key: longer than {sshkeys.MAX_INPUT} bytes"}
    try:
        return {"file": shown, "text": data.decode("utf-8")}
    except UnicodeDecodeError:
        return {"file": shown, "error": "ssh_key_invalid", "message": "not a valid SSH public key: not UTF-8 text"}


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

def _listed(k):
    return {"name": k["name"], "type": sshkeys.key_type(k["key"]), "fingerprint": k["fingerprint"],
            "added": k["added"]}


def described(rec):
    """The VM's SSH access as `describe` shows it: on or off, its SSH port, the fingerprint of
    its host key (None before there is one), its SSH keys and whether the VM has yet to take a
    change."""
    host = sshkeys.host_public(generate.host_key(rec["name"]) or b"")
    return {"enabled": registry.ssh_access(rec), "port": registry.ssh_port(rec),
            "host_key_fingerprint": sshkeys.fingerprint(host) if host else None,
            "keys": [_listed(k) for k in registry.ssh_keys(rec)], "pending": pending(rec["name"])}


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
    keys = [{**_listed(k), "added_by": k["added_by"]} for k in registry.ssh_keys(rec)]
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
    "add-ssh-key": declare(add_ssh_key, "agentbox " + USAGE_ADD,
                           options={"name": True, "from": True, "mine": False, "yes": False, "json": False},
                           own_entries=True, option_exit=1),
    "remove-ssh-key": declare(remove_ssh_key, "agentbox remove-ssh-key <vm> <fingerprint> [--json]",
                              options={"json": False}, own_entries=True, option_exit=1),
}
