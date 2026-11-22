"""Forbids: destinations one VM is refused at once, for a while or for good (GLOSSARY: Forbid,
ADR 0024).

Storage. One root-owned file, <CONF>/forbids.json, that the proxy reads directly and re-reads
whenever it changes, in the Allowlist's record format:

    {"entries": [{"domain": "telemetry.example", "scope": "work", "expires": 1793703600,
                  "note": "", "added": 1793700000, "added_by": "bewr"}]}

A Forbid covers its domain and every subdomain, as a domain Allowlist entry does. Its scope is
always one VM, and it names a domain, never a pattern. expires works as for a Temporary allowlist
entry (the same durations; --temporary means temporary_entry_default), null for a permanent one;
there is at most one Forbid per domain and VM.

The proxy refuses a destination a Forbid of its VM wins for at once, BLOCKED <host> (forbidden):
no hold, so no notification, also in Open web, and closes a tunnel to it when a Forbid comes to
win for it. An expired Forbid refuses nothing: the destination is unknown again, held and
notified. `agentbox expire-allowlist` (the timer, every minute) removes expired Forbids
with the expired Allowlist entries and records each in Activity. Strong Signals block whatever a
Forbid says (ADR 0021).

Allowlist and Forbids (ADR 0024). Of the entries and Forbids that match a destination for a VM
the more specific one wins (list_records.specificity): any named domain above any pattern, a longer
domain above its parent, at the same domain one VM's above all VMs'. So a Forbid of
gist.github.com for work beats an entry github.com, an entry api.gist.github.com beats that
Forbid, and a Forbid of x.example for work beats an entry x.example for all VMs. The same domain
for the same VM is never both allowed and forbidden: the newer decision takes the other's place.
- forbid removes the VM's own Allowlist entry of the same domain (the proxy closes its tunnels as
  for disallow), recorded in Activity; entries for all VMs, of other domains and patterns stay;
- allow for one VM, and extend and make-permanent of such an entry (the last two can bring an
  expired one back), remove that VM's Forbid of the same domain (lift), recorded in Activity. An
  entry for all VMs, or a pattern, lifts nothing: a VM's Forbid of the domain is more specific and
  keeps winning, as Forbids of subdomains below any entry do.
forbid holds the Allowlist's lock while it writes forbids.json under the Forbids' lock, and lift
runs after the Allowlist was written, so neither waits on the other the other way round.
"""
import json
import os
import sys
import time
from collections import Counter

from . import activity, allowlist, entry_store, list_records, options, paths, proxy_log, registry, times
from .commands import declare
from .errors import Fail, die
from .network import done, valid_domain
from .sanitize import clean

ALL = list_records.ALL
USES_WINDOW = allowlist.USES_WINDOW


def store_file():
    return os.path.join(paths.CONF, list_records.FORBIDS)


# --- arguments -----------------------------------------------------------------

def _target(positional):
    """(VM, domain) of `<vm> <domain>`: a Forbid is for one VM, never all, and names a domain."""
    vm = positional[0] if positional else ""
    if vm == ALL:
        die("a forbid is for one VM, not all", error="invalid_argument")
    vm = registry.check(vm)
    domain = valid_domain(positional[1] if len(positional) > 1 else "")
    if len(positional) > 2:
        die("too many arguments: " + clean(positional[2], False)[:60], error="invalid_argument")
    return vm, domain


# --- storage ---------------------------------------------------------------------

def read():
    """(entries, no legacy files): forbids.json, none while it does not exist."""
    path = store_file()
    try:
        with open(path, encoding="utf-8") as f:
            return list_records.forbid_entries(f.read()), []
    except FileNotFoundError:
        return [], []
    except OSError as e:
        die(f"{path}: {e.strerror}", error="action_failed")
    except ValueError as e:        # the record's checks and texts, as the proxy has them
        die(f"{path}: {'not valid JSON' if isinstance(e, UnicodeDecodeError) else e}", error="action_failed")


def _order(entries):
    return sorted(entries, key=lambda e: (e["scope"], e["domain"]))


def changing():
    """Read-modify-write of the Forbids under the record's lock (entry_store.changing)."""
    return entry_store.changing(store_file(), read, lambda entries, _: entry_store.write(store_file(), entries, _order),
                                _order)


def find(entries, vm, domain):
    return entry_store.find(entries, ("domain", domain), scope=vm)


def _existing(entries, vm, domain):
    e = find(entries, vm, domain)
    if e is None:
        die(f"{vm}: {domain} is not forbidden", error="forbid_not_found", vm=vm, domain=domain)
    return e


def in_force(e, now):
    return e["expires"] is None or e["expires"] > now


# --- the Allowlist's side -----------------------------------------------------------

def lift(e):
    """After the Allowlist entry e was made or brought back in force: the Forbid of its domain for
    its VM goes (the same domain for the same VM is never both), recorded in Activity. An entry for
    all VMs or a pattern lifts nothing. Returns what went ([] when forbids.json cannot be read: the
    entry is in place, the message says what stayed)."""
    if e["scope"] == ALL or "regex" in e:
        return []
    try:
        with changing() as entries:
            gone = [f for f in entries if f["scope"] == e["scope"] and f["domain"] == e["domain"]]
            entries[:] = [f for f in entries if f not in gone]
    except Fail as f:
        print(f"agentbox: Forbids the entry covers stay: {f.message}", file=sys.stderr)
        return []
    shown = allowlist.scope_text(e["scope"])
    for f in _order(gone):
        activity.record("allowlist", "allowlist.forbid-lifted",
                        f"Forbid {f['domain']} for {f['scope']} lifted: an Allowlist entry for {shown} takes its "
                        "place", f["scope"],
                        params={"domain": f["domain"], "scope": f["scope"]})
    return [{"domain": f["domain"], "scope": f["scope"]} for f in _order(gone)]


def lifted_text(gone):
    """The end of an Allowlist command's message for the Forbids lift took away."""
    if not gone:
        return ""
    return "; Forbid lifted: " + ", ".join(f"{f['domain']} ({f['scope']})" for f in gone)


# --- for create and delete (allowlist.start_vm and drop_vm) -----------------------------

def drop_vm(name):
    """The VM's Forbids go (with it, or those an earlier VM of the name left); whether there were any."""
    with changing() as entries:
        before = len(entries)
        entries[:] = [e for e in entries if e["scope"] != name]
        return len(entries) != before


# --- expiry (agentbox expire-allowlist) ------------------------------------------------

def expire(now):
    """Removes the expired Forbids and records each expiry in Activity; returns them."""
    with changing() as entries:
        gone = [e for e in entries if not in_force(e, now)]
        entries[:] = [e for e in entries if e not in gone]
    for e in _order(gone):
        activity.record("allowlist", "allowlist.forbid-expired",
                        f"Temporary Forbid {e['domain']} for {e['scope']} expired "
                        f"({times.local(e['expires'])})", e["scope"],
                        params={"domain": e["domain"], "scope": e["scope"]}, expired=e["expires"])
    return _order(gone)


# --- refusals -----------------------------------------------------------------------

def refusals(now, vms):
    """Connections the proxies of these VMs refused for a Forbid in the last 24 hours,
    {(vm, host): n}; None when the journal cannot be read."""
    try:
        log = activity.proxy_lines((now - USES_WINDOW, None), vms, ("BLOCKED",))
    except Fail as f:
        print(f"agentbox: refusals not readable ({f.message})", file=sys.stderr)
        return None
    counts = Counter()
    for _, event in log:
        if event and event.reason == proxy_log.FORBIDDEN:
            counts[(event.vm, event.host)] += 1
    return counts


# --- commands ---------------------------------------------------------------------

def show(args):
    """agentbox forbids [<vm>] [--json]: every Forbid, or a VM's; with remaining time and the
    connections each refused in the last 24 hours (refused_24h, null when the journal cannot be
    read)."""
    positional, opts = options.parse(args)
    if len(positional) > 1:
        die("too many arguments: " + clean(positional[1], False)[:60], error="invalid_argument")
    vm = registry.check(positional[0]) if positional else None
    entries, _ = read()
    if vm:
        entries = [e for e in entries if e["scope"] == vm]
    now = int(time.time())
    counts = refusals(now, sorted({e["scope"] for e in entries})) if entries else Counter()
    rows = []
    for e in _order(entries):
        refused = None if counts is None else sum(
            n for (v, host), n in counts.items() if v == e["scope"] and list_records.covers(e["domain"], host))
        rows.append({**e, "expired": not in_force(e, now),
                     "remaining": None if e["expires"] is None else max(0, e["expires"] - now),
                     "refused_24h": refused})
    if "json" in opts:
        print(json.dumps(rows, ensure_ascii=False))
        return
    for r in rows:
        left = ("permanent" if r["expires"] is None else "expired" if r["expired"]
                else "until " + allowlist.until_text(r["expires"]))
        refused = "?" if r["refused_24h"] is None else f"{r['refused_24h']}x"
        print(f"{r['domain']:<40} {r['scope']:<16} {left:<18} {refused:>6}  {r['note']}".rstrip())


def forbid(args):
    """agentbox forbid <vm> <domain> [--for 15min|1h|8h|7d | --temporary] [--note TEXT] [--json]:
    a new Forbid, or an existing one widened but never shortened (a permanent one stays
    permanent); it takes the place of the VM's own Allowlist entry of the domain. A more specific
    entry (of a subdomain) still wins over it."""
    positional, opts = options.parse(args)
    vm, domain = _target(positional)
    seconds = allowlist.duration_seconds(opts)
    note = allowlist.note_arg(opts.get("note"))
    now = int(time.time())
    expires = None if seconds is None else now + seconds
    with allowlist.changing() as allows:
        own = allowlist.find(allows, ("domain", domain), vm)
        with changing() as entries:
            e = find(entries, vm, domain)
            if e is None:
                e = {"domain": domain, "scope": vm, "expires": expires, "note": note or "", "added": now,
                     "added_by": activity.actor()}
                entries.append(e)
            else:
                if e["expires"] is not None:
                    e["expires"] = None if expires is None else max(e["expires"], expires)
                if note is not None:
                    e["note"] = note
        if own is not None:
            allows.remove(own)
    if own is not None:
        activity.record("allowlist", "allowlist.entry-forbidden",
                        f"Allowlist entry {domain} removed: a Forbid for {vm} takes its place", vm,
                        params={"domain": domain, "scope": vm})
    until = f" until {allowlist.until_text(e['expires'])}" if e["expires"] is not None else ""
    replaced = "; its Allowlist entry removed" if own is not None else ""
    done("json" in opts, f"{vm}: {domain} forbidden{until}{replaced}", **e, allowlist_removed=own is not None)


def unforbid(args):
    """agentbox unforbid <vm> <domain> [--json]: removes the Forbid; the destination is unknown
    again (held and notified)."""
    positional, opts = options.parse(args)
    vm, domain = _target(positional)
    with changing() as entries:
        e = find(entries, vm, domain)
        if e is not None:
            entries.remove(e)
    if e is None:
        activity.unchanged()
        done("json" in opts, f"{vm}: {domain} is not forbidden", scope=vm, domain=domain, removed=False)
        return
    done("json" in opts, f"{vm}: Forbid {domain} removed", scope=vm, domain=domain, removed=True)


def extend(args):
    """agentbox forbid-extend <vm> <domain> --for 15min|1h|8h|7d [--json]: a temporary Forbid
    runs that much longer (from now, if it has expired)."""
    positional, opts = options.parse(args)
    vm, domain = _target(positional)
    if "for" not in opts:
        die("forbid-extend needs --for 15min, 1h, 8h or 7d", error="invalid_duration")
    seconds = allowlist.duration_seconds(opts)
    with changing() as entries:
        e = _existing(entries, vm, domain)
        if e["expires"] is None:
            die(f"{vm}: {domain} is forbidden permanently", error="forbid_permanent", vm=vm, domain=domain)
        e["expires"] = max(e["expires"], int(time.time())) + seconds
    done("json" in opts, f"{vm}: {domain} forbidden until {allowlist.until_text(e['expires'])}", **e)


def make_permanent(args):
    """agentbox forbid-make-permanent <vm> <domain> [--json]: a temporary Forbid no longer
    expires."""
    positional, opts = options.parse(args)
    vm, domain = _target(positional)
    with changing() as entries:
        e = _existing(entries, vm, domain)
        was = e["expires"]
        e["expires"] = None
    if was is None:
        activity.unchanged()
    done("json" in opts, f"{vm}: {domain} forbidden permanently", **e)


def set_note(args):
    """agentbox forbid-set-note <vm> <domain> --note TEXT [--json]: the Forbid's note (empty to
    clear it)."""
    positional, opts = options.parse(args)
    vm, domain = _target(positional)
    if "note" not in opts:
        die("forbid-set-note needs --note TEXT", error="invalid_argument", option="--note")
    note = allowlist.note_arg(opts["note"])
    with changing() as entries:
        e = _existing(entries, vm, domain)
        e["note"] = note
    done("json" in opts, f"{vm}: note on the Forbid {domain} saved", **e)


DECLARATIONS = {
    "forbids": declare(show, "agentbox forbids [<vm>] [--json]", options={"json": False}, read_only=True,
                       control_room=True, option_exit=1),
    "forbid": declare(forbid, "agentbox forbid <vm> <domain> [--for D|--temporary] [--note TEXT] [--json]",
                      options={"for": True, "temporary": False, "note": True, "json": False},
                      option_exit=1, control_room=True, kind="allowlist",
                      describe=activity.listed("Forbid {}", "for", temporary=True)),
    "unforbid": declare(unforbid, "agentbox unforbid <vm> <domain> [--json]", options={"json": False},
                        option_exit=1, control_room=True, kind="allowlist",
                        describe=activity.listed("Remove the Forbid {}")),
    "forbid-extend": declare(extend, "agentbox forbid-extend <vm> <domain> --for D [--json]",
                             options={"for": True, "json": False}, option_exit=1, control_room=True,
                             kind="allowlist", describe=activity.listed("Extend the Forbid {}", "by")),
    "forbid-make-permanent": declare(make_permanent, "agentbox forbid-make-permanent <vm> <domain> [--json]",
                                     options={"json": False}, option_exit=1, control_room=True, kind="allowlist",
                                     describe=activity.listed("Make the Forbid {} permanent")),
    "forbid-set-note": declare(set_note, "agentbox forbid-set-note <vm> <domain> --note TEXT [--json]",
                               options={"note": True, "json": False}, option_exit=1, control_room=True,
                               kind="allowlist", describe=activity.listed("Note on the Forbid {}")),
}
