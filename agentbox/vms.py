"""Creating and deleting VMs.

`agentbox create` enters a VM into the registry with the lowest free network
number (Orca port = 6800 + network), starts it without Allowlist entries of its
own (or with a copy of another VM's) and a System disk from its Image, and brings up
everything else through regenerate. It does not start the VM: `agentbox pair`
or the first Orca connection does, so a VM made for later costs no memory.

`agentbox delete` takes down everything regenerate made for a VM, deletes its
System disk and archives its Data disk unless told to delete it. It first moves
the record into a marker (generate.delete_marker), so nothing wakes or
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
"""
import datetime
import json
import os
import re
import shutil

from . import activity, allowlist, detection, generate, images, lifecycle, paths, registry, responding, settings, state_files
from . import virsh, vmenv
from .credentials import OWNER
from .errors import Fail, die
from .registry import dom
from .sanitize import clean

MIN_MEMORY_MIB = 1024
MAX_MEMORY_MIB = 1048576             # when the Host's memory is unknown
MAX_DATA_DISK_GIB = 16384
# option -> takes a value; --owner may repeat
CREATE_OPTIONS = {"image": True, "memory-mib": True, "data-disk-gib": True, "owner": True,
                  "container-image-cleanup": True, "container-image-cleanup-days": True,
                  "copy-allowlist-from": True, "json": False}
DELETE_OPTIONS = {"confirm": True, "delete-data-disk": False, "json": False}


def _options(args, known):
    """Positional arguments and options (--name value, --name=value, or a flag) as (list, dict)."""
    positional, opts, i = [], {}, 0
    while i < len(args):
        a = args[i]
        i += 1
        if not a.startswith("--"):
            positional.append(a)
            continue
        name, eq, value = a[2:].partition("=")
        if name not in known:
            die("unknown option: " + clean(a, False)[:60], error="invalid_argument", option=clean(a, False)[:60])
        if not known[name]:
            if eq:
                die(f"--{name} takes no value", error="invalid_argument", option=f"--{name}")
            opts[name] = True
            continue
        if not eq:
            if i >= len(args):
                die(f"--{name} needs a value", error="invalid_argument", option=f"--{name}")
            value, i = args[i], i + 1
        if name == "owner":
            opts.setdefault(name, []).append(value)
        elif name in opts:
            die(f"--{name} given twice", error="invalid_argument", option=f"--{name}")
        else:
            opts[name] = value
    return positional, opts


def _remove(path):
    """Remove a file if it is there; whether it was."""
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        die(f"{path}: {e.strerror}", error="action_failed")


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
    used = {r["network"] for r in records} | {m["record"]["network"] for m in generate.being_deleted()}
    for n in range(1, 255):
        if n not in used:
            return n
    die("no free network left (at most 254 VMs)", error="no_free_network")


def _leftovers(rec):
    """What an earlier VM of this name left behind; a new VM must not take it over
    (its Claude token, its Orca identity on the Data disk, a network with another number)."""
    name = rec["name"]
    files = [("Data disk", generate.data_disk(name)), ("System disk", lifecycle.system_disk(name)),
             ("Claude token", state_files.claude_token_file(name)), ("VM variables", state_files.vm_env_file(name)),
             ("Observer", lifecycle.observer_dir(name)),
             *(("units", os.path.join(paths.SYSTEMD, u)) for u in generate.units(rec))]
    found = list(dict.fromkeys(what for what, path in files if os.path.lexists(path)))
    if virsh.run("domuuid", dom(name)).returncode == 0:
        found.append(f"VM {dom(name)}")
    if virsh.run("net-info", dom(name)).returncode == 0:
        found.append(f"network {dom(name)}")
    return found


def create(args):
    """agentbox create <vm> [--image I] [--memory-mib N] [--data-disk-gib N]
    [--container-image-cleanup on|off] [--container-image-cleanup-days N] [--owner O]...
    [--copy-allowlist-from VM] [--json]"""
    positional, opts = _options(args, CREATE_OPTIONS)
    if len(positional) != 1:
        die("create <vm> [--image I] [--memory-mib N] [--data-disk-gib N] [--owner O]... "
            "[--container-image-cleanup on|off] [--container-image-cleanup-days N] "
            "[--copy-allowlist-from VM]", error="invalid_argument")
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
    say, lines = lifecycle._reporter("json" in opts)
    with state_files.locked(paths.REGISTRY):
        records = registry.all_records()         # a broken registry stops create, as it stops regenerate
        generate.ports_free()                    # so does a service holding the resolver's or the proxy's ports
        if source is not None and source not in [r["name"] for r in records]:
            die("No such VM: " + clean(source, False)[:40], error="vm_not_found", vm=clean(source, False)[:40])
        if os.path.exists(registry.record_file(name)):
            die(f"{name} exists already", error="vm_exists", vm=name)
        if os.path.exists(generate.delete_marker(name)):
            die(f"{name} is being deleted, first finish agentbox delete {name} --confirm {name}",
                error="vm_being_deleted", vm=name)
        n = _free_network(records)
        rec = {"name": name, "network": n, "orca_port": registry.ORCA_BASE + n, "image": image, "build": None,
               "memory_mib": memory, "data_disk_gib": disk, "container_image_cleanup": cleanup,
               "container_image_cleanup_days": cleanup_days, "github_owners": owners,
               "created": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        left = _leftovers(rec)
        if left:
            die(f"{name}: leftovers of an earlier VM exist ({', '.join(left)}), remove them first",
                error="vm_leftovers", vm=name, leftovers=left)
        copied = allowlist.start_vm(name, source)
        _remove(state_files.open_web_file(name))
        registry.save(rec)
        say(f"{name}: entered into the registry (network {n}, Orca port {rec['orca_port']})")
        if source:
            say(f"{name}: {copied} Allowlist entries copied from {source}")
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
                          "allowlist_from": source, "message": lifecycle._message(lines)}, ensure_ascii=False))


# --- delete ------------------------------------------------------------------

def _archive_path(name):
    """A new name in the archive for the VM's Data disk."""
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = os.path.join(paths.ARCHIVE, f"{name}-data-{stamp}")
    path, n = base + ".qcow2", 1
    while os.path.lexists(path):
        n += 1
        path = f"{base}-{n}.qcow2"
    return path


def _write_marker(name, marker):
    path = generate.delete_marker(name)
    if not os.path.isdir(os.path.dirname(path)):
        os.makedirs(os.path.dirname(path))
        os.chmod(os.path.dirname(path), 0o700)
    state_files.write_atomic(path, (json.dumps(marker, indent=2) + "\n").encode(), 0o600)


def _begin(name, say):
    """The VM's marker, written before its record is removed: (marker, resumed)."""
    marker = next((m for m in generate.being_deleted() if m["record"]["name"] == name), None)
    record = registry.record_file(name)
    if not os.path.exists(record):
        if marker is None:
            die(f"No such VM: {name}", error="vm_not_found", vm=name)
        say(f"{name}: resuming the delete")
        return marker, True
    with state_files.locked(record):         # no set-owners writes it back meanwhile
        rec = registry.load(name)
        # a record that came back (migrate, by hand) means its network may be back as well
        marker = {"archive": _archive_path(name), **(marker or {}), "record": rec, "network_removed": False}
        _write_marker(name, marker)
        _remove(record)
    say(f"{name}: removed from the registry")
    return marker, False


def _units_off(rec, say):
    """The Orca port, proxy and token service of the VM stop and their units go."""
    name = rec["name"]
    files = [os.path.join(paths.SYSTEMD, u) for u in generate.units(rec)]
    present = [s for s in generate.sockets(rec) if os.path.exists(os.path.join(paths.SYSTEMD, s))]
    if present:
        generate._systemctl("disable", "--now", *present)        # the proxy service stops with its socket
    # open connections run in instances of their own (patterns: nothing to stop is fine)
    generate._systemctl("stop", f"agentbox-gui-{name}@*.service", f"agentbox-ghvm-{name}@*.service")
    removed = [f for f in files if _remove(f)]
    generate._systemctl("daemon-reload")
    if present or removed:
        say(f"{name}: Orca port, proxy and token service turned off")


def _libvirt(kind, name):
    """virsh's answer about the VM's domain or network; None when libvirt has
    none, dies when virsh cannot tell."""
    r = virsh.run("domuuid" if kind == "domain" else "net-info", dom(name))
    if r.returncode == 0:
        return r
    if f"failed to get {kind}" in r.stderr:
        return None
    die(f"{name}: virsh fails: " + (r.stderr.strip()[-300:] or f"exit {r.returncode}"), error="action_failed",
        vm=name)


def _domain_off(name, say):
    if _libvirt("domain", name) is None:
        return
    # no wake starts it between the stop and the undefine (a running domain would outlive it)
    with state_files.vm_lock(name):
        lifecycle._stop(name)        # discards a Pause, shuts down, forces off after the time limit
        generate._virsh(f"{name}: removing the VM", "undefine", dom(name), "--managed-save")
    say(f"{name}: VM stopped and removed")


def _network_off(name, say):
    info = _libvirt("network", name)
    if info is None:
        return
    if re.search(r"^Active:\s*yes$", info.stdout, re.MULTILINE):
        generate._virsh(f"{name}: stopping the network", "net-destroy", dom(name))
    generate._virsh(f"{name}: removing the network", "net-undefine", dom(name))
    say(f"{name}: network {dom(name)} removed")


def _disks(name, archive, delete_data, say):
    """System disk deleted; Data disk archived or deleted. Returns (what, archive path)."""
    if _remove(lifecycle.system_disk(name)):
        say(f"{name}: System disk deleted")
    data = generate.data_disk(name)
    if not os.path.lexists(data):
        if os.path.lexists(archive):         # archived before the delete was interrupted
            say(f"{name}: Data disk archived: {archive}")
            return "archived", archive
        return "missing", None
    if delete_data:
        _remove(data)
        say(f"{name}: Data disk deleted")
        return "deleted", None
    try:
        if not os.path.isdir(paths.ARCHIVE):
            os.makedirs(paths.ARCHIVE)
            os.chmod(paths.ARCHIVE, 0o700)
        if os.path.lexists(archive):
            die(f"{archive} exists already", error="action_failed", vm=name)
        os.rename(data, archive)
    except OSError as e:
        die(f"moving the Data disk to {archive}: {e.strerror}", error="action_failed", vm=name)
    say(f"{name}: Data disk archived: {archive}")
    return "archived", archive


def _files(name, say):
    responding.clear(name)           # bookkeeping only, never fails (the stop removed it already)
    gone = [_remove(f) for f in (state_files.claude_token_file(name), state_files.vm_env_file(name),
                                 state_files.open_web_file(name),
                                 generate.legacy_owners_file(name), detection.seen_file(name),
                                 os.path.join(paths.STATE, "domains", f"{name}.xml"))]
    gone.append(allowlist.drop_vm(name))
    observer = lifecycle.observer_dir(name)
    if os.path.isdir(observer):
        try:
            shutil.rmtree(observer)
        except OSError as e:
            die(f"{observer}: {e.strerror}", error="action_failed", vm=name)
        gone.append(True)
    if any(gone):
        say(f"{name}: Claude token, VM variables, Allowlist entries and Observer removed")


def delete(args):
    """agentbox delete <vm> --confirm <vm> [--delete-data-disk] [--json]"""
    positional, opts = _options(args, DELETE_OPTIONS)
    if len(positional) != 1:
        die("delete <vm> --confirm <vm> [--delete-data-disk]", error="invalid_argument")
    name = positional[0]
    if not registry.NAME.fullmatch(name):
        die("No such VM: " + clean(name, False)[:40], error="vm_not_found", vm=clean(name, False)[:40])
    if opts.get("confirm") != name:
        die(f"deleting only with --confirm {name}", error="confirmation_required", vm=name)
    say, lines = lifecycle._reporter("json" in opts)
    with state_files.locked(paths.REGISTRY):
        registry.all_records()           # a broken registry stops delete before any change
        generate.ports_free()            # so does a service holding the resolver's or the proxy's ports
        marker, resumed = _begin(name, say)
        _units_off(marker["record"], say)
        _domain_off(name, say)
        _network_off(name, say)
        if not marker["network_removed"]:
            marker["network_removed"] = True     # from now on regenerate leaves its nftables rule out
            _write_marker(name, marker)
        data, archive = _disks(name, marker["archive"], "delete-data-disk" in opts, say)
        _files(name, say)
        for line in generate.report(generate.apply()):
            say(line)
        _remove(generate.delete_marker(name))
    say(f"{name} deleted")
    if "json" in opts:
        print(json.dumps({"vm": name, "data_disk": data, "archive": archive, "resumed": resumed,
                          "message": lifecycle._message(lines)}))


# --- set-memory --------------------------------------------------------------

def set_memory(args):
    """agentbox set-memory <vm> <mib> [--json]: the VM's memory limit, from 1024 MiB up to
    the Host's memory; applies at the VM's next start, so a running VM at its next
    Restart or Reset. Refused while the VM is paused."""
    positional, opts = _options(args, {"json": False})
    if len(positional) != 2:
        die("set-memory <vm> <mib> [--json]", error="invalid_argument")
    name = registry.check(positional[0])
    memory = _whole(positional[1], "memory limit (MiB)", MIN_MEMORY_MIB, _host_memory_mib(), "invalid_memory")
    with state_files.locked(paths.REGISTRY):                 # no regenerate defines the domain meanwhile
        with state_files.locked(registry.record_file(name)):     # no set-owners writes the record back
            with state_files.vm_lock(name):              # no Pause, start or Reset between check and define
                if lifecycle.has_save(name):
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
    applies = None if not changed else "next_start" if state in ("shut off", virsh.MISSING) else "next_restart"
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
    positional, opts = _options(args, {"days": True, "json": False})
    usage = "set-container-image-cleanup <vm> [on|off] [--days N] [--json]"
    if (not 1 <= len(positional) <= 2                       # the VM and on or off, nothing else
            or len(positional) == 2 and positional[1] not in ("on", "off")
            or len(positional) == 1 and "days" not in opts):    # at least one of the two
        die(usage, error="invalid_argument")
    name = registry.check(positional[0])
    on = positional[1] == "on" if len(positional) == 2 else None
    days = _cleanup_days(opts["days"]) if "days" in opts else None
    with state_files.locked(paths.REGISTRY):             # no regenerate reads a half-changed registry
        with state_files.locked(registry.record_file(name)):    # no other call writes the record meanwhile
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
    state = lifecycle.vm_state(name)
    return state, None if not changed else "next_start" if state in ("shut off", virsh.MISSING) else "next_restart"


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
        with state_files.locked(registry.record_file(name)):
            with state_files.vm_lock(name):
                rec = registry.load(name)
                before = dict(rec.get("env", {}))
                after = vmenv.check(change(dict(before)), kept=before)
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


def _env_answer(name, opts, before, after, state, applies, defined, what):
    said = f"{name}: VM variables unchanged" if applies is None else f"{name}: {what}"
    if applies is None:
        activity.unchanged()
    if defined == "domain-postponed":
        # its saved memory belongs to the old definition (generate.domain); regenerate defines it
        # once the VM runs again, as for every other change of its definition
        message = (f"{said}; the VM is paused, so it is defined again only once it no longer has a Pause "
                   f"(agentbox start {name}, then agentbox regenerate), and the VM variables apply from the "
                   f"next Restart or Reset after that")
    elif applies is None:
        message = said
    elif applies == "next_start":
        message = f"{said}, applies from the next start"
    else:
        message = (f"{said}, applies from the next Restart (agentbox restart {name}) or Reset; "
                   f"until then the VM keeps the VM variables it started with")
    if "json" in opts:
        print(json.dumps({"vm": name, "env": after, "changed": applies is not None, "state": state,
                          "applies": applies, "message": message}, ensure_ascii=False))
    else:
        print(message)


def set_env(args):
    """agentbox set-env <vm> NAME=VALUE [NAME=VALUE ...] [--replace] [--json]: add or change VM
    variables; --replace makes these the whole list (none: no VM variables)."""
    positional, opts = _options(args, {"replace": False, "json": False})
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
    positional, opts = _options(args, {"json": False})
    if len(positional) < 2:
        die("unset-env <vm> NAME [NAME ...] [--json]", error="invalid_argument")
    name = registry.check(positional[0])
    names = [vmenv.valid_name(n) for n in positional[1:]]
    result = _change_env(name, lambda env: {k: v for k, v in env.items() if k not in names})
    _env_answer(name, opts, *result, _what(*result[:2]))


def env(args):
    """agentbox env <vm> [--json]: the VM's VM variables, names and values."""
    positional, opts = _options(args, {"json": False})
    if len(positional) != 1:
        die("env <vm> [--json]", error="invalid_argument")
    name = registry.check(positional[0])
    variables = registry.load(name).get("env", {})
    refused = vmenv.refused(variables)
    if "json" in opts:
        print(json.dumps({"vm": name, "env": variables, "refused": refused}, ensure_ascii=False))
    elif not variables:
        print(f"{name}: no VM variables")
    else:
        # one line each; the values are text without line breaks (vmenv.check_value)
        for n, v in variables.items():
            print(f"{n}={clean(v, False)}")
        for n in refused:
            print(f"{name}: {n} is not passed on to the VM: a Safeguard or Agent Box itself sets or reads it "
                  f"(agentbox unset-env {name} {n} removes it)")


COMMANDS = {"create": create, "delete": delete, "set-memory": set_memory,
            "set-container-image-cleanup": set_container_cleanup, "set-env": set_env,
            "unset-env": unset_env, "env": env}
