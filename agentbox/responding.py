"""Whether a running VM answers (#54): the states `starting` and `not_responding`.

After the Host rebooted on 2026-11-04, `priv` hung in the dracut emergency shell while
libvirt reported it as running. Every connection to its Orca port waited 120 s in
`agentbox wake` and failed, about 1,870 times overnight, and the Control Room showed the
VM as running. What the Host knows about a VM libvirt reports as running:

  start        its QEMU process, named by libvirt's pid file: when it started, and which
               start it is (boot id and start in clock ticks after boot)
  Orca         answers when it accepts a connection on its port in the VM
  guest agent  asked with a guest-ping only when Orca does not answer: no answer means
               the system did not boot, an answer that it runs without Orca
  wakes file   a state file per VM for one start, written by `wake` and `watch` under its
               lock: the last time Orca was known to answer (a wake reached it, or watch
               saw it), the first failure known after that (watch's first failed look, or
               when a wake that gave up began to wait), whether that incident has its
               Activity entry, and how many wakes gave up (time and remote address of the
               last one); start, Restart, Reset, Pause and delete remove it

Times that are compared are seconds on clock(), the clock a process's start in /proc is
counted on (since the Host's boot, suspended time included), so setting the wall clock
changes no judgement; the wall clock is only for showing a time.

judge() makes the state of them: `starting` while the VM started less than the wake time
limit ago (AGENTBOX_WAKE_SECONDS, 120 s) and Orca has not answered since, `not_responding`
once Orca has not answered for that long, else `running`. describe and watch judge alike,
from their own last look and the wakes file, so they differ only in how fresh that look
is. How long Orca has not answered counts from the first failure known after its last
answer to the last look, so one failed look does not switch the VM; from the VM's start
when Orca has not answered since and was found silent within the time limit after the
start (watch looks at a VM as it starts, a wake that started it waits from then). A wake
that gave up waited the whole time limit in vain.

The first that finds the VM not responding (a wake that gives up, or watch) records the
incident's Activity entry, under the file's lock: one entry per incident, not per
connection. The incident ends when Orca answers again (a wake reaches it or watch sees it)
or the VM starts again. A wake that waited while the VM stopped or started again, or while
Orca answered a wake or watch, counts for nothing.
"""
import collections
import os
import re
import socket
import sys
import time

from . import activity, guest, paths, registry, state_files
from .errors import Fail

# what judge() makes of a VM libvirt reports as running
STATES = ("starting", "running", "not_responding")
ORCA_SECONDS = 2                 # time limit of one connection to Orca when checking
PING_SECONDS = 3                 # time limit of the guest-ping
SLACK = 2                        # seconds a start read from /proc and clock() may disagree
PROC = paths.host("/proc")
PIDS = paths.host("/run/libvirt/qemu")       # libvirt's pid file of each running domain
PID_FILE = re.compile(r"agentbox-(.+)\.pid")
ADDRESS = re.compile(r"[0-9A-Fa-f.:%]{2,64}")

# one start of a VM's QEMU process: at when it started (on clock()), id which start it is
# ("<boot id>:<start in clock ticks after boot>")
Start = collections.namedtuple("Start", "at id")


def wake_seconds():
    """How long a wake waits for Orca; as long may a VM take to answer after its start."""
    return paths.tunable("AGENTBOX_WAKE_SECONDS", 120)


def clock():
    """Seconds since the Host booted, suspended time included (CLOCK_BOOTTIME)."""
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def _boot_time():
    with open(os.path.join(PROC, "stat")) as f:
        for line in f:
            if line.startswith("btime "):
                return int(line.split()[1])
    raise ValueError("no btime")


def wall(t):
    """A time on clock() as epoch seconds, to show (None stays None)."""
    if t is None:
        return None
    try:
        return _boot_time() + t
    except (OSError, ValueError):
        return time.time() - (clock() - t)


# --- when the VM started -------------------------------------------------------------

def _domain(argv):
    """libvirt domain name from a QEMU command line (-name guest=<name>,...)."""
    try:
        value = argv[argv.index("-name") + 1]
    except (ValueError, IndexError):
        return None
    for part in value.split(","):
        if part.startswith("guest="):
            return part[len("guest="):]
    first = value.split(",")[0]
    return None if "=" in first else first


def starts():
    """{vm: Start} of the VMs whose QEMU process runs (stopped and paused VMs are missing).
    The process is the one in libvirt's pid file, which libvirt (root) writes in a
    directory only root and the qemu user can write to: any local user can name a process
    like a VM's QEMU process, so a search of /proc could be fooled."""
    try:
        with open(os.path.join(PROC, "sys/kernel/random/boot_id")) as f:
            boot_id = f.read().strip()
        tick = os.sysconf("SC_CLK_TCK")
        files = os.listdir(PIDS)
    except (OSError, ValueError):
        return {}
    out = {}
    for file in files:
        m = PID_FILE.fullmatch(file)
        if not m or not registry.NAME.fullmatch(m.group(1)):
            continue
        try:
            with open(os.path.join(PIDS, file)) as f:
                d = os.path.join(PROC, str(int(f.read().strip())))
            with open(f"{d}/cmdline", "rb") as f:
                if _domain(f.read().decode(errors="replace").split("\0")) != registry.dom(m.group(1)):
                    continue         # a pid file left behind whose number another process has now
            with open(f"{d}/stat") as f:
                # the command name in parentheses may hold spaces; field 22 is the start
                ticks = int(f.read().rpartition(")")[2].split()[19])
        except (OSError, ValueError, IndexError):
            continue                 # ended meanwhile
        out[m.group(1)] = Start(ticks / tick, f"{boot_id}:{ticks}")
    return out


def start(name):
    """The VM's current start (Start), or None."""
    return starts().get(name)


def _id(start):
    return start.id if start is not None else None


# --- asking the VM ---------------------------------------------------------------------

def orca_address(name):
    return paths.test_address("AGENTBOX_ORCA_ADDR") or registry.orca_address(name)


def orca_answers(name, seconds=ORCA_SECONDS):
    """Whether Orca in the VM accepts a connection on its port."""
    try:
        socket.create_connection(orca_address(name), timeout=seconds).close()
        return True
    except OSError:
        return False


def check(name):
    """One look at a running VM: {"orca": bool, "guest_agent": bool or None}; the guest
    agent is asked only when Orca does not answer (None: not asked)."""
    orca = orca_answers(name)
    return {"orca": orca, "guest_agent": None if orca else guest.ping(name, PING_SECONDS)}


def look(name):
    """One look at a running VM now: check() and when it began ("checked", on clock())."""
    at = clock()
    return {**check(name), "checked": at}


def judge(began, seen, known, now=None):
    """The state of a VM libvirt reports as running and what answers: (state, responding).

    Times on clock(). began: when its QEMU process started, or None; seen: the observer's
    last look at this start (look()), None before one; known: wakes() of this start, what
    the wakes and watch found ("answered": the last time Orca was known to answer, to within
    half the time limit; "silent_since": the first failure known after it; "recorded": that
    incident has its entry, so the VM was found not responding). responding:
    {"guest_agent", "orca", "since"} (since: when Orca stopped answering, as far as known),
    None before a look while no incident is known. Silence counts as long as failures prove
    it: a VM whose Orca was not seen to answer stays `starting` past the time limit until a
    look after it fails too (watch looks then, see watch.agents_loop)."""
    now = clock() if now is None else now
    limit = wake_seconds()
    if seen is not None and seen["orca"]:
        known = _answer(known, seen["checked"])
        if known["silent_since"] is None:
            return "running", {"guest_agent": None, "orca": True, "since": None}
        seen = None                  # a wake gave up after this look: as before a look
    answered, silent = known["answered"], known["silent_since"]
    if seen is not None and silent is None and answered is not None and answered >= seen["checked"]:
        return "running", {"guest_agent": None, "orca": True, "since": None}     # a wake reached it since
    ever = answered is not None and (began is None or answered >= began - SLACK)   # since the start
    if seen is None:
        if known.get("recorded"):
            return "not_responding", {"guest_agent": None, "orca": False,
                                      "since": _since(began, ever, silent, limit)}
        young = began is not None and now - began < limit
        return ("starting" if young and not ever else "running"), None
    # the failures known after the last answer (the wakes file's, the observer's own first
    # one, this look): silent from the first to the last
    failed = [t for t in (silent, seen.get("failing_since"), seen["checked"])
              if t is not None and (answered is None or t > answered)]
    since = _since(began, ever, min(failed), limit)
    responding = {"guest_agent": seen.get("guest_agent"), "orca": False, "since": since}
    if known.get("recorded") or max(failed) - since >= limit:
        return "not_responding", responding
    if began is not None and not ever and since == began:
        return "starting", responding      # until a look after the time limit failed too
    return "running", responding


def _since(began, ever, first, limit):
    """When Orca fell silent, as far as known, given the first failure known after its last
    answer: the VM's start when Orca has not answered since and that failure came within the
    time limit after the start (someone looked while it started: watch looks at once, a wake
    that started it waits from then); else that failure. One failed look at a VM nobody
    watched start (it ran before an update, or its wakes file is gone) proves nothing."""
    return began if began is not None and not ever and first - began < limit else first


# --- the incident's entry --------------------------------------------------------------

NOT_ANSWERING = {
    "system-not-booted": "The VM is not responding: its system did not boot (neither Orca nor the guest agent "
                         "answers)",
    "orca-not-answering": "The VM is not responding: Orca does not answer (the guest agent answers)",
}


def record_incident(name, guest_agent, actor=None):
    """The Activity entry of a VM that does not respond, with what does not answer as its
    reason (the guest agent answers: Orca does not; else the system did not boot)."""
    reason = "orca-not-answering" if guest_agent else "system-not-booted"
    activity.record("action", "wake.not-responding", NOT_ANSWERING[reason], name, "failed", actor=actor,
                    params={"reason": reason})


# --- the wakes file ------------------------------------------------------------------

def wakes_file(name):
    return os.path.join(paths.STATE, f"wakes-{name}.json")


def _number(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _read(name, start, any_start=False):
    """The wakes record of this start of the VM ({} when there is none, or it belongs to
    another start; start None: a start that could not be read); any_start: the record of
    whichever start was last (a VM that does not run)."""
    rec = state_files.json_object(wakes_file(name))
    if rec is None or (not any_start and rec.get("start") != _id(start)):
        return {}
    return rec


def _count(rec):
    count = rec.get("count")
    return count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else 0


def wakes(name, start, any_start=False):
    """What the wakes and watch found in this start of the VM (any_start: in its last start,
    for a VM that does not run): {"start", "answered", "silent_since", "recorded", "count",
    "last", "address"}. answered: the last time Orca was known to answer; silent_since: the
    first failure known after it; recorded: that incident has its Activity entry; count: the
    wakes that gave up, 0 without; last (when the last one gave up) is epoch seconds, to
    show; answered and silent_since are on clock(); None when unknown."""
    rec = _read(name, start, any_start)
    answered, silent = _number(rec.get("answered")), _number(rec.get("silent_since"))
    if silent is not None and answered is not None and silent <= answered:
        silent = None                # its writers keep only a failure after the last answer
    address = rec.get("address")
    return {"start": rec.get("start"), "answered": answered, "silent_since": silent,
            "recorded": silent is not None and rec.get("recorded") is True, "count": _count(rec),
            "last": _number(rec.get("last")),
            "address": address if isinstance(address, str) and ADDRESS.fullmatch(address) else None}


def _write(name, rec):
    state_files.write_json(wakes_file(name), rec, indent=None)


def _news(known, at):
    """Whether Orca answering at `at` is news for the wakes file: no answer is known in this
    start, failures are known after the last one, or the last one known is older than half
    the time limit (so a wake that waits the whole limit while watch's looks reach Orca
    finds one of them). Not every look and every connection is a write."""
    return (known["answered"] is None or known["silent_since"] is not None
            or known["answered"] < at - wake_seconds() / 2)


def _answer(known, at):
    """known after Orca answered at `at`: the failures known before are over, and their
    incident; a failure known after `at` stands."""
    answered = at if known["answered"] is None else max(known["answered"], at)
    if known["silent_since"] is not None and known["silent_since"] > answered:
        return {**known, "answered": answered}
    return {**known, "answered": answered, "silent_since": None, "recorded": False}


def wake_reached(name, waited_for):
    """A wake that waited for Orca of the VM's start `waited_for` reached it: the failed
    wakes of that start and its incident are over, and Orca is known to have answered in
    it. Written only when that is news."""
    at = clock()
    known = wakes(name, waited_for)
    if not (_news(known, at) or known["count"]):
        return
    with state_files.locked(wakes_file(name)):
        current = start(name)
        if _id(current) != _id(waited_for):
            return                   # the VM stopped or started again meanwhile
        known = wakes(name, current)
        if not (_news(known, at) or known["count"]):
            return
        _write(name, {**_answer(known, at), "start": _id(current), "count": 0, "last": None, "address": None})


def wake_failed(name, waited_for, waiting, address, incident):
    """A wake that waited for Orca since `waiting` (on clock()), at the VM's start
    `waited_for`, gave up: count it with its time and remote address. Not counted when the
    VM stopped or started again meanwhile (a Restart or Reset ends an incident), or when Orca
    answered another wake or watch while this one waited. The wake waited the whole time
    limit in vain, so the VM does not respond: when its incident has no entry yet,
    incident() records it. It runs under the file's lock, so that the wakes and watch record
    it once, and the incident counts as recorded only once it returned: a wake ended on the
    way (systemd's time limit) leaves it to the next. "own": this wake recorded the
    incident's entry; "other": another wake or watch did; "none": not counted."""
    now = int(time.time())
    if address is not None and not ADDRESS.fullmatch(address):
        address = None
    with state_files.locked(wakes_file(name)):
        current = start(name)
        if _id(current) != _id(waited_for):
            return "none"
        known = wakes(name, current)
        if known["answered"] is not None and known["answered"] >= waiting:
            return "none"            # Orca answered another wake or watch while this one waited
        silent = waiting if known["silent_since"] is None else min(known["silent_since"], waiting)
        rec = {**known, "start": _id(current), "silent_since": silent, "count": known["count"] + 1,
               "last": now, "address": address}
        _write(name, rec)
        if rec["recorded"]:
            return "other"
        incident()
        _write(name, {**rec, "recorded": True})
    return "own"


def looked(name, at_start, seen, incident):
    """watch looked at the VM's start `at_start` (look()): what is news goes into the wakes
    file, for its next look, describe and the wakes. An answer ends the failures known and
    their incident (written when it is news, see _news); the first failure after an answer
    is when Orca fell silent. When that makes the VM not respond (judge) and its incident has
    no entry yet, incident() records it, under the file's lock as in wake_failed. True when
    the file changed."""
    at = seen["checked"]
    with state_files.locked(wakes_file(name)):
        current = start(name)
        if _id(current) != _id(at_start):
            return False             # the VM stopped or started again meanwhile
        known = wakes(name, current)
        if seen["orca"]:
            if not _news(known, at):
                return False
            _write(name, {**_answer(known, at), "start": _id(current)})
            return True
        if known["answered"] is not None and known["answered"] >= at:
            return False             # a wake reached Orca after this look began
        silent = at if known["silent_since"] is None else min(known["silent_since"], at)
        rec = {**known, "start": _id(current), "silent_since": silent}
        state, _ = judge(current and current.at, seen, rec)
        due = state == "not_responding" and not rec["recorded"]
        if silent == known["silent_since"] and not due:
            return False
        _write(name, rec)
        if due:
            incident()
            _write(name, {**rec, "recorded": True})
    return True


def clear(name):
    """Start, Restart, Reset, Pause, delete: the failed wakes are over. Called once the VM
    is off, so that a wake giving up while it shuts down does not begin a new incident.
    Bookkeeping only: a file that cannot be removed is reported, the action goes on; a
    caller that may not write agentbox's state (`pair` without root) leaves it alone."""
    if not os.access(paths.STATE, os.W_OK):
        return
    try:
        with state_files.locked(wakes_file(name)):
            os.unlink(wakes_file(name))
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"agentbox: {wakes_file(name)}: {e.strerror}", file=sys.stderr, flush=True)
    except Fail as f:
        print(f"agentbox: {f.message}", file=sys.stderr, flush=True)
