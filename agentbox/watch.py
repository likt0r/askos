"""`agentbox watch`: what happens on the Host, as one JSON line per event on stdout.

The Control Room's backend runs it once through sudo and feeds every connected
browser from it (Server-Sent Events). It changes nothing but a VM's wakes file, and
records Activity only for a VM it finds not responding (responding.py).
Each line is {"event": <name>, "time": <ISO time>, "data": {...}}:

  vms          every VM as `agentbox describe --json` shows it, and broken records;
               first, and again when a VM appears or goes or a record breaks
  vm           one VM whose object changed (state, agents, what answers, devices, ...)
  services     the Host's agentbox units and Ollama, with the timers and sockets that
               start each, and what the Host lacks for its maintenance (warnings, as
               `status --json` names them); first, and on every change
  activity     a new Activity entry, as `agentbox activity --json` shows it
  alert        a VM tried to reach a suspicious destination (the proxy's ALERT line)
  github-token the token service issued, refused or failed a GitHub token
  build        a Build's record changed (started, next step, done, failed, rejected)
  build-log    new lines in a running Build's journal, gathered for a moment
  host-reboot  whether updates need a Host reboot (dnf needs-restarting), checked rarely

VM state comes from polling (every few seconds, agents less often), not from libvirt's
events: the object also holds what libvirt does not know (agents, devices on the Orca
port, System disk, registry, Open web), one loop sees all of it the same way, and a
restarted libvirtd or a missed event changes nothing. Whether a running VM's Orca
answers is asked with the agents; what is news (when it answered, the first failure after
that) goes into the VM's wakes file for its start, where describe, a watch started later
and the wakes find it, so `starting` and `not_responding` (responding.py) follow the time
limit, not a single failed look. The first to find a VM not responding, watch or a wake,
records the incident's Activity entry (actor `watch` for watch); a VM that starts is asked
at once. Activity, Alerts and GitHub tokens come from following
the journald namespace agentbox, Build lines from following the agentbox-build@ units'
journal; after a follower restarts it continues after the last entry it saw.

Nothing secret leaves: the entries are cleaned like `agentbox activity` and
`agentbox build-log` clean them, and every string of every line passes through the
token, key and Pairing-code patterns once more on the way out.

watch ends when stdin is a pipe that closes (the backend went away), on SIGTERM, or
when nobody reads its stdout any more.
"""
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time

from . import activity, describe, images, lifecycle, maintenance, paths, registry, responding, times
from .errors import Fail, die
from .sanitize import PAIR_ANY, SECRET, entry as journal_entry, field

POLL = paths.tunable("AGENTBOX_WATCH_POLL_MS", 3000) / 1000
AGENTS = paths.tunable("AGENTBOX_WATCH_AGENTS_MS", 15000) / 1000
REBOOT = paths.tunable("AGENTBOX_WATCH_REBOOT_MS", 6 * 3600 * 1000) / 1000
RETRY = paths.tunable("AGENTBOX_WATCH_RETRY_MS", 5000) / 1000
MAX_RETRY = max(RETRY, 300)
BATCH = paths.tunable("AGENTBOX_WATCH_BATCH_MS", 500) / 1000
MAX_BATCH = 200                      # Build lines per build-log event; more: truncated, the rest via build-log
BUILD_UNIT = re.compile(r"agentbox-build@([a-z0-9-]{1,32})\.service")
GITHUB_ACTIONS = {"github.token": "issued", "github.refused": "refused", "github.error": "error"}
ACTOR = "watch"                      # of the entry of a VM watch found not responding

_lock = threading.Lock()             # stdout and the child processes
_children = set()


def _scrub(v):
    """Every string without anything that looks like a token, key or Pairing code."""
    if isinstance(v, str):
        return PAIR_ANY.sub("orca://pair?[hidden]", SECRET.sub("[hidden]", v))
    if isinstance(v, dict):
        return {k: _scrub(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_scrub(x) for x in v]
    return v


def emit(event, data):
    line = json.dumps({"event": event, "time": times.iso(time.time()), "data": _scrub(data)})
    with _lock:
        try:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
            gone = False
        except (BrokenPipeError, OSError, ValueError):
            gone = True
    if gone:                         # nobody reads any more
        _end(0)


def warn(text):
    try:
        print(f"agentbox watch: {text}", file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def _end(code):
    """Stop the journal followers and leave at once (from any thread)."""
    with _lock:
        children = list(_children)
    for p in children:
        try:
            p.terminate()
        except OSError:
            pass
    os._exit(code)


def _public(e):
    return {k: v for k, v in e.items() if not k.startswith("_")}


# --- VMs, services, Builds: polled -------------------------------------------------

def _build_view(rec):
    return {"image": rec.get("image"), "build": rec.get("build"), "status": rec.get("status"),
            "step": rec.get("step"), "started": times.iso(rec.get("started")),
            "finished": times.iso(rec.get("finished")), "release": rec.get("release"),
            "commit": rec.get("commit"), "base_build": rec.get("base_build"), "error": rec.get("error"),
            "problems": rec.get("problems") or [], "actor": rec.get("actor"), "unit": rec.get("unit")}


class Poller:
    def __init__(self):
        self.vms = None              # name -> object, as last sent
        self.problems = None
        self.services = None
        self.builds = None           # (image, build) -> view
        self.running = {}            # unit -> (image, build) of running Builds
        self.states = {}             # name -> state, for the agents thread
        self.agents = {}             # name -> {state: count} or None
        self.answers = {}            # name -> its last look (responding.look), at which start, first failure
        self.unkept = {}             # name -> why its last look is not in its wakes file
        self.backing = describe.Backing()
        self.triggers = describe.Triggers()
        self.wake = threading.Event()
        self.look = threading.Event()  # a VM started: the agents thread looks now, not after its pause
        self.lock = threading.Lock()

    def observed(self, name):
        with self.lock:
            return self.agents.get(name), self.answers.get(name)

    def poll(self):
        if self.builds is None:
            self.poll_builds()      # the baseline of Build records comes before the first "vms" event, which
                                    # is what a client (or test) waits for before it changes a record
        records, problems = registry.scan()
        units = describe.list_units(describe.UNIT_PATTERNS)
        snap = describe.snapshot(records, problems, units, self.observed, self.backing)
        vms = {v["name"]: v for v in snap["vms"]}
        with self.lock:
            before, self.states = self.states, {n: v["state"] for n, v in vms.items()}
        if self.vms is not None and any(st in responding.STATES and before.get(n) not in responding.STATES
                                        for n, st in self.states.items()):
            self.look.set()
        if self.vms is None or list(vms) != list(self.vms) or snap["problems"] != self.problems:
            emit("vms", snap)
        else:
            for name, v in vms.items():
                if v != self.vms[name]:
                    emit("vm", v)
        self.vms, self.problems = vms, snap["problems"]
        if units is not None:
            found = {"units": describe.services(units, self.triggers), "warnings": maintenance.warnings()}
            if found != self.services:
                emit("services", found)
                self.services = found
        self.poll_builds()

    def poll_builds(self):
        records = images._records()
        views = {(r["image"], r["build"]): _build_view(r) for r in records}
        if self.builds is not None:
            for key, view in sorted(views.items(), key=lambda kv: kv[1]["started"] or ""):
                if self.builds.get(key) != view:
                    emit("build", view)
        self.builds = views
        with self.lock:
            self.running = {r.get("unit"): (r["image"], r["build"]) for r in records if r.get("status") == "running"}

    def build_of(self, unit):
        with self.lock:
            return self.running.get(unit)

    def agents_loop(self):
        """Whether Orca answers and the agent states it reports (slow, so apart from the
        poll), for the VMs libvirt reports as running; when Orca does not answer, whether
        the guest agent does. What is news goes into the VM's wakes file, with the
        incident's Activity entry when the VM does not respond (responding.py)."""
        while True:
            with self.lock:
                running = [n for n, st in self.states.items() if st in responding.STATES]
                # forgotten only with the VM: a VM that was suspended for a moment is the same
                # start, one that stopped and started again is not (describe.vm)
                self.answers = {n: a for n, a in self.answers.items() if n in self.states}
            # a look belongs to one start (also one that began between two rounds, unseen by
            # the poll)
            starts = responding.starts()
            changed = False
            for name in running:
                start = starts.get(name)
                try:
                    seen = responding.look(name)
                except Exception as e:          # one VM must not stop the others
                    warn(f"{name}: answer unknown ({type(e).__name__})")
                    seen = None
                agents = None
                if seen and seen["orca"]:
                    try:
                        agents = lifecycle.agent_states(name)
                    except Exception as e:
                        warn(f"{name}: agents unknown ({type(e).__name__})")
                kept = seen is not None and self.keep(name, start, seen)
                with self.lock:
                    if name not in self.agents or self.agents[name] != agents:
                        self.agents[name] = agents
                        changed = True
                    if seen is not None:
                        before = self.answers.get(name)
                        # its own first failed look since Orca last answered it in this start,
                        # for when the wakes file cannot be written
                        failing = None
                        if not seen["orca"]:
                            same = before is not None and before["start"] == (start and start.id) and not before["orca"]
                            failing = (before["failing_since"] if same else None) or seen["checked"]
                        after = {**seen, "start": start and start.id, "failing_since": failing}
                        self.answers[name] = after
                        # an answer, a failure, a new start, news in the wakes file: the object changes
                        if kept or before is None or any(before.get(k) != after[k]
                                                         for k in ("start", "orca", "guest_agent")):
                            changed = True
            if changed:
                self.wake.set()
            # a start whose time limit runs out before the next round is looked at right
            # after it: until a look after the limit fails, it stays `starting` (judge)
            limit, now = responding.wake_seconds(), responding.clock()
            due = [s.at + limit + 0.5 - now for n, s in starts.items() if n in running and s.at + limit > now]
            self.look.wait(min([AGENTS, *due]))
            self.look.clear()

    def keep(self, name, start, seen):
        """A look at the VM's start into its wakes file, with the incident's entry when the
        VM does not respond (responding.looked); True when the file changed. When the file
        cannot be written, watch judges from its last look alone and records no entry, since
        it could not tell that it recorded one already; it says why once."""
        try:
            kept, problem = responding.looked(name, start, seen,
                                              lambda: responding.record_incident(name, seen["guest_agent"], ACTOR)), None
        except Fail as f:
            kept, problem = False, f.message.strip()
        except Exception as e:
            kept, problem = False, f"{responding.wakes_file(name)}: {type(e).__name__}"
        if problem is not None and problem != self.unkept.get(name):
            warn(problem)
        self.unkept[name] = problem
        return kept


# --- the journal: followed ---------------------------------------------------------

def _follow(argv, handle, name):
    """Run `journalctl --follow` for ever, each entry to handle(); after an end, again
    after the last entry seen."""
    cursor, delay = None, RETRY
    while True:
        started = time.monotonic()
        args = [*argv, f"--after-cursor={cursor}" if cursor else "--lines=0"]
        try:
            p = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
        except OSError as e:
            warn(f"{name}: journalctl does not start ({e.strerror or e})")
            time.sleep(delay)
            delay = min(delay * 2, MAX_RETRY)
            continue
        with _lock:
            _children.add(p)
        try:
            for raw in p.stdout:
                try:
                    e = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(e, dict):
                    continue
                c = field(e, "__CURSOR", limit=512)
                if c:
                    cursor = c
                try:
                    handle(e)
                except Exception as ex:          # one odd entry must not end the follower
                    warn(f"{name}: entry skipped ({type(ex).__name__})")
        finally:
            code = p.wait()
            with _lock:
                _children.discard(p)
        # ending quickly again and again (no namespace, ...): try less often
        delay = RETRY if time.monotonic() - started > 60 else min(delay * 2, MAX_RETRY)
        warn(f"{name}: journalctl ends (exit {code}), next attempt in {delay:g} s")
        time.sleep(delay)


def on_namespace(e):
    """Activity, Alerts and GitHub tokens from the namespace agentbox, each only from
    the writer known for its identifier; the others are counted (activity.trusted)."""
    ident = field(e, "SYSLOG_IDENTIFIER") or ""
    if not (ident == activity.IDENTIFIER or ident.startswith(("agentbox-proxy-", "agentbox-gh-"))):
        return
    if not activity.trusted(e):
        activity.count_untrusted()
        return
    if ident == activity.IDENTIFIER:
        entry = activity.recorded_entry(e)
        if entry:
            emit("activity", _public(entry))
    elif ident.startswith("agentbox-proxy-"):
        entry = activity.alert_entry(e)
        if entry:
            emit("alert", {"time": entry["time"], "vm": entry["vm"], "domain": entry["_domain"],
                           "signals": entry["_signals"]})
            emit("activity", _public(entry))
    elif ident.startswith("agentbox-gh-"):
        entry = activity.github_entry(e)
        if entry:
            emit("github-token", {"time": entry["time"], "vm": entry["vm"],
                                  "action": GITHUB_ACTIONS[entry["action"]], "outcome": entry["outcome"],
                                  "repository": entry["_repository"], "request": entry["_request"],
                                  "reason": entry["_reason"], "reason_code": entry["_code"],
                                  "reason_params": entry["_code_params"], "expires": entry["_expires"]})
            emit("activity", _public(entry))


class BuildLines:
    """Lines of the agentbox-build@ units, gathered per unit and sent every BATCH seconds."""

    def __init__(self, poller):
        self.poller = poller
        self.pending = {}            # unit -> [entries]
        self.dropped = {}            # unit -> lines not sent
        self.lock = threading.Lock()

    def add(self, e):
        unit = field(e, "_SYSTEMD_UNIT") or ""
        if not BUILD_UNIT.fullmatch(unit) or field(e, "SYSLOG_IDENTIFIER") == activity.IDENTIFIER:
            return
        line = journal_entry(e)
        line = {k: line[k] for k in ("time", "priority", "message", "cursor")}
        with self.lock:
            lines = self.pending.setdefault(unit, [])
            lines.append(line)
            if len(lines) > MAX_BATCH:
                del lines[0]
                self.dropped[unit] = self.dropped.get(unit, 0) + 1

    def loop(self):
        while True:
            time.sleep(BATCH)
            with self.lock:
                pending, dropped = self.pending, self.dropped
                self.pending, self.dropped = {}, {}
            for unit, lines in sorted(pending.items()):
                image, build = self.poller.build_of(unit) or (None, None)
                if image is None:
                    # started after the last poll
                    self.poller.poll_builds()
                    image, build = self.poller.build_of(unit) or (None, None)
                emit("build-log", {"unit": unit, "target": BUILD_UNIT.fullmatch(unit).group(1), "image": image,
                                   "build": build, "entries": lines, "cursor": lines[-1]["cursor"],
                                   "truncated": unit in dropped})


# --- Host reboot -------------------------------------------------------------------

def reboot_needed():
    """{"needed": True/False/None (unknown), "packages": [...]} from dnf needs-restarting.
    Without any repository: it only compares installed packages with the boot, and never
    touches the network. (-C failed on titan: root has no metadata cache, and dnf's error
    exit 1 looked like "reboot needed".) Exit 1 counts only with dnf's reboot sentence."""
    try:
        r = subprocess.run(["dnf", "-q", "--disablerepo=*", "needs-restarting"], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, errors="replace", timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return {"needed": None, "packages": []}
    if r.returncode == 1 and "reboot is required" not in r.stdout.lower():
        return {"needed": None, "packages": []}
    needed = {0: False, 1: True}.get(r.returncode)
    packages = [l.strip()[2:].strip() for l in r.stdout.splitlines() if l.strip().startswith("* ")]
    return {"needed": needed, "packages": [p for p in packages if p.isprintable()][:50]}


def reboot_loop():
    last = None
    while True:
        found = reboot_needed()
        if found != last:
            emit("host-reboot", {**found, "checked": times.iso(time.time())})
            last = found
        time.sleep(REBOOT)


# --- the command -------------------------------------------------------------------

def _stdin_closes():
    """When stdin is a pipe or socket (the backend), its end is the end of watch."""
    try:
        mode = os.fstat(0).st_mode
    except OSError:
        return
    if not (stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)):
        return

    def wait():
        try:
            while os.read(0, 4096):
                pass
        except OSError:
            pass
        _end(0)
    threading.Thread(target=wait, name="stdin", daemon=True).start()


def _thread(target, name, *args):
    threading.Thread(target=target, args=args, name=name, daemon=True).start()


def watch(args):
    """agentbox watch: events as JSON lines on stdout until stdin closes or SIGTERM."""
    if args:
        die("watch (without arguments)", error="invalid_argument")
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda signum, frame: _end(0))
    _stdin_closes()
    poller = Poller()
    lines = BuildLines(poller)
    first = True
    agents_started = False
    problem = None
    while True:
        try:
            poller.poll()
            problem = None
        except Exception as e:
            # the same problem every few seconds is said once
            now = e.message.strip() if isinstance(e, Fail) else f"query failed ({type(e).__name__}: {e})"
            if now != problem:
                warn(now)
            problem = now
        untrusted = activity.untrusted_warning()          # once per poll, never what they said
        if untrusted:
            warn(untrusted)
        if first:
            first = False
            _thread(_follow, "activity",
                    ["journalctl", f"--namespace={activity.NAMESPACE}", "--follow", "--output=json", "--no-pager"],
                    on_namespace, "Activity")
            _thread(_follow, "builds",
                    ["journalctl", "--follow", "--output=json", "--no-pager", "--unit=agentbox-build@*.service"],
                    lines.add, "Builds")
            _thread(lines.loop, "build-lines")
            _thread(reboot_loop, "reboot")
        if not agents_started and poller.vms is not None:
            agents_started = True
            _thread(poller.agents_loop, "agents")
        poller.wake.wait(POLL)
        poller.wake.clear()


COMMANDS = {"watch": watch}
