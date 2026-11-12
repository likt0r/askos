"""A VM's lifecycle: status, start, wake, Pause, Reset, Restart, Focus, the
Reset check of the Maintenance window and the Host as Observer of the VM's Orca.

Where the Bash version let a failing program (virsh, qemu-img, the Orca CLI) end
the command (`set -e`), the port passes that program's own error message through
unchanged and exits with its exit code, instead of adding an `agentbox:` line.
"""
import collections
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

from . import activity, guest, images, paths, registry, responding, settings, state_files, virsh
from .registry import check, dom
from .errors import Fail, die

ORCA_TIMEOUT = 20


def _exit_with(r):
    """End the command like `set -e` ended the Bash version: the failed program's
    own error message on stderr, then its exit code."""
    raise Fail(r.stderr or "", r.returncode or 1, error="action_failed", raw=True)


def _must(*args):
    r = virsh.run(*args)
    if r.returncode != 0:
        _exit_with(r)


def _reporter(as_json):
    """say(line) prints at once in text mode, as the Bash version did; the lines
    are collected as well, for the JSON result."""
    lines = []

    def say(line):
        lines.append(line)
        if not as_json:
            print(line, flush=True)
    return say, lines


def _message(lines):
    return "\n".join(lines) if lines else None


def _flags(args):
    ctx = check(args[0] if args else "")
    return ctx, args[1:]


# --- what the Host knows about a VM -----------------------------------------

def system_disk(ctx):
    return os.path.join(paths.VMS, f"{ctx}-system.qcow2")


def observer_dir(ctx):
    return os.path.join(paths.STATE, f"observer-{ctx}")


def has_save(ctx):
    """Whether the VM is paused (libvirt holds a managed save of its memory)."""
    return bool(re.search(r"^Managed save: *yes", virsh.run("dominfo", dom(ctx)).stdout, re.MULTILINE))


def vm_state(ctx):
    """The state as `status --json` names it: "paused" when a managed save exists."""
    return "paused" if has_save(ctx) else virsh.state(ctx)


def _rss_fields(ctx):
    """Second fields of the rss lines of `virsh dommemstat` (KiB); empty when that fails."""
    r = virsh.run("dommemstat", dom(ctx))
    if r.returncode != 0:
        return []
    out = []
    for line in r.stdout.split("\n"):
        if line.startswith("rss"):
            f = line.split()
            out.append(f[1] if len(f) > 1 else "")
    return out


def _awk_number(s):
    """A field as awk reads it as a number: its numeric prefix, else 0."""
    m = re.match(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?", s)
    return float(m.group(0)) if m else 0.0


def _backing(ctx, missing):
    """File name of the Image Build the System disk was created from; None if unreadable."""
    try:
        r = subprocess.run(["qemu-img", "info", "-U", "--output=json", system_disk(ctx)],
                           capture_output=True, text=True, errors="replace", timeout=30)
        if r.returncode != 0:
            return None
        return os.path.basename(json.loads(r.stdout).get("backing-filename", missing))
    except Exception:
        return None


def _disk_mtime(ctx):
    try:
        return int(os.stat(system_disk(ctx)).st_mtime)
    except OSError:
        return None


def _days(seconds):
    """Whole days, truncated toward zero like shell arithmetic."""
    d = abs(seconds) // 86400
    return d if seconds >= 0 else -d


def gui_connections(ctx):
    """Established TCP connections to the VM's Orca (a laptop's GUI is attached),
    or None when ss cannot tell."""
    try:
        ip, port = registry.orca_address(ctx)
        r = subprocess.run(["ss", "-Htn", "state", "established", f"( dst {ip}:{port} )"],
                           capture_output=True, text=True, errors="replace", timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.count("\n") if r.returncode == 0 else None


# --- Orca in the VM, reached from the Host as its own Paired device ---------

def orca_cli(ctx, *args, stdout=subprocess.PIPE, stderr=subprocess.PIPE):
    env = dict(os.environ, ORCA_USER_DATA_PATH=observer_dir(ctx))
    try:
        return subprocess.run([paths.ORCA, *args], stdout=stdout, stderr=stderr, text=True,
                              errors="replace", env=env, timeout=ORCA_TIMEOUT)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", "")
    except OSError as e:
        return subprocess.CompletedProcess(args, 127, "", f"{paths.ORCA}: {e.strerror}\n")


def agent_states(ctx):
    """Agent states according to Orca (its hooks in Claude, Codex, OpenCode) as
    {state: count} sorted by state, e.g. {"waiting": 2, "working": 1}; empty
    when no agent runs; None when unknown (no Observer, Orca not answering)."""
    if not os.path.isdir(observer_dir(ctx)):
        return None
    r = orca_cli(ctx, "worktree", "ps", "--json", "--environment", ctx, stderr=subprocess.DEVNULL)
    # A failing Orca CLI means unknown. (The Bash version printed "?" twice
    # here, which broke `status`, and weekly-check went on to reset.)
    if r.returncode != 0:
        return None
    try:
        wts = json.loads(r.stdout)["result"]["worktrees"]
        c = collections.Counter(a.get("state", "?") for w in wts for a in w.get("agents") or [])
        return {str(k): v for k, v in sorted(c.items())}
    except Exception:
        return None


def agents_json(states):
    """The shape `status --json` has always had: counts as strings, {} when unknown."""
    return {k: str(v) for k, v in (states or {}).items()}


def agents_text(states):
    """The Bash form: "waiting=2 working=1", "" without agents, "?" when unknown."""
    return "?" if states is None else " ".join(f"{k}={v}" for k, v in states.items())


# --- actions -----------------------------------------------------------------

class OrcaSilent(Fail):
    """wake gave up: Orca in the VM did not answer within the time limit. started: the
    wake started the VM; entry: who recorded the Activity entry of the incident the failure
    belongs to: "own" this wake, "other" another wake or watch, "none" no incident (the
    failure was not counted), None when the wakes file failed."""

    def __init__(self, message, started, entry, vm):
        super().__init__(message, error="orca_unavailable", vm=vm)
        self.started, self.entry = started, entry


# the actor of a wake from a connection to the VM's Orca port (generate.units)
ORCA_ACTOR = re.compile(r"Orca \((.{1,64})\)")


def wake(ctx):
    """Start the VM if needed and wait until its Orca accepts connections.
    True when the VM had to be started. While a Reset, Restart, Pause or delete runs,
    wait for it; then act on the state it left. The lock is not held while waiting
    for Orca. The outcome goes to the VM's wakes file (responding.py)."""
    with state_files.vm_lock(ctx):
        started = virsh.state(ctx) != "running"
        if started:
            responding.clear(ctx)         # the failed wakes of its last start are over
            _must("start", dom(ctx))
    start = responding.start(ctx)         # the start of the VM this wake waits for
    waiting = responding.clock()
    addr = responding.orca_address(ctx)
    deadline = time.monotonic() + responding.wake_seconds()
    while time.monotonic() < deadline:
        try:
            socket.create_connection(addr, timeout=1).close()
        except OSError:
            time.sleep(1)
            continue
        _bookkeeping(responding.wake_reached, ctx, start)
        return started
    entry = _bookkeeping(_gave_up, ctx, start, waiting)
    raise OrcaSilent(f"{ctx}: Orca in the VM does not answer", started, entry, ctx)


def _bookkeeping(step, ctx, *args):
    """The wakes file and its Activity entry must not decide a connection's outcome; None
    when they failed."""
    try:
        return step(ctx, *args)
    except Fail as f:
        print(f"agentbox: {f.message}", file=sys.stderr, flush=True)
        return None
    except OSError as e:                 # e.g. the file's lock
        print(f"agentbox: {responding.wakes_file(ctx)}: {e.strerror or e}", file=sys.stderr, flush=True)
        return None


def _gave_up(ctx, start, waiting):
    """Count the failed wake; who recorded its incident's Activity entry (OrcaSilent.entry).
    The first that finds the VM not responding records an entry that says what does not
    answer (#54); the other failures are none (priv: about 1,870 in one night). A VM
    stopped, paused or started again meanwhile is not one that does not answer."""
    if virsh.state(ctx) != "running":
        return "none"
    m = ORCA_ACTOR.fullmatch(activity.actor())
    return responding.wake_failed(ctx, start, waiting, m.group(1) if m else None,
                                  lambda: responding.record_incident(ctx, guest.ping(ctx, responding.PING_SECONDS)))


def pause(ctx, say):
    with state_files.vm_lock(ctx):
        if virsh.state(ctx) != "running":
            return
        _must("managedsave", dom(ctx))
        responding.clear(ctx)
    say(f"{ctx} paused")


def _stop(ctx):
    """Discard a Pause, shut the VM down and force it off if it is still running
    after the time limit; once it is off, its failed wakes are over. The caller holds
    the VM's lifecycle lock."""
    virsh.run("managedsave-remove", dom(ctx))
    if virsh.state(ctx) == "running":
        _must("shutdown", dom(ctx))
        for _ in range(paths.tunable("AGENTBOX_SHUTDOWN_SECONDS", 60)):
            if virsh.state(ctx) != "running":
                break
            time.sleep(1)
        if virsh.state(ctx) == "running":
            _must("destroy", dom(ctx))
    responding.clear(ctx)


def _off(ctx):
    """(True, state) when the domain is shut off or libvirt has none (create defines it
    after the Reset); (False, state) otherwise, also when virsh cannot tell."""
    r = virsh.run("domstate", dom(ctx))
    if r.returncode == 0:
        st = r.stdout.split("\n")[0].strip()
        return st == "shut off", st or "unknown"
    if "failed to get domain" in r.stderr:
        return True, "not defined"
    return False, "unknown"


def _running_backing(ctx):
    """The backing file of the System disk as the running VM uses it, or None when
    libvirt does not say. libvirt fixes the chain when it starts the VM; a header
    written later does not change it (#52)."""
    r = virsh.run("dumpxml", dom(ctx))
    if r.returncode != 0:
        return None
    try:
        root = ET.fromstring(r.stdout)
    except ET.ParseError:
        return None
    want = os.path.realpath(system_disk(ctx))
    for disk in root.iter("disk"):
        source, target = disk.find("source"), disk.find("target")
        # the System disk: by its file, else by its device (generate: vda)
        if (source is not None and source.get("file") and os.path.realpath(source.get("file")) == want) \
                or (target is not None and target.get("dev") == "vda"):
            backing = disk.find("backingStore/source")
            return backing.get("file") if backing is not None else None
    return None


def _check_chain(ctx, base):
    """A VM running on another Build than the one its System disk was just created from
    writes that Build's data into the new disk and no longer boots after its next cold
    start (#52): force it off."""
    if virsh.state(ctx) != "running":
        return
    running = _running_backing(ctx)
    if running is None or os.path.basename(running) == os.path.basename(base):
        return
    virsh.run("destroy", dom(ctx))
    die(f"{ctx}: the VM ran on another Build ({os.path.basename(running)}) than its System disk "
        f"({os.path.basename(base)}) and was forced off; agentbox reset {ctx} again", error="build_mismatch", vm=ctx)


def reset(ctx, start, say):
    """Replace the System disk with a fresh copy of the VM's Image: the Build it is
    pinned to, else the Image's current Build (the Data disk stays)."""
    with state_files.vm_lock(ctx):
        _reset(ctx, start, say)


def reset_as_found(ctx, say):
    """A Reset that leaves the VM as it found it (the Resets of an Update, images.py): under
    its lifecycle lock, whether it runs is read and the Reset starts it again if it ran. When
    the Reset fails or is interrupted (KeyboardInterrupt) before the System disk is touched,
    a VM that ran is started again on its old disk; when it is interrupted after the new disk
    was made, on the new one. Returns (whether it ran, the disk file of the Build used)."""
    progress = {}
    with state_files.vm_lock(ctx):
        ran = virsh.state(ctx) == "running"
        try:
            base = _reset(ctx, ran, say, progress)
        except (Fail, KeyboardInterrupt) as e:
            safe = "disk" not in progress or (isinstance(e, KeyboardInterrupt) and "fresh" in progress)
            if ran and safe and virsh.state(ctx) != "running" and virsh.run("start", dom(ctx)).returncode == 0:
                say(f"{ctx} started again" + ("" if "fresh" in progress else " (System disk unchanged)"))
            raise
    return ran, os.path.basename(base)


def _reset(ctx, start, say, progress=None):
    """reset() for a caller that holds the VM's lifecycle lock; the disk file of the Build
    the System disk now comes from. `progress` gets "disk" once the System disk is being
    replaced and "fresh" once it is."""
    progress = {} if progress is None else progress
    _stop(ctx)
    disk = system_disk(ctx)
    sys.stdout.flush()
    # no Build goes (images.prune) between choosing it and creating the disk on it
    with state_files.locked(paths.IMAGES):
        base = images.disk_for(registry.load(ctx))
        # a VM started meanwhile (not by agentbox, which waits for the lock) keeps using
        # the old disk's chain: never write a new header under it
        off, st = _off(ctx)
        if not off:
            die(f"{ctx}: the VM is not off ({st}), the System disk stays unchanged", error="vm_not_off", vm=ctx,
                state=st)
        progress["disk"] = True
        try:
            r = subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", base, disk],
                               stdout=sys.stderr)
        except OSError:
            die("qemu-img is missing", 127, error="action_failed")
    if r.returncode != 0:
        raise Fail(f"qemu-img create fails (exit {r.returncode})", r.returncode, error="action_failed", raw=True,
                   vm=ctx)
    progress["fresh"] = True
    if os.geteuid() == 0:
        try:
            shutil.chown(disk, "qemu", "qemu")
        except (OSError, LookupError):
            pass
    say(f"{ctx}: fresh System disk ({os.path.basename(base)})")
    if start:
        r = virsh.run("start", dom(ctx))
        if r.returncode == 0:
            say(f"{ctx} started")
        _check_chain(ctx, base)
        if r.returncode != 0:
            why = " ".join(r.stderr.split())[-300:] or f"exit {r.returncode}"
            die(f"{ctx}: fresh System disk, but the start fails: {why}", error="reset_start_failed", vm=ctx)
    return base


# --- commands ----------------------------------------------------------------

def _result(ctx, as_json, lines):
    if as_json:
        print(json.dumps({"vm": ctx, "state": vm_state(ctx), "message": _message(lines)}))


def status(args):
    """agentbox status [--json]: the VMs, and what the Host lacks for its maintenance
    (maintenance.warnings): with --json under "host_warnings", only when there is something
    (no VM name has an underscore), in text after the table."""
    from . import maintenance           # maintenance imports this module
    warnings = maintenance.warnings()
    if "--json" in args:
        out = {}
        for ctx in registry.names():
            st = vm_state(ctx)
            rss = _rss_fields(ctx)
            try:
                rss_kb = int(rss[0]) if rss and rss[0] else 0
            except ValueError:
                rss_kb = 0
            base = _backing(ctx, "") or ""
            since = _disk_mtime(ctx) or 0
            states = agent_states(ctx) if st == "running" else {}
            out[ctx] = {"state": st, "rss_kb": rss_kb, "base": base, "system_disk_since": since,
                        "agents": agents_json(states),
                        "agents_unknown": states is None,
                        "open_until": state_files.open_until(ctx),
                        "claude_token_since": state_files.claude_token_since(ctx),
                        "gui_connections": gui_connections(ctx)}
        if warnings:
            out["host_warnings"] = warnings
        print(json.dumps(out))
        return
    vms = registry.names()
    row = "{:<%d} {:<12} {:<10} {:<24} {:<8} {}" % max([6, *map(len, vms)])
    print(row.format("VM", "STATE", "RAM", "BASE-DISK", "SYS-AGE", "AGENTS"), flush=True)
    for ctx in vms:
        st = virsh.state(ctx)
        if has_save(ctx):
            st = "paused"
        mem = "".join("%.1f GB" % (_awk_number(f) / 1048576) for f in _rss_fields(ctx))
        base = _backing(ctx, "-")
        now = int(time.time())
        mtime = _disk_mtime(ctx)
        age = _days(now - (mtime if mtime is not None else now))
        agents = agents_text(agent_states(ctx)) if st == "running" else "-"
        print(row.format(ctx, st, mem or "-", "-" if base is None else base, f"{age} days", agents or "none"),
              flush=True)
    for w in warnings:
        print(f"\nWarning: {w['message']}", flush=True)


def start_cmd(args):
    """agentbox start <vm> [--json]: start without waiting for Orca."""
    ctx, flags = _flags(args)
    with state_files.vm_lock(ctx):
        if virsh.state(ctx) != "running":
            responding.clear(ctx)
            _must("start", dom(ctx))
    say, lines = _reporter("--json" in flags)
    say(f"{ctx} is running")
    _result(ctx, "--json" in flags, lines)


def wake_cmd(args):
    """agentbox wake <vm> [--json]: start and wait until Orca answers."""
    ctx, flags = _flags(args)
    # every Orca connection wakes the VM; only a wake that started it is Activity, and a
    # VM that does not answer once per incident (_gave_up), not once per connection. A
    # wake that started the VM and gave up is recorded as failed unless it recorded the
    # incident's entry itself (ADR 0007: who caused the start), and so is every wake that
    # gave up when the wakes file failed (as before it existed).
    try:
        started = wake(ctx)
    except OrcaSilent as e:
        if e.entry == "own" or (e.entry is not None and not e.started):
            activity.own_entry()
        raise
    if not started:
        activity.unchanged()
    _result(ctx, "--json" in flags, [])


def pause_cmd(args):
    """agentbox pause <vm> [--json]"""
    ctx, flags = _flags(args)
    say, lines = _reporter("--json" in flags)
    pause(ctx, say)
    _result(ctx, "--json" in flags, lines)


def reset_cmd(args):
    """agentbox reset <vm> [--no-start] [--json]"""
    ctx, flags = _flags(args)
    say, lines = _reporter("--json" in flags)
    reset(ctx, "--no-start" not in flags, say)
    _result(ctx, "--json" in flags, lines)


def restart_cmd(args):
    """agentbox restart <vm> [--json]: reboot, keeping the System disk."""
    ctx, flags = _flags(args)
    with state_files.vm_lock(ctx):
        _stop(ctx)
        _must("start", dom(ctx))
    say, lines = _reporter("--json" in flags)
    say(f"{ctx} restarted")
    _result(ctx, "--json" in flags, lines)


def focus_cmd(args):
    """agentbox focus <vm|off> [--json]: pause every other VM."""
    target = args[0] if args else ""
    as_json = "--json" in args[1:]
    say, lines = _reporter(as_json)
    if not target:
        die("focus <vm>|off", error="invalid_argument")
    if target != "off":
        check(target)
    others = [] if target == "off" else [vm for vm in registry.names() if vm != target]
    for other in others:
        pause(other, say)
    if target == "off":
        say("all VMs wake up again when needed")
    if as_json:
        print(json.dumps({"focus": target, "others": [{"vm": o, "state": vm_state(o)} for o in others],
                          "message": _message(lines)}))


def weekly_check(args):
    """agentbox weekly-check [--json]: the Reset check of the Maintenance window (reset_check),
    on its own."""
    as_json = "--json" in args
    results, failed = reset_check(as_json)
    if as_json:
        print(json.dumps(results))
    if failed:
        raise Fail("", failed, error="action_failed", raw=True)


def reset_check(as_json=False):
    """The Reset check, the last step of the Maintenance window (maintenance.py): Reset VMs
    whose System disk is reset_age_days (Settings) old, and with auto_update on (#114) those
    whose Image has a newer current Build than their System disk (not those pinned to a
    Build), but only when they are idle; when in doubt (Orca not answering) postpone. Each VM
    on its own: one failing does not keep the others from being checked. Returns (one result
    per VM, the exit code of the first failure or 0); what it did goes to stdout, failures to
    stderr, unless as_json."""
    results = []
    failed = 0
    now = int(time.time())
    records, problems = registry.scan()
    for name, problem in problems:
        if not as_json:
            print(f"agentbox: {problem}", file=sys.stderr, flush=True)
        results.append({"vm": name, "action": "failed", "reason": "error", "update": None,
                        "message": f"agentbox: {problem}"})
        _record_check(name, "failed", "error", None, problem, "action_failed")
        failed = failed or 1
    for ctx in (r["name"] for r in records):
        say, lines = _reporter(as_json)
        update = failure = None
        try:
            with state_files.vm_lock(ctx):
                values = settings.load()        # broken Settings fail each VM, in Activity
                update = _update_due(ctx) if values["auto_update"] else None
                action, reason = _check_one(ctx, now, say, values["reset_age_days"], update)
        except Fail as f:
            # one VM failing must not keep the others from being checked
            failed = failed or f.exit
            text = f.message.strip() if f.raw else f"agentbox: {f.message}"
            if not as_json and text:
                print(text, file=sys.stderr, flush=True)
            lines.append(text)
            action, reason, failure = "failed", "error", f
        results.append({"vm": ctx, "action": action, "reason": reason,
                        "update": "-".join(update) if update else None, "message": _message(lines)})
        _record_check(ctx, action, reason, update, lines[-1] if lines else "", failure and failure.error,
                      failure and failure.params)
    return results, failed


CHECK_REASONS = {"idle": "idle", "stopped": "the VM was off", "gui-connected": "a device is connected",
                 "gui-unknown": "connected devices unknown", "agents-working": "agents are working",
                 "agents-unknown": "agent status unknown"}


def _record_check(ctx, action, reason, update, message, error=None, error_params=None):
    """Automatic Resets, postponed ones with their reason, and failures (with their error
    code) in Activity; VMs that were left alone are not. Parameters: the reason, and
    for a Reset onto a newer Build of the VM's Image (update: its Image and Build) the image
    and build. A done one then has the reason "update", a postponed one keeps why it was
    postponed (the VM's page words that)."""
    what = f"Automatic Reset onto Build {update[0]}-{update[1]}" if update else "Automatic Reset"
    build = {"image": update[0], "build": update[1]} if update else {}
    if action == "reset":
        activity.record("reset", "reset.automatic", f"{what} ({CHECK_REASONS[reason]})", ctx,
                        params={"reason": "update" if update else reason, **build})
    elif action == "postponed":
        activity.record("reset", "reset.postponed", f"{what} postponed ({CHECK_REASONS[reason]})",
                        ctx, params={"reason": reason, **build})
    elif action == "failed":
        activity.record("reset", "reset.failed",
                        f"{what} failed: {message.removeprefix('agentbox: ')}", ctx, "failed",
                        params={"reason": reason, **build}, error=error, error_params=error_params)


def _update_due(ctx):
    """(Image, Build) when the VM's Image has a newer current Build than the one its System
    disk came from, and the VM takes it at a Reset (no Build pin); else None, also when that
    is not known."""
    rec = registry.load(ctx)
    return images.behind(rec, _backing(ctx, "") or None, images.current(rec["image"]))


def _check_one(ctx, now, say, age_days, update):
    """What the daily check does with one VM: (action, reason). Due by age, or for an update
    (the newer Build it would take, else None)."""
    mtime = _disk_mtime(ctx) if os.path.isfile(system_disk(ctx)) else None
    if mtime is None:
        return "skipped", "no-system-disk"
    if update is None and _days(now - mtime) < age_days:
        return "skipped", "recent"
    what = f"Reset onto the new Build {update[0]}-{update[1]}" if update else "Reset"
    if virsh.state(ctx) != "running":
        _reset(ctx, False, say)
        return "reset", "stopped"
    gui = gui_connections(ctx)
    if gui is None:
        say(f"{ctx}: {what} postponed (GUI connections unknown)")
        return "postponed", "gui-unknown"
    if gui > 0:
        say(f"{ctx}: {what} postponed (GUI connected)")
        return "postponed", "gui-connected"
    states = agents_text(agent_states(ctx))
    if states == "?" or "working=" in states:
        say(f"{ctx}: {what} postponed (agents: {states})")
        return "postponed", "agents-unknown" if states == "?" else "agents-working"
    _reset(ctx, True, say)
    return "reset", "idle"


def observe(args):
    """agentbox observe <vm> [--json]: pair the Host with the VM's Orca as Observer."""
    ctx, flags = _flags(args)
    wake(ctx)
    d = observer_dir(ctx)
    try:
        os.makedirs(d, exist_ok=True)
        os.chmod(d, 0o700)
    except OSError as e:
        die(f"{d}: {e.strerror}", error="action_failed", vm=ctx)
    orca_cli(ctx, "environment", "rm", "--environment", ctx,
             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    code = guest.find_code(ctx)
    r = orca_cli(ctx, "environment", "add", "--name", ctx, "--pairing-code", code,
                 stdout=subprocess.DEVNULL, stderr=None)
    if r.returncode != 0:
        _exit_with(r)
    states = agent_states(ctx)
    line = f"{ctx}: the Host observes Orca in the VM. Agents: {agents_text(states)}"
    if "--json" in flags:
        print(json.dumps({"vm": ctx, "agents": agents_json(states), "agents_unknown": states is None, "message": line}))
    else:
        print(line)


COMMANDS = {"status": status, "start": start_cmd, "wake": wake_cmd, "pause": pause_cmd, "reset": reset_cmd,
            "restart": restart_cmd, "focus": focus_cmd, "observe": observe}
