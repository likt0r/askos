"""Every Host path and runtime resource that belongs to one VM, and the delete that
removes them (ADR 0001: the VM is the unit of separation).

create, describe, regenerate and the commands ask the path functions here; paths(record)
lists every per-VM path a delete leaves behind empty. libvirt's domain and network and the
systemd units are the runtime resources; delete stops and removes them through virsh and
systemctl.

registry and state_files import this module and the Control Room loads those as leaf
modules, so it imports only leaf modules at the top, and registry (which needs settings
first) and the commands' modules (generate, lifecycle, allowlist, responding) when a
function runs.
"""
import datetime
import fcntl
import os
import re
import shutil

from . import list_records, paths as host_paths, state_files
from .errors import die


def record_file(name):
    return os.path.join(host_paths.REGISTRY, f"{name}.json")


def data_disk(name):
    return os.path.join(host_paths.VMS, f"{name}-data.qcow2")


def system_disk(name):
    return os.path.join(host_paths.VMS, f"{name}-system.qcow2")


SYSTEM_DISK = re.compile(r"(.+)-system\.qcow2")      # a System disk's file name, group 1 the VM


def archive_path(name):
    """A new, collision-free name in the archive for the VM's Data disk."""
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = os.path.join(host_paths.ARCHIVE, f"{name}-data-{stamp}")
    path, n = base + ".qcow2", 1
    while os.path.lexists(path):
        n += 1
        path = f"{base}-{n}.qcow2"
    return path


def delete_marker(name):
    """The record and progress of a delete, present until its last step finishes."""
    return os.path.join(host_paths.STATE, "pending", f"delete-{name}.json")


def legacy_owners_file(name):
    return os.path.join(host_paths.CONF, "github", f"owners-{name}")


def legacy_allowlist_file(name):
    return os.path.join(host_paths.CONF, list_records.LEGACY_ALLOWLIST[1].format(name))


def legacy_allowlist_migrated_file(name):
    return legacy_allowlist_file(name) + ".migrated"


def observer_dir(name):
    return os.path.join(host_paths.STATE, f"observer-{name}")


def open_web_file(name):
    return os.path.join(host_paths.CONF, list_records.OPEN_WEB.format(name))


def claude_token_file(name):
    return os.path.join(host_paths.CONF, "secrets", f"claude-{name}")


def vm_env_file(name):
    """The VM variables as QEMU passes them in (fw_cfg opt/agentbox/env)."""
    return os.path.join(host_paths.STATE, "vm-env", f"{name}.env")


def ssh_host_key_file(name):
    """The VM's SSH host key, made once by the Host (ADR 0020)."""
    return os.path.join(host_paths.CONF, "secrets", f"ssh-host-key-{name}")


def vm_ssh_file(name):
    """SSH access, the SSH keys and the host key as QEMU passes them in (fw_cfg opt/agentbox/ssh)."""
    return os.path.join(host_paths.STATE, "vm-ssh", f"{name}.ssh")


def seen_file(name):
    return os.path.join(host_paths.STATE, f"alerts-seen-{name}.json")


def wakes_file(name):
    return os.path.join(host_paths.STATE, f"wakes-{name}.json")


def domain_file(name):
    """The last domain XML generated for the VM."""
    return os.path.join(host_paths.STATE, "domains", f"{name}.xml")


QEMU_PIDS = host_paths.host("/run/libvirt/qemu")    # libvirt's pid file of each running domain
QEMU_PID_FILE = re.compile(r"agentbox-(.+)\.pid")   # its file name, group 1 the VM


def qemu_pid_file(name):
    """The QEMU process's pid file, owned by libvirt."""
    return os.path.join(QEMU_PIDS, f"agentbox-{name}.pid")


def lifecycle_lock(name):
    """What state_files.vm_lock locks: the VM's lifecycle lock (its file lives in locks/)."""
    return f"lifecycle-{name}"


UNIT_KINDS = ("proxy.socket", "proxy.service", "ghvm.socket", "ghvm@.service", "gui.socket", "gui@.service",
              "ssh.socket", "ssh@.service")
# the units of the SSH port exist only while the VM has SSH access (ADR 0020)
SSH_UNIT_KINDS = ("ssh.socket", "ssh@.service")
UNIT_TEMPLATES = {
    "proxy.socket": "agentbox-proxy-{vm}.socket",
    "proxy.service": "agentbox-proxy-{vm}.service",
    "ghvm.socket": "agentbox-ghvm-{vm}.socket",
    "ghvm@.service": "agentbox-ghvm-{vm}@.service",
    "gui.socket": "agentbox-gui-{vm}.socket",
    "gui@.service": "agentbox-gui-{vm}@.service",
    "ssh.socket": "agentbox-ssh-{vm}.socket",
    "ssh@.service": "agentbox-ssh-{vm}@.service",
}


def unit_name(name, kind):
    return UNIT_TEMPLATES[kind].format(vm=name)


def unit_names(rec):
    return tuple(unit_name(rec["name"], kind) for kind in UNIT_KINDS)


def unit_file(unit):
    return os.path.join(host_paths.SYSTEMD, unit)


def sockets(rec, every=False):
    """The VM's sockets: the SSH port's only while it has SSH access, or with every=True."""
    ssh = every or rec.get("ssh_access", False)
    return [unit_name(rec["name"], kind) for kind in ("proxy.socket", "ghvm.socket", "gui.socket", "ssh.socket")
            if ssh or kind != "ssh.socket"]


def socket_pending_file(socket):
    """A changed socket unit that regenerate has not rebound yet."""
    return os.path.join(host_paths.STATE, "pending", f"socket-{socket}")


def mark_socket(socket):
    path = socket_pending_file(socket)
    state_files.directory(os.path.dirname(path), 0o700, only_new=True)
    open(path, "w").close()


def transfer_file(name, transfer_id):
    return os.path.join(host_paths.TRANSFERS, f"{name}-{transfer_id}.tar")


def transfer_files(name):
    """Import archives of this VM, never those of a VM whose name extends it."""
    pattern = re.compile(re.escape(name) + r"-[0-9a-f]{12}\.tar")
    try:
        names = os.listdir(host_paths.TRANSFERS)
    except OSError:
        return ()
    return tuple(os.path.join(host_paths.TRANSFERS, f) for f in sorted(names) if pattern.fullmatch(f))


def unit_instances(name):
    """Patterns of the instances the VM's Orca port, SSH port and token service start per connection."""
    return tuple(unit_name(name, kind).replace("@.", "@*.") for kind in ("gui@.service", "ssh@.service",
                                                                          "ghvm@.service"))


def unit_files(rec):
    return tuple(unit_file(unit) for unit in unit_names(rec))


def socket_pending_files(rec):
    return tuple(socket_pending_file(socket) for socket in sockets(rec, every=True))


def paths(rec):
    """Every fixed per-VM path a delete removes.

    The delete marker belongs here too, although it exists only while a delete runs. Left out:
    the archive (delete keeps the Data disk there), import archives (transfer_files: delete
    removes those no running import holds), lock files (state_files.locked; unlinking one
    another process holds would break its lock) and QEMU's pid file (libvirt's own).
    """
    name = rec["name"]
    return (record_file(name), data_disk(name), system_disk(name), delete_marker(name),
            legacy_owners_file(name), legacy_allowlist_file(name), legacy_allowlist_migrated_file(name),
            observer_dir(name), open_web_file(name), claude_token_file(name), vm_env_file(name),
            ssh_host_key_file(name), vm_ssh_file(name), seen_file(name), wakes_file(name), domain_file(name),
            *unit_files(rec), *socket_pending_files(rec))


def being_deleted():
    """The markers of VMs being deleted; dies on a broken one."""
    from . import registry
    folder = os.path.dirname(delete_marker(""))
    markers = []
    for f in state_files.listdir(folder, fail=False):
        m = re.fullmatch(r"delete-(.+)\.json", f)
        if not m:
            continue
        path = delete_marker(m.group(1))
        marker = state_files.json_object(path)
        if marker is None or registry.problem(marker.get("record"), m.group(1)):
            die(f"{path}: broken marker", error="action_failed", path=path)
        markers.append(marker)
    return markers


def sweep_transfer(path):
    """Unlink an import archive nobody holds a lock on any more (its import was killed
    hard); a running import keeps its file and deletes it itself."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.unlink(path)
    except OSError:
        pass                                 # locked by a running import, or gone
    finally:
        os.close(fd)


def _write_marker(name, marker):
    path = delete_marker(name)
    state_files.directory(os.path.dirname(path), 0o700, only_new=True)
    state_files.write_json(path, marker, 0o600)


def _begin(name, say):
    """The VM's marker, written before its record is removed: (marker, resumed)."""
    from . import registry
    marker = next((m for m in being_deleted() if m["record"]["name"] == name), None)
    record = record_file(name)
    if not os.path.exists(record):
        if marker is None:
            die(f"No such VM: {name}", error="vm_not_found", vm=name)
        say(f"{name}: resuming the delete")
        return marker, True
    with state_files.locked(record):         # no set-owners writes it back meanwhile
        rec = registry.load(name)
        # a record that came back (migrate, by hand) means its network may be back as well
        marker = {"archive": archive_path(name), **(marker or {}), "record": rec, "network_removed": False}
        _write_marker(name, marker)
        state_files.remove(record)
    say(f"{name}: removed from the registry")
    return marker, False


def _units_off(rec, say):
    """The Orca port, SSH port, proxy and token service of the VM stop and their units go."""
    from . import programs
    name = rec["name"]
    files = list(unit_files(rec))
    present = [s for s in sockets(rec, every=True) if os.path.exists(unit_file(s))]
    if present:
        programs.systemctl("disable", "--now", *present)        # the proxy service stops with its socket
    # open connections run in instances of their own (patterns: nothing to stop is fine)
    programs.systemctl("stop", *unit_instances(name))
    removed = [f for f in files if state_files.remove(f)]
    programs.systemctl("daemon-reload")
    if present or removed:
        say(f"{name}: Orca port, proxy and token service turned off")


def _domain_off(name, say):
    from . import generate, lifecycle, programs, registry, virsh
    r = virsh.run("domuuid", registry.dom(name))
    if r.returncode != 0:
        if "failed to get domain" in r.stderr:      # libvirt has none
            return
        programs.fail(r, f"{name}: virsh", program="virsh", vm=name)
    # no wake starts it between the stop and the undefine (a running domain would outlive it)
    with state_files.vm_lock(name):
        lifecycle.stop(name)        # discards a Pause, shuts down, forces off after the time limit
        generate.virsh_checked(f"{name}: removing the VM", "undefine", registry.dom(name), "--managed-save")
    say(f"{name}: VM stopped and removed")


def _network_off(name, say):
    from . import generate, registry, states
    found = states.network(name)
    if found is None:
        return
    if found[0]:                                    # active
        generate.virsh_checked(f"{name}: stopping the network", "net-destroy", registry.dom(name))
    generate.virsh_checked(f"{name}: removing the network", "net-undefine", registry.dom(name))
    say(f"{name}: network {registry.dom(name)} removed")


def _disks(name, archive, delete_data, say):
    """System disk deleted; Data disk archived or deleted. Returns (what, archive path)."""
    if state_files.remove(system_disk(name)):
        say(f"{name}: System disk deleted")
    data = data_disk(name)
    if not os.path.lexists(data):
        if os.path.lexists(archive):         # archived before the delete was interrupted
            say(f"{name}: Data disk archived: {archive}")
            return "archived", archive
        return "missing", None
    if delete_data:
        state_files.remove(data)
        say(f"{name}: Data disk deleted")
        return "deleted", None
    try:
        state_files.directory(host_paths.ARCHIVE, 0o700, only_new=True)
        if os.path.lexists(archive):
            die(f"{archive} exists already", error="action_failed", vm=name)
        os.rename(data, archive)
    except OSError as e:
        die(f"moving the Data disk to {archive}: {e.strerror}", error="action_failed", vm=name)
    say(f"{name}: Data disk archived: {archive}")
    return "archived", archive


def _files(rec, say):
    from . import allowlist, responding
    name = rec["name"]
    responding.clear(name)           # bookkeeping only, never fails (the stop removed it already)
    gone = [state_files.remove(f) for f in (claude_token_file(name), vm_env_file(name),
                                            ssh_host_key_file(name), vm_ssh_file(name),
                                            open_web_file(name), legacy_owners_file(name),
                                            seen_file(name), domain_file(name))]
    gone.append(allowlist.drop_vm(name))
    observer = observer_dir(name)
    if os.path.isdir(observer):
        try:
            shutil.rmtree(observer)
        except OSError as e:
            die(f"{observer}: {e.strerror}", error="action_failed", vm=name)
        gone.append(True)
    if any(gone):
        say(f"{name}: Claude token, VM variables, SSH host key, Allowlist entries and Observer removed")
    # stale markers and import archives, not named in the message above
    for path in socket_pending_files(rec):
        state_files.remove(path)
    for path in transfer_files(name):
        sweep_transfer(path)


def delete(name, delete_data, say):
    """Remove every Host resource of one VM; (Data disk result, archive, resumed).

    Callers hold no VM locks. The registry lock spans the entire operation, exactly as
    regenerate does. The marker makes every failure before its last removal resumable.
    """
    from . import generate, registry
    with state_files.locked(host_paths.REGISTRY):
        registry.all_records()           # a broken registry stops delete before any change
        generate.ports_free()            # so does a service holding the resolver's or the proxy's ports
        marker, resumed = _begin(name, say)
        _units_off(marker["record"], say)
        _domain_off(name, say)
        _network_off(name, say)
        if not marker["network_removed"]:
            marker["network_removed"] = True     # from now on regenerate leaves its nftables rule out
            _write_marker(name, marker)
        data, archive = _disks(name, marker["archive"], delete_data, say)
        _files(marker["record"], say)
        for line in generate.report(generate.apply()):
            say(line)
        state_files.remove(delete_marker(name))
    return data, archive, resumed
