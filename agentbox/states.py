"""A VM's and a unit's state in GLOSSARY terms: the one module that reads libvirt's and
systemd's state words (#105).

VMs and their networks, from libvirt (virsh): a VM runs when libvirt reports its domain as
"running", "idle" or "blocked"; `status` and the Control Room call all three "running". A
**Pause** is a managed save of its memory (has_pause); libvirt reports such a VM as shut off,
and `status` and the Control Room call it PAUSED. libvirt's own "paused" and "pmsuspended" (a
domain suspended in memory, which Agent Box never does) are no Pause: the Control Room calls
them "suspended".

Units, from systemd (programs.systemctl): what each question about a unit's state counts.
A Build's unit counts as running while it is still stopping (build_running, build_active); a
connection to a VM's Orca port that is being closed is no connection (connection_open). The
Control Room gets a unit's state as unit_state() names it and whether the update runs, and
compares no systemd word itself (#115).
"""
import re

from . import programs, virsh
from .errors import Fail

RUNNING = "running"        # the canonical word for a running domain in `status` and the Control Room
RUNNING_WORDS = (RUNNING, "idle", "blocked")     # libvirt's words for a domain that is live
# `status`'s and the Control Room's word for a VM with a Pause. `status` keeps libvirt's own
# "paused" (a domain suspended in memory) as it is, so has_pause() is the question for a Pause.
PAUSED = "paused"
SHUT_OFF = "shut off"
MISSING = virsh.MISSING    # libvirt has no such domain, or cannot tell

# libvirt's word -> the state the Control Room shows (a VM with a Pause is PAUSED instead)
CONTROL_ROOM = {**dict.fromkeys(RUNNING_WORDS, "running"), SHUT_OFF: "stopped", "paused": "suspended",
                "in shutdown": "shutting_down", "crashed": "crashed", "pmsuspended": "suspended"}
# libvirt's words of a domain without a QEMU process
NO_PROCESS = (SHUT_OFF, "crashed", MISSING)


# --- VMs -------------------------------------------------------------------------------------

def has_pause(name):
    """Whether the VM has a Pause: libvirt holds a managed save of its memory."""
    return virsh.has_managed_save(name)


def running(name):
    """Whether libvirt reports the VM as running (including idle or blocked)."""
    return virsh.state(name) in RUNNING_WORDS


def status(name):
    """The state as `status` names it: PAUSED when the VM has a Pause, RUNNING for every live
    domain, else libvirt's word (SHUT_OFF, ...; MISSING when libvirt has no domain or cannot tell)."""
    if has_pause(name):
        return PAUSED
    word = virsh.state(name)
    return RUNNING if word in RUNNING_WORDS else word


def control_room(word, pause):
    """The state the Control Room shows for libvirt's word and whether the VM has a Pause;
    "unknown" for a word it does not know (MISSING among them)."""
    return PAUSED if pause else CONTROL_ROOM.get(word, "unknown")


def starts_anew(word):
    """Whether a VM in this state (status()'s word) reads its definition at its next start:
    it is off, or libvirt has no domain. A running VM reads it at its next restart, and so
    does a paused one, which resumes with the definition it started with."""
    return word in (SHUT_OFF, MISSING)


def off(name):
    """(True, word) when the VM is shut off or libvirt has no domain; (False, word)
    otherwise, also when virsh cannot tell. word: libvirt's, "not defined" or "unknown"."""
    word = virsh.domain_state(name)
    if word is None:
        return True, "not defined"
    return word == SHUT_OFF, word


def gone(name):
    """Whether libvirt has no domain of the name (a transient one that ended, a build VM's);
    False when virsh cannot tell."""
    return off(name)[1] == "not defined"


def has_process(name):
    """Whether the VM has a QEMU process (running, or suspended in memory); False when
    libvirt cannot tell."""
    return virsh.state(name) not in NO_PROCESS


def network(name):
    """The VM's network as libvirt reports it (virsh net-info): (active, autostart), None when
    libvirt has no such network; raises Fail when virsh cannot tell."""
    return virsh.network_info(name)


# --- units -----------------------------------------------------------------------------------

# systemd's ActiveStates of a unit that runs or is about to
UNIT_RUNNING = ("active", "activating", "reloading")
STOPPING = "deactivating"
# systemd's LoadStates of a unit it does not find or cannot load
UNIT_NOT_LOADED = ("not-found", "bad-setting", "error")


def build_active(active):
    """Whether a unit of a Build in this ActiveState runs, or is about to: deactivating counts,
    a Build that is stopping may still hold the run lock and write its Image."""
    return active in UNIT_RUNNING or active == STOPPING


def build_running(unit):
    """build_active() of the unit as systemd reports it now; False when systemd cannot say."""
    try:
        r = programs.systemctl("is-active", unit, timeout=30, ok=None)
    except Fail:
        return False
    return build_active(r.stdout.strip())


def connection_open(active):
    """Whether a connection to a VM's Orca port (an instance unit, by its ActiveState from
    list-units) counts as connected: one being closed (deactivating) does not."""
    return active in UNIT_RUNNING


def unit_state(load, active):
    """The state the Control Room shows for a unit, from its LoadState and ActiveState:
    "failed" (it failed, or systemd has no such unit or cannot load it), "running" (also
    while it starts or reloads), "stopping", else "stopped"."""
    if active == "failed" or load in UNIT_NOT_LOADED:
        return "failed"
    if active in UNIT_RUNNING:
        return "running"
    return "stopping" if active == STOPPING else "stopped"


def unit_text(active, sub):
    """systemd's words for a unit as the Control Room shows them: "active (running)", or
    the ActiveState alone when the SubState says nothing more."""
    return f"{active} ({sub})" if sub and sub != active else active


def unit_stopped(unit, timeout):
    """Whether systemd says the unit is not running (inactive, or failed; one it does not know is
    inactive); False when it says anything else or cannot say."""
    try:
        r = programs.systemctl("is-active", unit, timeout=timeout, ok=None)
    except Fail:
        return False
    return r.stdout.strip() in ("inactive", "failed")


def started(unit, timeout):
    """Whether the unit runs (systemctl is-active); a failure of systemctl itself raises Fail."""
    return programs.systemctl("is-active", unit, timeout=timeout, ok=None).returncode == 0


def enabled(unit, timeout):
    """Whether the unit is enabled (systemctl is-enabled); a failure of systemctl itself raises Fail."""
    return programs.systemctl("is-enabled", unit, timeout=timeout, ok=None).stdout.strip() == "enabled"


def failed_since(unit, since):
    """Whether the unit began a run at or after `since` (epoch seconds) that failed (systemd's
    Result of its latest run, which left the inactive state then). False when systemd cannot say."""
    try:
        r = programs.systemctl("show", "--timestamp=unix", "--property=Result,InactiveExitTimestamp", "--", unit,
                               timeout=30)
    except Fail:
        return False
    props = dict(line.partition("=")[::2] for line in r.stdout.splitlines())
    began = re.fullmatch(r"@(\d+)", props.get("InactiveExitTimestamp", "").strip())
    result = props.get("Result", "").strip()
    return began is not None and int(began.group(1)) >= since and result not in ("", "success")
