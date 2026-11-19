"""Creating and deleting VMs.

`agentbox create` enters a VM into the registry with the lowest free network
number (Orca port = 6800 + network), starts it without Allowlist entries of its
own (or with a copy of another VM's), without SSH access and without SSH keys (or with a
copy of another VM's keys, each through the rules of add-ssh-key) and a System disk from its
Image, and brings up everything else through regenerate. It does not start the VM: `agentbox pair`
or the first Orca connection does, so a VM made for later costs no memory.

`agentbox delete` takes down everything regenerate made for a VM, deletes its
System disk and archives its Data disk unless told to delete it. It first moves
the record into a marker (footprint.delete_marker), so nothing wakes or
regenerates the VM any more; running it again finishes an interrupted delete.

Both hold the registry lock for their whole run, as regenerate does, so two
creates never pick the same network and no regenerate sees half a VM.

`agentbox set-memory` changes a VM's memory limit afterwards: the record, then the
libvirt definition. libvirt keeps a new definition of a running VM for its next
start, so the running VM keeps its memory until a Restart or Reset (a reboot inside
the VM is not enough, QEMU keeps running). Nothing is changed live: QEMU cannot raise
the maximum of a running VM, and lowering its balloon would take memory away from
working agents. A paused VM is refused, its saved memory belongs to the old definition.

`agentbox set-container-image-cleanup` turns a VM's Container image cleanup on or off
and sets its days (1 to 365), two separate values in the record, so turning it off and
on again keeps the days. Whatever state the VM is in: nothing of the VM is touched, so
it takes effect the next time the cleanup runs. A record without the two fields means
on and 7 days, and only the field a call changes is written.

`agentbox set-env`, `unset-env` and `env` change and show a VM's VM variables (vmenv.py):
the record, then the file QEMU passes into the VM at its next start. The domain names that
file and is not defined again, so a paused VM is not refused: it resumes with the variables
of its last boot and gets the new ones at its next Restart or Reset.

`agentbox set-secret` and `unset-secret` do the same for a VM's secrets: values the VM gets
like VM variables, which the Host never shows or logs. The record holds only their names,
the values live only in footprint.vm_secrets_file (root:qemu, 0640), which QEMU passes in as
fw_cfg opt/agentbox/secrets. set-secret reads the value from stdin, never from an argument,
so it stays out of shell history, `ps` and Activity; no message, answer or Activity entry
repeats it, and `env` and `describe` show the names only. No name is both a VM variable and a
secret, and vmenv.MAX_VARIABLES counts both together.
"""
import json
import os
import re
import sys
import termios
import time

from . import activity, allowlist, footprint, generate, images, lifecycle, paths, registry, settings, state_files
from . import options, ssh_access, states, times, virsh, vmenv
from .commands import declare
from .credentials import OWNER
from .errors import Fail, die
from .registry import dom
from .sanitize import clean

MIN_MEMORY_MIB = 1024
MAX_MEMORY_MIB = 1048576             # when the Host's memory is unknown
MAX_DATA_DISK_GIB = 16384
# --- create ------------------------------------------------------------------

def _new_name(name):
    if not registry.NAME.fullmatch(name):
        die("invalid name: " + clean(name, False)[:40] +
            " (2 to 16 characters from a-z, 0-9 and -, starting with a letter)", error="invalid_name",
            name=clean(name, False)[:40])
    if registry.reserved(name):
        die(f"the name {name} is reserved", error="name_reserved", name=name)
    return name


def _image(name):
    """An Image with a Build on the Host (images.py)."""
    available = images.available()
    if name in available:
        return name
    if not available:
        die("no Image yet, first agentbox build base", error="image_has_no_build")
    die(f"No such Image: {clean(name, False)[:40]} (available: {', '.join(available)})", error="image_not_found",
        image=clean(name, False)[:40])


def _host_memory_mib():
    """The Host's memory: no VM may have more."""
    try:
        with open(paths.host("/proc/meminfo")) as f:
            m = re.search(r"^MemTotal:\s+(\d+) kB$", f.read(), re.MULTILINE)
    except OSError:
        m = None
    return int(m.group(1)) // 1024 if m else MAX_MEMORY_MIB


def _whole(value, what, low, high, error="invalid_argument"):
    """A whole number from low to high: text as given, or a default as a number."""
    if isinstance(value, int) and not isinstance(value, bool):
        n = value
    else:
        n = int(value) if re.fullmatch(r"[0-9]{1,9}", value) else None
    if n is None or not low <= n <= high:
        die(f"{what} must be a whole number from {low} to {high}", error=error, low=low, high=high)
    return n


def _number(opts, name, default, low, high, error="invalid_argument"):
    return _whole(opts.get(name, default), f"--{name}", low, high, error)


def _cleanup_days(value):
    """The days of the Container image cleanup: a whole number from 1 to 365 (the text an
    option was given, or an int default)."""
    if isinstance(value, bool) or not isinstance(value, int):
        value = int(value) if isinstance(value, str) and re.fullmatch(r"[0-9]{1,9}", value) else None
    if value is None or not registry.CLEANUP_DAYS[0] <= value <= registry.CLEANUP_DAYS[1]:
        die(f"Days for the Container image cleanup out of range "
            f"({registry.CLEANUP_DAYS[0]} to {registry.CLEANUP_DAYS[1]})", error="invalid_cleanup_days")
    return value


def _cleanup_on(value, default):
    """--container-image-cleanup on|off, or the Setting's default without the option."""
    if value is None:
        return default
    if value not in ("on", "off"):
        die("--container-image-cleanup must be on or off", error="invalid_argument",
            option="--container-image-cleanup")
    return value == "on"


def _cleanup_text(on, days):
    """The cleanup as people read it: "on, after 14 days" or "off (14 days)"."""
    return f"on, after {days} days" if on else f"off ({days} days)"


def _owners(names):
    for o in names:
        if not OWNER.fullmatch(o):
            die("invalid GitHub name: " + clean(o, False)[:40], error="invalid_github_owner",
                owner=clean(o, False)[:40])
    return list(dict.fromkeys(names))


def _free_network(records):
    """The lowest network number no VM uses, counting VMs still being deleted."""
    used = {r["network"] for r in records} | {m["record"]["network"] for m in footprint.being_deleted()}
    for n in range(1, 255):
        if n not in used:
            return n
    die("no free network left (at most 254 VMs)", error="no_free_network")


def _leftovers(rec):
    """What an earlier VM of this name left behind; a new VM must not take it over
    (its Claude token or secrets, its Orca identity on the Data disk, a network with another number)."""
    name = rec["name"]
    files = [("Data disk", footprint.data_disk(name)), ("System disk", footprint.system_disk(name)),
             ("Claude token", footprint.claude_token_file(name)), ("VM variables", footprint.vm_env_file(name)),
             ("secrets", footprint.vm_secrets_file(name)), ("SSH host key", footprint.ssh_host_key_file(name)),
             ("SSH host key", footprint.ssh_host_key_public_file(name)), ("SSH access", footprint.vm_ssh_file(name)),
             ("Observer", footprint.observer_dir(name)),
             *(("units", f) for f in footprint.unit_files(rec))]
    found = list(dict.fromkeys(what for what, path in files if os.path.lexists(path)))
    if virsh.run("domuuid", dom(name)).returncode == 0:
        found.append(f"VM {dom(name)}")
    if virsh.run("net-info", dom(name)).returncode == 0:
        found.append(f"network {dom(name)}")
    return found


def create(args):
    """agentbox create <vm> [--image I] [--memory-mib N] [--data-disk-gib N]
    [--container-image-cleanup on|off] [--container-image-cleanup-days N] [--owner O]...
    [--copy-allowlist-from VM] [--copy-ssh-keys-from VM] [--json]"""
    positional, opts = options.parse(args)
    if len(positional) != 1:
        die("create <vm> [--image I] [--memory-mib N] [--data-disk-gib N] [--owner O]... "
            "[--container-image-cleanup on|off] [--container-image-cleanup-days N] "
            "[--copy-allowlist-from VM] [--copy-ssh-keys-from VM]", error="invalid_argument")
    name = _new_name(positional[0])
    defaults = settings.load()     # the defaults for new VMs (Settings)
    image = _image(opts.get("image", defaults["default_image"]))
    memory = _number(opts, "memory-mib", defaults["default_memory_mib"], MIN_MEMORY_MIB, _host_memory_mib(),
                     "invalid_memory")
    disk = _number(opts, "data-disk-gib", defaults["default_data_disk_gib"], 1, MAX_DATA_DISK_GIB)
    cleanup = _cleanup_on(opts.get("container-image-cleanup"), defaults["default_container_image_cleanup"])
    cleanup_days = _cleanup_days(opts.get("container-image-cleanup-days",
                                          defaults["default_container_image_cleanup_days"]))
    owners = _owners(opts.get("owner", []))
    source = opts.get("copy-allowlist-from")
    ssh_source = opts.get("copy-ssh-keys-from")
    say, lines = lifecycle.reporter("json" in opts)
    with state_files.locked(paths.REGISTRY):
        records = registry.all_records()         # a broken registry stops create, as it stops regenerate
        generate.ports_free()                    # so does a service holding the resolver's or the proxy's ports
        for given in (source, ssh_source):
            if given is not None and given not in [r["name"] for r in records]:
                die("No such VM: " + clean(given, False)[:40], error="vm_not_found", vm=clean(given, False)[:40])
        if os.path.exists(footprint.record_file(name)):
            die(f"{name} exists already", error="vm_exists", vm=name)
        if os.path.exists(footprint.delete_marker(name)):
            die(f"{name} is being deleted, first finish agentbox delete {name} --confirm {name}",
                error="vm_being_deleted", vm=name)
        n = _free_network(records)
        rec = {"name": name, "network": n, "orca_port": registry.ORCA_BASE + n, "image": image, "build": None,
               "memory_mib": memory, "data_disk_gib": disk, "container_image_cleanup": cleanup,
               "container_image_cleanup_days": cleanup_days, "github_owners": owners,
               "created": times.iso(time.time())}
        left = _leftovers(rec)
        if left:
            die(f"{name}: leftovers of an earlier VM exist ({', '.join(left)}), remove them first",
                error="vm_leftovers", vm=name, leftovers=left)
        keys, refused = [], []
        if ssh_source is not None:
            # SSH access stays off; regenerate makes the host key for the keys (generate.vm_ssh)
            found, refused = ssh_access.keys_of(next(r for r in records if r["name"] == ssh_source))
            ssh_keys, keys, more = ssh_access.copy_keys([], found, activity.actor())
            refused += more
            if ssh_keys:
                rec["ssh_keys"] = ssh_keys
        copied = allowlist.start_vm(name, source)
        state_files.remove(footprint.open_web_file(name))
        registry.save(rec)
        ssh_access.record_copied(name, ssh_source, keys)
        say(f"{name}: entered into the registry (network {n}, Orca port {rec['orca_port']})")
        if source:
            say(f"{name}: {copied} Allowlist entries copied from {source}")
        if ssh_source:
            say(f"{name}: {len(keys)} SSH keys copied from {ssh_source}"
                + (f", SSH access is off (agentbox set-ssh {name} on)" if keys else ""))
            for k in refused:
                say(f"{name}: {ssh_access.label(k)} not copied: {k['message']}")
        todo = f"agentbox reset {name} --no-start and agentbox regenerate finish it"
        try:
            lifecycle.reset(name, False, say)
            todo = "agentbox regenerate finishes it"
            result = generate.apply()
        except Fail as f:
            text = f.message.rstrip("\n") if f.raw else f"agentbox: {f.message}"
            raise Fail(f"{text}\nagentbox: {name} is registered, but not completely set up: {todo} "
                       f"(or agentbox delete {name} --confirm {name})", f.exit, error=f.error, raw=True,
                       **{**f.params, "vm": name})
    for line in generate.report(result):
        say(line)
    say(f"{name} created. Pair with: agentbox pair {name} (starts the VM)")
    if "json" in opts:
        print(json.dumps({"vm": name, **{k: rec[k] for k in ("network", "orca_port", "image", "memory_mib",
                                                              "data_disk_gib", "container_image_cleanup",
                                                              "container_image_cleanup_days", "github_owners")},
                          "allowlist_from": source, "ssh_keys_from": ssh_source,
                          "ssh_keys": [{"fingerprint": k["fingerprint"], "name": k["name"], "type": k["type"]}
                                       for k in keys],
                          "message": lifecycle.message(lines)}, ensure_ascii=False))


# --- delete ------------------------------------------------------------------

def delete(args):
    """agentbox delete <vm> --confirm <vm> [--delete-data-disk] [--json]"""
    positional, opts = options.parse(args)
    if len(positional) != 1:
        die("delete <vm> --confirm <vm> [--delete-data-disk]", error="invalid_argument")
    name = positional[0]
    if not registry.NAME.fullmatch(name):
        die("No such VM: " + clean(name, False)[:40], error="vm_not_found", vm=clean(name, False)[:40])
    if opts.get("confirm") != name:
        die(f"deleting only with --confirm {name}", error="confirmation_required", vm=name)
    say, lines = lifecycle.reporter("json" in opts)
    data, archive, resumed = footprint.delete(name, "delete-data-disk" in opts, say)
    say(f"{name} deleted")
    if "json" in opts:
        print(json.dumps({"vm": name, "data_disk": data, "archive": archive, "resumed": resumed,
                          "message": lifecycle.message(lines)}))


# --- set-memory --------------------------------------------------------------

def set_memory(args):
    """agentbox set-memory <vm> <mib> [--json]: the VM's memory limit, from 1024 MiB up to
    the Host's memory; applies at the VM's next start, so a running VM at its next
    Restart or Reset. Refused while the VM is paused."""
    positional, opts = options.parse(args)
    if len(positional) != 2:
        die("set-memory <vm> <mib> [--json]", error="invalid_argument")
    name = registry.check(positional[0])
    memory = _whole(positional[1], "memory limit (MiB)", MIN_MEMORY_MIB, _host_memory_mib(), "invalid_memory")
    with state_files.locked(paths.REGISTRY):                 # no regenerate defines the domain meanwhile
        with state_files.locked(footprint.record_file(name)):     # no set-owners writes the record back
            with state_files.vm_lock(name):              # no Pause, start or Reset between check and define
                if states.has_pause(name):
                    die(f"{name} is paused, its saved memory belongs to the previous memory limit: "
                        f"first resume it (agentbox start {name}), then try again", error="vm_paused", vm=name)
                rec = registry.load(name)
                before = rec["memory_mib"]
                if before != memory:
                    rec = {**rec, "memory_mib": memory}
                    registry.save(rec)
                try:
                    defined = generate.domain(rec)
                except Fail as f:
                    die(f"{name}: memory limit {memory} MiB saved, but the VM was not defined again: "
                        f"{f.message.strip()}; agentbox regenerate finishes it", error="memory_not_applied",
                        vm=name, memory_mib=memory)
                state = virsh.state(name)
    changed = before != memory or defined is not None
    # libvirt has no domain (missing) or it is off: the next start uses the new definition
    applies = None if not changed else "next_start" if states.starts_anew(state) else "next_restart"
    what = f"{name}: memory limit " + (f"{before} -> {memory} MiB" if before != memory else f"{memory} MiB")
    if not changed:
        activity.unchanged()
        message = f"{name}: memory limit stays {memory} MiB"
    elif applies == "next_start":
        message = f"{what}, applies from the next start"
    else:
        message = (f"{what}, applies from the next Restart (agentbox restart {name}) or Reset; "
                   f"until then the running VM keeps its memory")
    if "json" in opts:
        print(json.dumps({"vm": name, "memory_mib": memory, "previous_mib": before, "changed": changed,
                          "state": state, "applies": applies, "message": message}))
    else:
        print(message)


# --- set-container-image-cleanup ----------------------------------------------

def set_container_cleanup(args):
    """agentbox set-container-image-cleanup <vm> [on|off] [--days N] [--json]: the VM's
    Container image cleanup, on or off and after how many days an unused Container image
    goes (1 to 365). Whatever state the VM is in; takes effect the next time the cleanup
    runs. A call that changes nothing records nothing."""
    positional, opts = options.parse(args)
    usage = "set-container-image-cleanup <vm> [on|off] [--days N] [--json]"
    if (not 1 <= len(positional) <= 2                       # the VM and on or off, nothing else
            or len(positional) == 2 and positional[1] not in ("on", "off")
            or len(positional) == 1 and "days" not in opts):    # at least one of the two
        die(usage, error="invalid_argument")
    name = registry.check(positional[0])
    on = positional[1] == "on" if len(positional) == 2 else None
    days = _cleanup_days(opts["days"]) if "days" in opts else None
    with state_files.locked(paths.REGISTRY):             # no regenerate reads a half-changed registry
        with state_files.locked(footprint.record_file(name)):    # no other call writes the record meanwhile
            rec = registry.load(name)
            before_on, before_days = registry.cleanup(rec)
            now = (before_on if on is None else on, before_days if days is None else days)
            changes = {}
            if on is not None and on != before_on:
                changes["container_image_cleanup"] = on
            if days is not None and days != before_days:
                changes["container_image_cleanup_days"] = days
            if changes:
                registry.save({**rec, **changes})
            else:
                activity.unchanged()
    activity.result(enabled="true" if now[0] else "false", days=now[1])
    changed = bool(changes)
    was = f"after {before_days} days" if before_on else f"off, {before_days} days"
    message = (f"{name}: Container image cleanup stays {_cleanup_text(*now)}" if not changed else
               f"{name}: Container image cleanup {_cleanup_text(*now)} (was {was})")
    if "json" in opts:
        print(json.dumps({"vm": name, "container_image_cleanup": now[0],
                          "container_image_cleanup_days": now[1],
                          "previous": {"container_image_cleanup": before_on,
                                       "container_image_cleanup_days": before_days},
                          "changed": changed, "message": message}, ensure_ascii=False))
    else:
        print(message)


# --- VM variables ------------------------------------------------------------

def _applies(name, changed):
    """(state, applies) after a change of what the VM gets at its start: next_start when it is
    off (or libvirt has no domain), next_restart when it runs or is paused, None unchanged."""
    state = states.status(name)
    return state, None if not changed else "next_start" if states.starts_anew(state) else "next_restart"


def _change_env(name, change):
    """Apply change(env) -> new env to the VM's VM variables: everything is checked first,
    then the record is saved, then the file QEMU passes in is written and the VM defined again
    if its definition changed (a domain from before VM variables lacks the fw_cfg entry, and
    an upgrade runs no regenerate). A variable the record has already may have a name refused
    since (vmenv.refused): it stays, never passed on. Holds the registry lock (no regenerate
    writes the file or defines the VM meanwhile), the record's lock and the lifecycle lock (no
    start or Pause between the change and the answer saying when it applies), as set-memory
    does. Returns (before, after, state, applies, defined), defined as generate.domain."""
    with state_files.locked(paths.REGISTRY):
        with state_files.locked(footprint.record_file(name)):
            with state_files.vm_lock(name):
                rec = registry.load(name)
                before = dict(rec.get("env", {}))
                secrets = registry.secrets(rec)
                after = vmenv.check(change(dict(before)), kept=before, besides=len(secrets))
                for n in after:
                    if n in secrets and n not in before:
                        die(f"{n} is a secret of {name}, a name cannot be both (agentbox unset-secret {name} {n} "
                            f"removes the secret)", error="env_name_is_secret", vm=name, name=n)
                if after != before:
                    rec = {**rec, "env": after}
                    registry.save(rec)
                try:
                    written = generate.vm_env(rec)
                except (Fail, OSError) as e:
                    why = e.message.strip() if isinstance(e, Fail) else f"{e.filename}: {e.strerror}"
                    die(f"{name}: VM variables saved, but the file the VM reads was not written: {why}; "
                        f"agentbox regenerate finishes it", error="env_not_applied", vm=name)
                try:
                    defined = generate.domain(rec)
                except Fail as f:
                    die(f"{name}: VM variables saved, but the VM was not defined again: "
                        f"{f.message.strip()}; agentbox regenerate finishes it", error="env_not_applied", vm=name)
                state, applies = _applies(name, after != before or written or defined == "domain")
    return before, after, state, applies, defined


def _what(before, after):
    """What changed, for the message: the names set and the names removed."""
    changed = [n for n in after if before.get(n) != after[n]]
    removed = [n for n in before if n not in after]
    parts = ([", ".join(changed) + " set"] if changed else []) + ([", ".join(removed) + " removed"] if removed else [])
    return "VM variables " + "; ".join(parts) if parts else "VM variables written again"


def _applies_message(name, said, applies, defined, what):
    """The message of a change the VM gets at its next start (VM variables, secrets): said,
    then when it applies."""
    if defined == "domain-postponed":
        # its saved memory belongs to the old definition (generate.domain); regenerate defines it
        # once the VM runs again, as for every other change of its definition
        return (f"{said}; the VM is paused, so it is defined again only once it no longer has a Pause "
                f"(agentbox start {name}, then agentbox regenerate), and the {what} apply from the "
                f"next Restart or Reset after that")
    if applies is None:
        return said
    if applies == "next_start":
        return f"{said}, applies from the next start"
    return (f"{said}, applies from the next Restart (agentbox restart {name}) or Reset; "
            f"until then the VM keeps the {what} it started with")


def _env_answer(name, opts, before, after, state, applies, defined, what):
    said = f"{name}: VM variables unchanged" if applies is None else f"{name}: {what}"
    if applies is None:
        activity.unchanged()
    message = _applies_message(name, said, applies, defined, "VM variables")
    if "json" in opts:
        print(json.dumps({"vm": name, "env": after, "changed": applies is not None, "state": state,
                          "applies": applies, "message": message}, ensure_ascii=False))
    else:
        print(message)


def set_env(args):
    """agentbox set-env <vm> NAME=VALUE [NAME=VALUE ...] [--replace] [--json]: add or change VM
    variables; --replace makes these the whole list (none: no VM variables)."""
    positional, opts = options.parse(args)
    if not positional or len(positional) == 1 and "replace" not in opts:
        die("set-env <vm> NAME=VALUE [NAME=VALUE ...] [--replace] [--json]", error="invalid_argument")
    name = registry.check(positional[0])
    given = vmenv.pairs(positional[1:])
    replace = "replace" in opts
    result = _change_env(name, lambda env: dict(given) if replace else {**env, **given})
    _env_answer(name, opts, *result, _what(*result[:2]))


def unset_env(args):
    """agentbox unset-env <vm> NAME [NAME ...] [--json]: remove VM variables; names the VM
    does not have change nothing."""
    positional, opts = options.parse(args)
    if len(positional) < 2:
        die("unset-env <vm> NAME [NAME ...] [--json]", error="invalid_argument")
    name = registry.check(positional[0])
    names = [vmenv.valid_name(n) for n in positional[1:]]
    result = _change_env(name, lambda env: {k: v for k, v in env.items() if k not in names})
    _env_answer(name, opts, *result, _what(*result[:2]))


def env(args):
    """agentbox env <vm> [--json]: the VM's VM variables, names and values, and the names of its
    secrets, never their values."""
    positional, opts = options.parse(args)
    if len(positional) != 1:
        die("env <vm> [--json]", error="invalid_argument")
    name = registry.check(positional[0])
    rec = registry.load(name)
    variables, secrets = rec.get("env", {}), registry.secrets(rec)
    refused, secrets_refused = vmenv.refused(variables), vmenv.refused(dict.fromkeys(secrets))
    if "json" in opts:
        print(json.dumps({"vm": name, "env": variables, "refused": refused, "secrets": secrets,
                          "secrets_refused": secrets_refused}, ensure_ascii=False))
        return
    if not variables and not secrets:
        print(f"{name}: no VM variables and no secrets")
        return
    # one line each; the values are text without line breaks (vmenv.check_value)
    for n, v in variables.items():
        print(f"{n}={clean(v, False)}")
    for n in secrets:
        print(f"{n} (secret, its value is not shown)")
    for n in refused:
        print(f"{name}: {n} is not passed on to the VM: a Safeguard or Agent Box itself sets or reads it "
              f"(agentbox unset-env {name} {n} removes it)")
    for n in secrets_refused:
        print(f"{name}: the secret {n} is not passed on to the VM: a Safeguard or Agent Box itself sets or "
              f"reads it (agentbox unset-secret {name} {n} removes it)")


# --- Secrets per VM ----------------------------------------------------------

def _read_secret(name, secret, opts):
    """The value of a secret from stdin: on a terminal (without --no-prompt) one line typed
    without echo, else all of stdin; one line break at its end is not part of it (a value
    has none, vmenv.check_value). Read before any lock is taken."""
    limit = vmenv.MAX_VALUE + 2              # the value and a line break (\r\n)
    stdin = sys.stdin.buffer
    if "no-prompt" not in opts and sys.stdin.isatty():
        fd = sys.stdin.fileno()
        before = termios.tcgetattr(fd)
        quiet = termios.tcgetattr(fd)
        quiet[3] &= ~termios.ECHO
        try:
            # echo off before the prompt, so nothing typed after it shows
            termios.tcsetattr(fd, termios.TCSAFLUSH, quiet)
            print(f"The value of the secret {secret} for {name} (it is not shown), then Enter:", file=sys.stderr,
                  flush=True)
            data = stdin.readline(limit + 1)
        finally:
            termios.tcsetattr(fd, termios.TCSAFLUSH, before)
            print(file=sys.stderr, flush=True)
    else:
        data = stdin.read(limit + 1)
    data = data[:-2] if data.endswith(b"\r\n") else data[:-1] if data.endswith(b"\n") else data
    if len(data) > vmenv.MAX_VALUE:
        die(f"the value of {secret} is longer than {vmenv.MAX_VALUE} bytes", error="secret_too_large",
            name=secret, max_bytes=vmenv.MAX_VALUE)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        die(f"the value of {secret} is not valid UTF-8", error="invalid_secret_value", name=secret)


def _change_secrets(name, change):
    """Apply change(rec, names, values) -> (names, values) to the VM's secrets, under the locks
    of _change_env. A secret that is added goes into the file before its name goes into the
    record, one that is removed leaves the record first: whatever dies in between leaves at
    most a value no name in the record points to, which regenerate removes from the file
    (generate.vm_secrets). Then the VM is defined again if its definition changed (a domain
    from before secrets lacks their fw_cfg entry). Returns (names before, names after,
    written, defined), defined as generate.domain."""
    path = footprint.vm_secrets_file(name)
    with state_files.locked(paths.REGISTRY):
        with state_files.locked(footprint.record_file(name)):
            with state_files.vm_lock(name):
                rec = registry.load(name)
                before = registry.secrets(rec)
                try:
                    values = generate.read_secrets(name)
                except OSError as e:
                    die(f"{name}: {e.filename}: {e.strerror}", error="action_failed", vm=name)
                after, values = change(rec, before, values)
                new = {k: v for k, v in rec.items() if k != "secrets"}
                if after:
                    new["secrets"] = after

                def write(saved):
                    try:
                        return generate.fw_cfg_file(path, generate.secrets_file(new, values))
                    except (Fail, OSError) as e:
                        why = e.message.strip() if isinstance(e, Fail) else f"{e.filename}: {e.strerror}"
                        if not saved:
                            die(f"{name}: the file of the secrets was not written, nothing changed: {why}",
                                error="action_failed", vm=name)
                        die(f"{name}: secrets saved, but the file the VM reads was not written: {why}; "
                            f"agentbox regenerate finishes it", error="secret_not_applied", vm=name)

                if set(after) - set(before):
                    written = write(False)
                    registry.save(new)
                else:
                    if after != before:
                        registry.save(new)
                    written = write(after != before)
                try:
                    defined = generate.domain(new)
                except Fail as f:
                    die(f"{name}: secrets saved, but the VM was not defined again: {f.message.strip()}; "
                        f"agentbox regenerate finishes it", error="secret_not_applied", vm=name)
    return before, after, written, defined


def _secret_answer(name, opts, secrets, changed, defined, said):
    """The answer of set-secret and unset-secret: names only, never a value."""
    state, applies = _applies(name, changed or defined == "domain")
    if applies is None:
        activity.unchanged()
    message = _applies_message(name, f"{name}: {said}" if applies else f"{name}: secrets unchanged", applies,
                               defined, "secrets")
    if "json" in opts:
        print(json.dumps({"vm": name, "secrets": secrets, "changed": applies is not None, "state": state,
                          "applies": applies, "message": message}, ensure_ascii=False))
    else:
        print(message)


def set_secret(args):
    """agentbox set-secret <vm> NAME [--no-prompt] [--json]: add a secret or replace its value,
    read from stdin. Setting a value counts as a change even when it is the stored one, so no
    answer tells whether a value was the one stored."""
    positional, opts = options.parse(args)
    if len(positional) != 2:
        die("set-secret <vm> NAME [--no-prompt] [--json]  (the value on stdin)", error="invalid_argument")
    name = registry.check(positional[0])
    secret = vmenv.check_name(positional[1], vmenv.SECRET)
    value = vmenv.check_value(secret, _read_secret(name, secret, opts), vmenv.SECRET)

    def change(rec, names, values):
        if secret in rec.get("env", {}):
            die(f"{secret} is a VM variable of {name}, a name cannot be both (agentbox unset-env {name} {secret} "
                f"removes the variable)", error="secret_name_is_env", vm=name, name=secret)
        after = names if secret in names else [*names, secret]
        vmenv.check_count(len(after), len(rec.get("env", {})), vmenv.SECRET)
        return after, {**values, secret: value}

    _, after, _, defined = _change_secrets(name, change)
    _secret_answer(name, opts, after, True, defined, f"secret {secret} set")


def unset_secret(args):
    """agentbox unset-secret <vm> NAME [--json]: remove a secret, its name and its value; a name
    the VM does not have changes nothing."""
    positional, opts = options.parse(args)
    if len(positional) != 2:
        die("unset-secret <vm> NAME [--json]", error="invalid_argument")
    name = registry.check(positional[0])
    secret = vmenv.valid_name(positional[1], vmenv.SECRET)
    before, after, written, defined = _change_secrets(
        name, lambda rec, names, values: ([n for n in names if n != secret],
                                          {k: v for k, v in values.items() if k != secret}))
    _secret_answer(name, opts, after, before != after or written, defined,
                   f"secret {secret} removed" if secret in before else "secrets written again")


DECLARATIONS = {
    "create": declare(create, "agentbox create <vm> [--image I] [--memory-mib N] [--data-disk-gib N] [--owner O]... [--container-image-cleanup on|off] [--container-image-cleanup-days N] [--copy-allowlist-from VM] [--copy-ssh-keys-from VM] [--json]",
                      options={"image": True, "memory-mib": True, "data-disk-gib": True, "owner": options.MANY,
                               "container-image-cleanup": True, "container-image-cleanup-days": True,
                               "copy-allowlist-from": True, "copy-ssh-keys-from": True, "json": False},
                      control_room=True, option_exit=1,
                      describe=lambda a: activity.said(
                          "Create the VM" + (f" with Image {activity.name(a.values.get('image'), settings.IMAGE)}"
                                             if activity.name(a.values.get("image"), settings.IMAGE) else ""),
                          image=activity.name(a.values.get("image"), settings.IMAGE))),
    "delete": declare(delete, "agentbox delete <vm> --confirm <vm> [--delete-data-disk] [--json]",
                      options={"confirm": True, "delete-data-disk": False, "json": False}, control_room=True,
                      option_exit=1,
                       describe=lambda a: activity.said("Delete the VM and its Data disk", delete_data_disk=True)
                       if "delete-data-disk" in a.values else activity.said("Delete the VM")),
    "set-memory": declare(set_memory, "agentbox set-memory <vm> <mib> [--json]", options={"json": False},
                           control_room=True, option_exit=1,
                           describe=lambda a: activity.said(f"Memory limit {a.positional[1]} MiB", mib=a.positional[1])
                           if len(a.positional) > 1 and re.fullmatch(r"[0-9]{1,9}", a.positional[1])
                           else activity.said("Change the memory limit")),
    "set-container-image-cleanup": declare(
        set_container_cleanup, "agentbox set-container-image-cleanup <vm> [on|off] [--days N] [--json]",
        options={"days": True, "json": False}, control_room=True, option_exit=1,
         describe=lambda a: activity.said("Set the Container image cleanup")),
    "set-env": declare(set_env, "agentbox set-env <vm> NAME=VALUE [NAME=VALUE ...] [--replace] [--json]",
                       options={"replace": False, "json": False}, control_room=True, option_exit=1,
                       describe=activity.describe_set_env),
    "unset-env": declare(unset_env, "agentbox unset-env <vm> NAME [NAME ...] [--json]", options={"json": False},
                         describe=activity.describe_unset_env, option_exit=1),
    "env": declare(env, "agentbox env <vm> [--json]", options={"json": False}, read_only=True,
                   option_exit=1),
    "set-secret": declare(set_secret, "agentbox set-secret <vm> NAME [--no-prompt] [--json]",
                          options={"no-prompt": False, "json": False}, control_room=True,
                          option_exit=1, hide_arguments=True,
                          describe=activity.describe_secret("Set the secret", "Set a secret")),
    "unset-secret": declare(unset_secret, "agentbox unset-secret <vm> NAME [--json]", options={"json": False},
                            control_room=True, option_exit=1, hide_arguments=True,
                            describe=activity.describe_secret("Remove the secret", "Remove a secret")),
}
