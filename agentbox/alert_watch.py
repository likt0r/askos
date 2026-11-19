"""`agentbox alert-watch`: pause a VM whose own proxy raised an Alert with a strong Signal (#11,
ADR 0022).

Runs as root in agentbox-alert-watch.service (rpm/units, enabled by `agentbox setup`) for as
long as the Host runs. The proxies never reach root for it: it reads what they log. It follows
their ALERT lines in the journald namespace of Activity (journalctl --follow, entries of the
proxies' user only, ALERT lines only), and pauses a VM for an Alert

  - that the VM's own proxy wrote: journald's own fields name agentbox-proxy-<vm>.service and
    the user agentbox-proxy for the identifier agentbox-proxy-<vm> (activity.trusted), and the
    line names the same VM. Anything else is counted and left out, so no proxy, and nobody else
    who may write into the namespace, can pause another VM;
  - that is at most MAX_AGE old, so a watcher that was stopped does not pause a VM for what
    happened long before;
  - that has at least one strong Signal (list_records.STRONG); a weak one never pauses;
  - while alert_action of the Settings is `pause` (the default). A Settings file agentbox
    refuses counts as `pause`, the stricter of the two.

The Pause waits for the VM's lifecycle lock (a Reset, Restart, start or delete that holds it
finishes first) and happens only when the VM then runs: one that is off, paused already or no
longer registered stays as it is, and nothing is recorded for it. It is an ordinary Pause
(managedsave): `agentbox start`, a connection to its Orca or SSH port, a Restart or a Reset ends
it like any other. A Pause made is recorded in Activity (kind alert, action alert.paused, actor
alert-watch, with the host and the Signals), and so is one that failed; the VM is marked as
paused by an Alert (footprint.alert_pause_file) until the Pause ends (responding.clear), which
describe, watch and the Control Room show.

It does nothing else. Of the line it takes the VM's name, which journald's fields vouch for and
which has to name a registered VM, and the host and the Signals, which go only into the entry
and the mark, as text. Each VM's Pause waits in a thread of its own, so a held lock delays no
other VM; Alerts for a VM whose Pause is pending are left to that Pause.

Where it left off: the time of the last Alert line it read (STATE_FILE). A watcher that starts
(an update of the RPM restarts it) reads from then on, within MAX_AGE; one that never ran starts
at the end of the journal. While it runs, a journalctl that ends is followed again after the
last entry it saw.
"""
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time

from . import activity, footprint, paths, programs, proxy_log, registry, responding, settings, state_files, states
from . import times, virsh
from .commands import declare
from .errors import Fail, die
from .list_records import STRONG
from .registry import dom
from .sanitize import clean, field, redact

ACTOR = "alert-watch"
PROXY = "agentbox-proxy-"            # the identifier of a VM's proxy, before the VM's name
MAX_AGE = 600                        # seconds: an older Alert pauses nothing
STATE_FILE = os.path.join(paths.STATE, "alert-watch.json")
RETRY = paths.tunable("AGENTBOX_ALERT_WATCH_RETRY_MS", 5000) / 1000
MAX_RETRY = 300
SIGNAL = re.compile(r"[a-z][a-z0-9-]{0,31}")
MAX_SIGNALS = 16
MAX_HOST = 253

_lock = threading.Lock()             # stderr
_child = None                        # the follower's journalctl


def warn(text):
    with _lock:
        try:
            print(f"agentbox alert-watch: {text}", file=sys.stderr, flush=True)
        except (OSError, ValueError):
            pass


def strong(signals):
    """The strong Signals among those of an Alert, in its order; none for weak ones only."""
    return [s for s in signals if s in STRONG]


def alert(e, now=None):
    """(VM, host, Signals) of a journal entry that is an Alert with a strong Signal, written by
    that VM's own proxy, at most MAX_AGE old; else None. Entries no known writer wrote are
    counted (activity.trusted)."""
    ident = field(e, "SYSLOG_IDENTIFIER") or ""
    if not ident.startswith(PROXY):
        return None
    if not activity.trusted(e):
        activity.count_untrusted()
        return None
    vm = ident[len(PROXY):]
    event = proxy_log.parse(field(e, "MESSAGE", limit=None) or "")
    # the line's own VM is the proxy's: a line naming another one is no Alert of this VM
    if not (event and event.kind == "ALERT" and event.vm == vm):
        return None
    usec = times.journal_usec(e)
    if usec is None or (time.time() if now is None else now) - usec / 1e6 > MAX_AGE:
        return None
    signals = [s for s in event.signals if SIGNAL.fullmatch(s)][:MAX_SIGNALS]
    if not strong(signals):
        return None
    return vm, redact(clean(event.host, False))[:MAX_HOST], signals


def wanted():
    """Whether alert_action asks for a Pause. A Settings file agentbox refuses: yes, the
    stricter choice, said every time."""
    try:
        return settings.load()["alert_action"] == settings.ALERT_PAUSE
    except Fail as f:
        warn(f"{f.message.strip()}; the VM is paused as with alert_action={settings.ALERT_PAUSE}")
        return True


# --- the mark of a Pause by an Alert --------------------------------------------------

def paused_by(name):
    """The Alert that paused the VM, while that Pause lasts: {"time", "domain", "signals"},
    or None (none, or a mark that is no such object)."""
    try:
        with open(footprint.alert_pause_file(name), encoding="utf-8") as f:
            mark = json.load(f)
    except (OSError, ValueError):
        return None
    if not (isinstance(mark, dict) and isinstance(mark.get("time"), (int, float)) and not isinstance(mark["time"], bool)
            and isinstance(mark.get("domain"), str) and isinstance(mark.get("signals"), list)):
        return None
    return {"time": times.iso(mark["time"]), "domain": redact(clean(mark["domain"], False))[:MAX_HOST],
            "signals": [s for s in mark["signals"] if isinstance(s, str) and SIGNAL.fullmatch(s)][:MAX_SIGNALS]}


def _mark(name, host, signals):
    state_files.write_json(footprint.alert_pause_file(name), {"time": int(time.time()), "domain": host,
                                                              "signals": signals})


# --- the Pause ---------------------------------------------------------------------------

def pause(name, host, signals):
    """Pause the VM for this Alert, under its lifecycle lock, waited for; only when it runs
    then. Returns "paused", "failed" or why it stayed as it is ("not-registered", "not-running")."""
    try:
        registry.load(name)
    except Fail:
        return "not-registered"
    text = f"{host} [{','.join(signals)}]"
    params = {"domain": host, "signals": ", ".join(signals)}
    with state_files.vm_lock(name):
        if not states.running(name):
            return "not-running"
        r = virsh.run("managedsave", dom(name))
        if r.returncode == 0:
            responding.clear(name)          # its failed wakes, and the mark of an earlier Pause
            try:
                _mark(name, host, signals)
            except Fail as f:               # paused all the same: the mark only shows why
                warn(f.message.strip())
    if r.returncode != 0:
        why = programs.summary(r)
        warn(f"{name}: the Pause for an Alert ({text}) failed: {why}")
        activity.record("alert", activity.ALERT_PAUSED, f"Pause for an Alert failed: {text}: {why}", name,
                        outcome="failed", actor=ACTOR, params=params, error="action_failed")
        return "failed"
    print(f"{name} paused: Alert {text}", flush=True)
    activity.record("alert", activity.ALERT_PAUSED, f"Paused for an Alert: {text}", name, actor=ACTOR, params=params)
    return "paused"


class Pauses:
    """One Pause at a time per VM, each in a thread of its own."""

    def __init__(self):
        self.lock = threading.Lock()
        self.pending = set()

    def add(self, name, host, signals):
        """Start the VM's Pause; False when one is pending already, which this Alert leaves to."""
        with self.lock:
            if name in self.pending:
                return False
            self.pending.add(name)
        threading.Thread(target=self.run, args=(name, host, signals), name=f"pause-{name}", daemon=True).start()
        return True

    def run(self, name, host, signals):
        try:
            pause(name, host, signals)
        except Exception as e:              # one VM's failure must not end the watcher
            warn(f"{name}: the Pause for an Alert failed ({type(e).__name__}: {e})")
        finally:
            with self.lock:
                self.pending.discard(name)


# --- the journal -------------------------------------------------------------------------

def _last():
    """The time (microseconds) of the last Alert line an earlier run read, or None."""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            last = json.load(f).get("last")
    except (OSError, ValueError, AttributeError):
        return None
    return last if isinstance(last, int) and not isinstance(last, bool) and last > 0 else None


def _keep(usec):
    try:
        state_files.write_json(STATE_FILE, {"last": usec})
    except Fail as f:
        warn(f.message.strip())


def handle(e, pauses, now=None):
    found = alert(e, now)
    if found and wanted():
        pauses.add(*found)


def follow(pauses):
    """Follow the proxies' Alert lines for ever (journalctl --follow); after an end, again
    after the last entry seen."""
    global _child
    cursor, last, delay = None, _last(), RETRY
    while True:
        started = time.monotonic()
        users = activity.proxy_user_matches()
        if not users:
            warn(f"no user {activity.PROXY_USER}, no proxy logs, next attempt in {delay:g} s")
            time.sleep(delay)
            delay = min(delay * 2, MAX_RETRY)
            continue
        if cursor:
            where = [f"--after-cursor={cursor}"]
        elif last:                           # an earlier run read up to then, within MAX_AGE
            where = [f"--since=@{max(int(last // 10**6), int(time.time()) - MAX_AGE)}"]
        else:
            where = ["--lines=0"]
        argv = ["journalctl", f"--namespace={activity.NAMESPACE}", "--follow", "--output=json", "--no-pager",
                f"--grep={proxy_log.grep('ALERT')}", *users, *where]
        try:
            p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
        except OSError as e:
            warn(f"journalctl does not start ({e.strerror or e}), next attempt in {delay:g} s")
            time.sleep(delay)
            delay = min(delay * 2, MAX_RETRY)
            continue
        _child = p
        try:
            for raw in p.stdout:
                try:
                    e = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(e, dict):
                    continue
                usec = times.journal_usec(e)
                if not cursor and last and usec is not None and usec <= last:
                    continue                 # read by the run before
                c = field(e, "__CURSOR", limit=512)
                if c:
                    cursor = c
                try:
                    handle(e, pauses)
                except Exception as ex:      # one odd entry must not end the watcher
                    warn(f"entry skipped ({type(ex).__name__})")
                if usec is not None and (last is None or usec > last):
                    last = usec
                    _keep(usec)
                untrusted = activity.untrusted_warning()
                if untrusted:
                    warn(untrusted)
        finally:
            code = p.wait()
            _child = None
        delay = RETRY if time.monotonic() - started > 60 else min(delay * 2, MAX_RETRY)
        warn(f"journalctl ends (exit {code}), next attempt in {delay:g} s")
        time.sleep(delay)


def _end(signum, frame):
    child = _child                   # no lock: the handler may interrupt a holder of _lock
    if child is not None:
        try:
            child.terminate()
        except OSError:
            pass
    os._exit(0)


def alert_watch(args):
    """agentbox alert-watch (agentbox-alert-watch.service): pause a VM whose proxy raised an
    Alert with a strong Signal, until SIGTERM."""
    if args:
        die("alert-watch (without arguments)", error="invalid_argument")
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _end)
    follow(Pauses())


DECLARATIONS = {
    "alert-watch": declare(alert_watch, "agentbox alert-watch", options={}, own_entries=True),
}
