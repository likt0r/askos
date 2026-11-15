"""A VM's and a unit's state in GLOSSARY terms: the one module that reads libvirt's and
systemd's state words (#105).

VMs and their networks, from libvirt (virsh): a VM runs when libvirt reports its domain as running. A **Pause**
is a managed save of its memory (has_pause); libvirt reports such a VM as shut off, and
`status` and the Control Room call it PAUSED. libvirt's own "paused" and "pmsuspended" (a
domain suspended in memory, which Agent Box never does) are no Pause: the Control Room
calls them "suspended".

Units, from systemd (programs.systemctl): what each question about a unit's state counts.
A Build's unit counts as running while it is still stopping (build_running); a connection
to a VM's Orca port that is being closed is no connection (connection_open).
"""
import re

from . import programs, virsh
from .errors import Fail
from .registry import dom

RUNNING = "running"        # libvirt's word for a running domain, kept by `status` and the Control Room
# `status`'s and the Control Room's word for a VM with a Pause. `status` keeps libvirt's own
# "paused" (a domain suspended in memory) as it is, so has_pause() is the question for a Pause.
PAUSED = "paused"
SHUT_OFF = "shut off"
MISSING = virsh.MISSING    # libvirt has no such domain, or cannot tell

# libvirt's word -> the state the Control Room shows (a VM with a Pause is PAUSED instead)
CONTROL_ROOM = {RUNNING: "running", SHUT_OFF: "stopped", "paused": "suspended", "in shutdown": "shutting_down",
                "crashed": "crashed", "pmsuspended": "suspended", "idle": "running", "blocked": "running"}
# libvirt's words of a domain without a QEMU process
NO_PROCESS = (SHUT_OFF, "crashed", MISSING)


# --- VMs -------------------------------------------------------------------------------------

def has_pause(name):
    """Whether the VM has a Pause: libvirt holds a managed save of its memory."""
    return bool(re.search(r"^Managed save: *yes", virsh.run("dominfo", dom(name)).stdout, re.MULTILINE))


def running(name):
    """Whether libvirt reports the VM as running."""
    return virsh.state(name) == RUNNING


def status(name):
    """The state as `status` names it: PAUSED when the VM has a Pause, else libvirt's word
    (RUNNING, SHUT_OFF, ...; MISSING when libvirt has no domain or cannot tell)."""
    return PAUSED if has_pause(name) else virsh.state(name)


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
    r = virsh.run("domstate", dom(name))
    if r.returncode == 0:
        word = r.stdout.split("\n")[0].strip()
        return word == SHUT_OFF, word or "unknown"
    if "failed to get domain" in r.stderr:
        return True, "not defined"
    return False, "unknown"


def has_process(name):
    """Whether the VM has a QEMU process (running, or suspended in memory); False when
    libvirt cannot tell."""
    return virsh.state(name) not in NO_PROCESS


def network(name):
    """The VM's network as libvirt reports it (virsh net-info): (active, autostart), None when
    libvirt has no such network; raises Fail when virsh cannot tell."""
    r = virsh.run("net-info", dom(name))
    if r.returncode != 0:
        if "failed to get network" in r.stderr:
            return None
        programs.fail(r, f"{name}: virsh", program="virsh", vm=name)
    info = dict(line.split(":", 1) for line in r.stdout.splitlines() if ":" in line)
    info = {k.strip(): v.strip() for k, v in info.items()}
    return info.get("Active") == "yes", info.get("Autostart") == "yes"


# --- units -----------------------------------------------------------------------------------

def build_running(unit):
    """Whether a unit of a Build runs, or is about to: deactivating counts, a Build that is
    stopping may still hold the run lock and write its Image. False when systemd cannot say."""
    try:
        r = programs.systemctl("is-active", unit, timeout=30, ok=None)
    except Fail:
        return False
    return r.stdout.strip() in ("active", "activating", "deactivating", "reloading")


def connection_open(active):
    """Whether a connection to a VM's Orca port (an instance unit, by its ActiveState from
    list-units) counts as connected: one being closed (deactivating) does not."""
    return active in ("active", "activating", "reloading")


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
