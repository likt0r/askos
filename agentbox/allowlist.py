"""The Allowlist: which domains VMs may reach through the proxy.

Storage. One root-owned file, <CONF>/allowlist.json, that the proxy reads
directly and re-reads whenever it changes:

    {"entries": [{"domain": "docs.rs", "scope": "priv", "expires": 1793703600,
                  "note": "Rust docs", "added": 1793700000, "added_by": "bewr"}]}

An entry covers its domain and every subdomain. scope is "all" (every VM) or
the name of one VM; expires is the end of a Temporary allowlist entry (epoch
seconds), null for a permanent one; added and added_by say when and by whom
(the actor) it was made. There is at most one entry per domain and scope.

A Regex entry (ADR 0013, patterns.py) has a pattern, "regex": "^mirror[0-9]+\\.example\\.org$",
in place of the domain, and the commands take it with --regex PATTERN. It allows the hosts
the pattern matches; their Signals are not checked when it is added, so --force does not apply
to it. Adding one shows which hosts of the scope's proxy log of the last 7 days it would allow,
and needs --confirm-pattern. Scope, expiry, notes, uses and closing tunnels work as for a domain.

Expiry and tunnels (ADR 0005). The proxy ignores expired entries for new
connections; tunnels already open keep running. `agentbox expire-allowlist`
(a timer, every minute) removes expired entries and records each in Activity.
Removing an entry before it expired (disallow) makes the proxy close the
tunnels that no other entry, and no Open web, still allows.

Before allowlist.json, the Allowlist was <CONF>/allowlist (all VMs) and
<CONF>/allowlist-<vm>, one domain per line. `agentbox migrate-allowlist`
(agentbox setup) takes their entries over and moves the files aside
(<file>.migrated). Until then the proxy and the read-only commands use them,
and the first change through agentbox migrates them.

Strong Signals (ADR 0021). The proxy blocks a destination with strong Signals whatever
allows it, checking every connection, so allow, extend and make-permanent refuse such a
domain, also with --force (domain_suspicious): the Allowlist holds no entry that cannot work
when it is made. The message names how to lift the Signal instead. While which Signals are
strong cannot be told (the hand-maintained Watchlist cannot be read, or the classifier does
not answer) they refuse every domain without --force; with it they store the entry unchecked,
and the proxy, which lets nothing through while the Watchlist cannot be read, checks it once
it can. The Control Room never passes --force.

Forbids (forbids.py, ADR 0024). Of an entry and a Forbid the more specific one wins. The same
domain for the same VM is never both: allow, extend and make-permanent of an entry for one VM lift
that VM's Forbid of the domain (after the entry is written), and `agentbox expire-allowlist` removes expired Forbids too. A VM's Forbids come
and go with its entries (start_vm, drop_vm).
"""
import json
import os
import re
import sys
import time
from collections import Counter

from . import activity, classifier, durations, entry_store, footprint, list_records, options, paths, patterns, proxy_log, registry, settings, state_files, times
from .commands import declare
from .errors import Fail, die
from .network import DOMAIN, done, valid_domain
from .sanitize import clean

ALL = list_records.ALL
# --temporary without --for; Settings (#22) will make this configurable
MAX_NOTE = list_records.MAX_TEXT
USES_WINDOW = 86400                  # uses are counted over the last 24 hours
# besides domain or regex (patterns.py)
FIELDS = list_records.ALLOWLIST_FIELDS
# the preview of a regex entry: the hosts of the proxy log of these days that it matches,
# from at most so many of the newest lines, at most so many of them shown
PREVIEW_DAYS = 7
PREVIEW_LINES = paths.tunable("AGENTBOX_PREVIEW_LINES", 200_000)
PREVIEW_SHOWN = 100


def store_file():
    return os.path.join(paths.CONF, list_records.ALLOWLIST)


def default_duration():
    """The duration --temporary means: temporary_entry_default of the Settings."""
    return settings.load()["temporary_entry_default"]


# --- arguments -----------------------------------------------------------------

def scope_arg(s):
    """"all" or a VM of the registry."""
    return s if s == ALL else registry.check(s)


def _target(positional, opts):
    """Scope and the entry asked for (entry_store.target): the scope first, then the domain
    (any valid one) or --regex."""
    scope = scope_arg(positional[0] if positional else "")
    return scope, entry_store.target(positional[1:], opts, valid_domain)


def _no_force(ref, opts):
    if ref[0] == "regex" and "force" in opts:
        die("--force does not apply to a regex entry: the Signals of its hosts are not checked when it is added",
            error="invalid_argument", option="--force")


def duration_seconds(opts):
    """The duration asked for, in seconds, or None for permanent."""
    if "for" in opts and "temporary" in opts:
        die("--for and --temporary not together", error="invalid_duration")
    if "temporary" in opts:
        return durations.DURATIONS[default_duration()][0]
    if "for" in opts:
        if opts["for"] not in durations.DURATIONS:
            die("--for must be 15min, 1h, 8h or 7d", error="invalid_duration",
                value=clean(str(opts["for"]), False)[:20])
        return durations.DURATIONS[opts["for"]][0]
    return None


def note_arg(value):
    return entry_store.text(value, "note", MAX_NOTE)


def lift_hint(domain):
    """How a destination with strong Signals can be let through: only by lifting the Signal."""
    return (f"Lift the Signal on the Host instead: remove or lower its Watchlist entry (agentbox watch-remove "
            f"--force, agentbox watch-add --force), or make it a Legitimate domain (agentbox legitimate-add "
            f"--force); nothing lifts the blockchain pattern, a raw IP, a metadata address or a Feed hit below a "
            f"user-content host. agentbox check-domain {domain} shows where each Signal comes from")


def _signals(domain, opts):
    """No entry for a destination with strong Signals, also with --force: the proxy blocks it
    whatever allows it (ADR 0021). While which Signals are strong cannot be told, every
    destination only with --force, which stores it unchecked."""
    from .lists import UNREADABLE
    tags, strong, unknown = classifier.classify(domain)
    if strong:
        die(f"{domain} has strong Signals ({tags}), which block it whatever the Allowlist says, also with "
            f"--force. {lift_hint(domain)}", error="domain_suspicious", domain=domain)
    if unknown is None or "force" in opts:
        return
    code, why = unknown
    if code == "watchlist_unreadable":
        die(f"{UNREADABLE}: {why or 'reason unknown'}; whether {domain} has strong Signals cannot be checked. "
            f"Allowing it unchecked only with --force", error=code, domain=domain)
    die(f"Whether {domain} has strong Signals cannot be checked ({why}). Allowing it unchecked only with --force",
        error=code, domain=domain)


def scope_text(scope):
    return "all VMs" if scope == ALL else scope


def until_text(t):
    """End of a temporary entry for people: the time, with the day unless it is today."""
    today = time.strftime("%Y-%m-%d ", time.localtime())
    text = times.local(t)
    return text[len(today):] if text.startswith(today) else text


# --- storage ---------------------------------------------------------------------

def _clipped(text, limit):
    return clean(text, False)[:limit]


def legacy_files():
    """The Allowlist files from before allowlist.json, as (path, scope)."""
    out = []
    every, one = list_records.LEGACY_ALLOWLIST
    prefix = one.format("")
    for n in state_files.listdir(paths.CONF):
        vm = n[len(prefix):] if n.startswith(prefix) else None
        if n == every:
            out.append((os.path.join(paths.CONF, n), ALL))
        elif vm and registry.NAME.fullmatch(vm) and not registry.reserved(vm):
            out.append((os.path.join(paths.CONF, n), vm))
    return out


def _from_legacy(files, now):
    """(entries, lines that are no domain) of the legacy files, read as the proxy read them."""
    entries, skipped, seen = [], [], set()
    for path, scope in files:
        for domain in list_records.legacy_allowlist(state_files.read(path, b"").decode("utf-8", "replace")):
            if (domain, scope) in seen:
                continue
            if not DOMAIN.fullmatch(domain):
                skipped.append(clean(domain, False)[:60])
                continue
            seen.add((domain, scope))
            entries.append(entry(domain, scope, None, "", now, "migration"))
    return entries, skipped


def entry(domain, scope, expires, note, added, added_by):
    return {"domain": domain, "scope": scope, "expires": expires, "note": note, "added": added,
            "added_by": added_by}


def _new(ref, scope, expires, note, added, added_by):
    """A new entry for a domain or, ref ("regex", pattern), a Regex entry."""
    return {**entry_store.param(ref), "scope": scope, "expires": expires, "note": note, "added": added, "added_by": added_by}


def read():
    """(entries, legacy files they come from): allowlist.json, or while it does
    not exist the legacy files, which the proxy uses then as well."""
    path = store_file()
    try:
        with open(path, encoding="utf-8") as f:
            return list_records.allowlist_entries(f.read()), []
    except FileNotFoundError:
        files = legacy_files()
        return _from_legacy(files, int(time.time()))[0], files
    except OSError as e:
        die(f"{path}: {e.strerror}", error="action_failed")
    except ValueError as e:        # the record's checks and texts, as the proxy has them
        die(f"{path}: {'not valid JSON' if isinstance(e, UnicodeDecodeError) else e}", error="action_failed")


def _order(entries):
    """For all VMs first, then by VM; domains before Regex entries."""
    return sorted(entries, key=lambda e: (e["scope"] != ALL, e["scope"], "regex" in e, patterns.value(e)))


def _write(entries, legacy=()):
    """allowlist.json, then the legacy files ((path, scope)) moved aside."""
    entry_store.write(store_file(), entries, _order, [path for path, _ in legacy])


def changing():
    """Read-modify-write of the entries under the store's lock (entry_store.changing)."""
    return entry_store.changing(store_file(), read, _write, _order)


def find(entries, ref, scope):
    """The entry of ref, ("domain", domain) or ("regex", pattern), and scope, else None."""
    return entry_store.find(entries, ref, scope=scope)


def _existing(entries, ref, scope):
    e = find(entries, ref, scope)
    if e is None:
        die(f"{scope_text(scope)}: {entry_store.label(ref)} is not on the Allowlist", error="allowlist_entry_not_found",
            scope=scope, **entry_store.param(ref))
    return e


# --- for create and delete (vms.py) -------------------------------------------------

def _remove_legacy(name):
    """A legacy file of the VM, and its migrated copy, go as well."""
    for p in (footprint.legacy_allowlist_file(name), footprint.legacy_allowlist_migrated_file(name)):
        state_files.remove(p)


def start_vm(name, source=None):
    """A new VM starts without entries of its own, or with a one-time copy of the
    current entries of `source` (ADR 0001), and without Forbids; nothing of an earlier VM of
    this name carries over. Returns the number of entries copied."""
    from . import forbids
    forbids.drop_vm(name)
    now = int(time.time())
    with changing() as entries:
        entries[:] = [e for e in entries if e["scope"] != name]
        copies = [_new(patterns.key(e), name, e["expires"], e["note"], now, activity.actor())
                  for e in entries if e["scope"] == source and (e["expires"] is None or e["expires"] > now)]
        entries.extend(copies)
    _remove_legacy(name)
    return len(copies)


def drop_vm(name):
    """The VM's own entries and its Forbids go with it; whether there were any."""
    from . import forbids
    dropped = forbids.drop_vm(name)
    with changing() as entries:
        before = len(entries)
        entries[:] = [e for e in entries if e["scope"] != name]
        gone = len(entries) != before or dropped
    legacy = footprint.legacy_allowlist_file(name)
    gone = gone or os.path.exists(legacy)
    _remove_legacy(name)
    return gone


# --- uses -------------------------------------------------------------------------

def uses(now):
    """Connections each entry allowed in the last 24 hours, {(("domain", domain) or ("regex",
    pattern), scope): n}, from the proxy logs of every VM; None when the journal cannot be read."""
    try:
        log = activity.proxy_lines((now - USES_WINDOW, None), registry.names(), ("ALLOWED",))
    except Fail as f:
        print(f"agentbox: uses not readable ({f.message})", file=sys.stderr)
        return None
    counts = Counter()
    for _, event in log:
        if event and event.entry:
            kind, value, scope = event.entry
            counts[((kind, value), scope)] += 1
    return counts


def preview(scope, pattern):
    """What a Regex entry for scope would allow: the hosts of the proxy log of the last
    PREVIEW_DAYS days (ALLOWED and BLOCKED lines of the VM's proxy, every VM's for all)
    that the pattern matches, each with how often it was allowed and blocked, its Signals
    and whether one is strong (then it stays blocked), as
    {"hosts": [...], "more": hosts not shown, "watchlist_unreadable": why or None (then it
    allows nothing), "truncated": whether the log had more lines than were read (a VM can
    fill its log), "reaches_back": how far back what was read reaches (epoch seconds: the
    oldest line read when truncated, else the start of the PREVIEW_DAYS),
    "log_unreadable": why the proxy log could not be read, else None}."""
    now = int(time.time())
    names = registry.names() if scope == ALL else [scope]
    try:
        log, unreadable = activity.proxy_lines((now - PREVIEW_DAYS * 86400, None), names, ("ALLOWED", "BLOCKED"),
                                               cap=PREVIEW_LINES), None
    except Fail as f:
        log, unreadable = [], f.message
    compiled = list_records.compile_pattern(pattern)
    counts = {}
    for _, event in log:
        # a BLOCKED line for a client reason names the VM's address, not a host
        if not event or event.reason in proxy_log.CLIENT_REASONS:
            continue
        if compiled is not None and list_records.pattern_matches(compiled, event.host):
            c = counts.setdefault(event.host, {"allowed": 0, "blocked": 0})
            c[event.kind.lower()] += 1
    hosts = sorted(counts)
    shown = hosts[:PREVIEW_SHOWN]
    signals, watchlist_unreadable = classifier.signals(shown)
    rows = [{"host": h, **counts[h], "signals": signals[h]["signals"],
             "strong": signals[h]["strong"]} for h in shown]
    truncated = len(log) >= PREVIEW_LINES
    stamps = [u // 10**6 for u in (times.journal_usec(e) for e, _ in log) if u is not None and u >= 0] if truncated else []
    return {"hosts": rows, "more": len(hosts) - len(shown),
            "watchlist_unreadable": clean(watchlist_unreadable, False)[:300] if watchlist_unreadable else None,
            "truncated": truncated,
            "reaches_back": min(stamps) if stamps else now - PREVIEW_DAYS * 86400, "log_unreadable": unreadable}


def preview_text(scope, pattern, p):
    """The preview for people, one line per host."""
    if p["log_unreadable"]:
        return (f"The proxy log cannot be read ({p['log_unreadable']}), so which hosts pattern {pattern} would "
                f"allow for {scope_text(scope)} cannot be shown.")
    lines = [f"pattern {pattern} for {scope_text(scope)} matches "
             + (f"{len(p['hosts']) + p['more']} hosts" if p["hosts"] else "no host")
             + f" of the proxy log of the last {PREVIEW_DAYS} days" + (":" if p["hosts"] else "")]
    if p["truncated"]:
        lines.insert(0, f"WARNING: the proxy log of these {PREVIEW_DAYS} days has more lines than the newest "
                        f"{PREVIEW_LINES} read, which reach back only to {times.local(p['reaches_back'])}: hosts seen "
                        f"only before then are missing below.")
    for h in p["hosts"]:
        seen = f"allowed {h['allowed']}x, blocked {h['blocked']}x"
        stays = f"; stays blocked, strong Signals: {', '.join(h['signals'])}" if h["strong"] else ""
        lines.append(f"  {h['host']} ({seen}{stays})")
    if p["more"]:
        lines.append(f"  ... and {p['more']} more")
    if p["watchlist_unreadable"]:
        lines.append(f"While the Watchlist cannot be read ({p['watchlist_unreadable']}), it allows nothing.")
    return "\n".join(lines)


# --- commands ---------------------------------------------------------------------

def show(args):
    """agentbox allowlist [<vm>|all] [--json]: every entry, those that apply to a
    VM (its own and those for all VMs), or those for all VMs; with remaining time
    and uses in the last 24 hours. Each row has domain and regex, one of them null, and
    refused: why the rules refuse a stored pattern (it allows nothing until removed), else null."""
    positional, opts = options.parse(args)
    if len(positional) > 1:
        die("too many arguments: " + clean(positional[1], False)[:60], error="invalid_argument")
    scope = scope_arg(positional[0]) if positional else None
    entries, _ = read()
    if scope == ALL:
        entries = [e for e in entries if e["scope"] == ALL]
    elif scope:
        entries = [e for e in entries if e["scope"] in (ALL, scope)]
    now = int(time.time())
    counts = uses(now) if entries else Counter()
    rows = []
    for e in _order(entries):
        expired = e["expires"] is not None and e["expires"] <= now
        rows.append({**patterns.both(e), "expired": expired,
                     "remaining": None if e["expires"] is None else max(0, e["expires"] - now),
                     "uses_24h": None if counts is None else counts[(patterns.key(e), e["scope"])],
                     "refused": patterns.refused(e)})
    if "json" in opts:
        print(json.dumps(rows, ensure_ascii=False))
        return
    for r in rows:
        left = "permanent" if r["expires"] is None else "expired" if r["expired"] else "until " + until_text(r["expires"])
        used = "?" if r["uses_24h"] is None else f"{r['uses_24h']}x"
        what = r["domain"] or f"regex {clean(r['regex'], False)}"
        note = f"[{r['refused']}; it allows nothing] {r['note']}" if r["refused"] else r["note"]
        print(f"{what:<40} {scope_text(r['scope']):<16} {left:<18} {used:>6}  {note}".rstrip())


def allow(args):
    """agentbox allow <vm|all> <domain> [--for 15min|1h|8h|7d | --temporary] [--note TEXT]
    [--force] [--json]: a new entry, or an existing one widened but never
    shortened (a permanent one stays permanent); never one for a destination with strong
    Signals, and while they cannot be told only with --force (_signals). With --regex PATTERN
    in place of the domain a Regex entry: refused unless it keeps the rules (patterns.py), and
    without --confirm-pattern it only shows which hosts of the proxy log it would allow
    (preview). An entry for one VM lifts the VM's Forbid of the domain (forbids.lift)."""
    from . import forbids
    positional, opts = options.parse(args)
    scope, ref = _target(positional, opts)
    seconds = duration_seconds(opts)
    note = note_arg(opts.get("note"))
    if ref[0] == "regex":
        _no_force(ref, opts)
        patterns.checked(ref[1])
        if "confirm-pattern" not in opts:
            p = preview(scope, ref[1])
            die(f"{preview_text(scope, ref[1], p)}\nAllowing it only with --confirm-pattern",
                error="pattern_confirmation_required", scope=scope, regex=ref[1], hosts=len(p["hosts"]) + p["more"])
    else:
        if "confirm-pattern" in opts:
            die("--confirm-pattern only with --regex", error="invalid_argument", option="--confirm-pattern")
        _signals(ref[1], opts)
    now = int(time.time())
    expires = None if seconds is None else now + seconds
    with changing() as entries:
        e = find(entries, ref, scope)
        if e is None:
            e = _new(ref, scope, expires, note or "", now, activity.actor())
            entries.append(e)
        else:
            if e["expires"] is not None:
                e["expires"] = None if expires is None else max(e["expires"], expires)
            if note is not None:
                e["note"] = note
    lifted = forbids.lift(e)
    until = f" until {until_text(e['expires'])}" if e["expires"] is not None else ""
    done("json" in opts, f"{scope_text(scope)}: {entry_store.label(ref)} allowed{until}{forbids.lifted_text(lifted)}",
         **patterns.both(e), forbids_lifted=lifted)


def disallow(args):
    """agentbox disallow <vm|all> <domain>|--regex PATTERN [--json]: removes the entry; the
    proxy closes tunnels to it that nothing else allows"""
    positional, opts = options.parse(args)
    scope, ref = _target(positional, opts)
    with changing() as entries:
        e = find(entries, ref, scope)
        if e is not None:
            entries.remove(e)
    given = entry_store.given(ref)
    if e is None:
        activity.unchanged()
        hint = ""
        if scope != ALL and find(read()[0], ref, ALL):
            again = f"--regex {ref[1]}" if ref[0] == "regex" else ref[1]
            hint = f" (applies to all VMs: agentbox disallow all {again})"
        done("json" in opts, f"{scope_text(scope)}: {entry_store.label(ref)} is not on the Allowlist{hint}",
             scope=scope, **given, removed=False)
        return
    done("json" in opts, f"{scope_text(scope)}: {entry_store.label(ref)} removed", scope=scope, **given, removed=True)


def extend(args):
    """agentbox extend <vm|all> <domain>|--regex PATTERN --for 15min|1h|8h|7d [--force]
    [--json]: a temporary entry runs that much longer (from now, if it has expired); it lifts the
    Forbid of its domain, as allow does"""
    from . import forbids
    positional, opts = options.parse(args)
    scope, ref = _target(positional, opts)
    if "for" not in opts:
        die("extend needs --for 15min, 1h, 8h or 7d", error="invalid_duration")
    seconds = duration_seconds(opts)
    _no_force(ref, opts)
    if _existing(read()[0], ref, scope)["expires"] is None:
        die(f"{scope_text(scope)}: {entry_store.label(ref)} is allowed permanently", error="allowlist_entry_permanent",
            scope=scope, **entry_store.param(ref))
    if ref[0] == "domain":
        _signals(ref[1], opts)
    with changing() as entries:
        e = _existing(entries, ref, scope)
        if e["expires"] is None:
            die(f"{scope_text(scope)}: {entry_store.label(ref)} is allowed permanently", error="allowlist_entry_permanent",
                scope=scope, **entry_store.param(ref))
        e["expires"] = max(e["expires"], int(time.time())) + seconds
    lifted = forbids.lift(e)
    done("json" in opts, f"{scope_text(scope)}: {entry_store.label(ref)} allowed until {until_text(e['expires'])}"
         f"{forbids.lifted_text(lifted)}", **patterns.both(e), forbids_lifted=lifted)


def make_permanent(args):
    """agentbox make-permanent <vm|all> <domain>|--regex PATTERN [--force] [--json]: a
    temporary entry no longer expires; it lifts the Forbid of its domain, as allow does"""
    from . import forbids
    positional, opts = options.parse(args)
    scope, ref = _target(positional, opts)
    _no_force(ref, opts)
    if _existing(read()[0], ref, scope)["expires"] is not None and ref[0] == "domain":
        _signals(ref[1], opts)
    with changing() as entries:
        e = _existing(entries, ref, scope)
        was = e["expires"]
        e["expires"] = None
    lifted = forbids.lift(e)
    if was is None and not lifted:
        activity.unchanged()
    done("json" in opts, f"{scope_text(scope)}: {entry_store.label(ref)} allowed permanently"
         f"{forbids.lifted_text(lifted)}", **patterns.both(e), forbids_lifted=lifted)


def set_note(args):
    """agentbox set-note <vm|all> <domain>|--regex PATTERN --note TEXT [--json]: the
    entry's note (empty to clear it)"""
    positional, opts = options.parse(args)
    scope, ref = _target(positional, opts)
    if "note" not in opts:
        die("set-note needs --note TEXT", error="invalid_argument", option="--note")
    note = note_arg(opts["note"])
    with changing() as entries:
        e = _existing(entries, ref, scope)
        e["note"] = note
    done("json" in opts, f"{scope_text(scope)}: note on {entry_store.label(ref)} saved", **patterns.both(e))


def expire(args):
    """agentbox expire-allowlist [--json] (timer, every minute): removes expired
    entries and expired Forbids and records each expiry in Activity"""
    from . import forbids
    positional, opts = options.parse(args)
    if positional:
        die("too many arguments: " + clean(positional[0], False)[:60], error="invalid_argument")
    now = int(time.time())
    with changing() as entries:
        gone = [e for e in entries if e["expires"] is not None and e["expires"] <= now]
        entries[:] = [e for e in entries if e not in gone]
    for e in _order(gone):
        everywhere = e["scope"] == ALL
        kind, value = patterns.key(e)
        activity.record("allowlist", "allowlist.expired",
                        f"Temporary Allowlist entry {patterns.shown(e)}{' for all VMs' if everywhere else ''} "
                        f"expired ({times.local(e['expires'])})", None if everywhere else e["scope"],
                        params={kind: value, "scope": e["scope"]}, expired=e["expires"])
    forbids_gone = forbids.expire(now)
    if "json" in opts:
        print(json.dumps({"expired": [patterns.both(e) for e in _order(gone)], "forbids_expired": forbids_gone},
                         ensure_ascii=False))
        return
    for e in _order(gone):
        print(f"{scope_text(e['scope'])}: {patterns.shown(e)} expired")
    for e in forbids_gone:
        print(f"{e['scope']}: Forbid {e['domain']} expired")


def check_regex(args):
    """agentbox check-regex <allowlist|watchlist|legitimate|harmless> --regex PATTERN
    [<host>...] [--scope vm|all] [--json]: whether the pattern can be an entry of that list
    (else the command fails with the reason, as adding it would), which of the hosts it
    matches, and for the Allowlist the preview of what it would allow for the scope (all VMs
    unless --scope)"""
    positional, opts = options.parse(args)
    lists = ("allowlist", "watchlist", "legitimate", "harmless")
    if not positional or positional[0] not in lists:
        die(f"check-regex needs the list: {', '.join(lists)}", error="invalid_argument")
    which, hosts = positional[0], positional[1:]
    if "regex" not in opts:
        die("check-regex needs --regex PATTERN", error="invalid_argument", option="--regex")
    if "scope" in opts and which != "allowlist":
        die("--scope only for the allowlist", error="invalid_argument", option="--scope")
    scope = scope_arg(opts.get("scope", ALL))
    pattern = patterns.checked(opts["regex"], broad=which != "watchlist")
    for h in hosts:
        if not DOMAIN.fullmatch(h):
            die(f"invalid domain: {clean(h, False)[:60]}", error="invalid_domain", domain=clean(h, False)[:60])
    compiled = re.compile(pattern)
    result = {"list": which, "regex": pattern, "scope": scope if which == "allowlist" else None,
              "matches": {h: list_records.pattern_matches(compiled, h) for h in hosts},
              "preview": preview(scope, pattern) if which == "allowlist" else None}
    if "json" in opts:
        print(json.dumps(result, ensure_ascii=False))
        return
    print(f"pattern {pattern}: can be an entry of the {which}")
    for h, yes in result["matches"].items():
        print(f"  {h}: {'matches' if yes else 'does not match'}")
    if result["preview"] is not None:
        print(preview_text(scope, pattern, result["preview"]))


def migrate(args):
    """agentbox migrate-allowlist [--json]: entries of the Allowlist files from
    before allowlist.json taken over, every one of them; the files are moved
    aside (<file>.migrated). Running it again changes nothing."""
    positional, opts = options.parse(args)
    if positional:
        die("too many arguments: " + clean(positional[0], False)[:60], error="invalid_argument")
    path = store_file()
    with state_files.locked(path):
        files = legacy_files()
        if os.path.exists(path):
            entries, _ = read()
        else:
            entries = []
        found, skipped = _from_legacy(files, int(time.time()))
        added = [e for e in found if not find(entries, patterns.key(e), e["scope"])]
        if files or not os.path.exists(path):
            _write(entries + added, files)
    if not files:
        activity.unchanged()
        message = "Allowlist: nothing to take over"
    else:
        message = (f"Allowlist: {len(added)} entries taken over from "
                   f"{', '.join(os.path.basename(p) for p, _ in files)}")
        if skipped:
            message += f"; not a domain, skipped: {', '.join(skipped)}"
    done("json" in opts, message, migrated=len(added), files=[os.path.basename(p) for p, _ in files],
         skipped=skipped)


DECLARATIONS = {
    "allowlist": declare(show, "agentbox allowlist [<vm>|all] [--json]", options={"json": False},
                         read_only=True, control_room=True, option_exit=1),
    "allow": declare(allow, "agentbox allow <vm|all> <domain>|--regex PATTERN [--confirm-pattern] [--for D|--temporary] [--note TEXT] [--force] [--json]",
                     options={"regex": True, "confirm-pattern": False, "for": True, "temporary": False,
                              "note": True, "force": False, "json": False}, option_exit=1,
                     control_room=True, kind="allowlist",
                     describe=activity.listed("Allow {}", "for", temporary=True, force=True)),
    "disallow": declare(disallow, "agentbox disallow <vm|all> <domain> [--regex PATTERN] [--json]",
                        options={"regex": True, "json": False}, option_exit=1, control_room=True,
                        kind="allowlist", describe=activity.listed("Remove the Allowlist entry {}")),
    "extend": declare(extend, "agentbox extend <vm|all> <domain> --for D [--regex PATTERN] [--force] [--json]",
                      options={"for": True, "regex": True, "force": False, "json": False}, option_exit=1,
                      control_room=True, kind="allowlist",
                      describe=activity.listed("Extend the Allowlist entry {}", "by", force=True)),
    "make-permanent": declare(make_permanent, "agentbox make-permanent <vm|all> <domain> [--regex PATTERN] [--force] [--json]",
                              options={"regex": True, "force": False, "json": False}, option_exit=1,
                              control_room=True, kind="allowlist",
                              describe=activity.listed("Make the Allowlist entry {} permanent", force=True)),
    "set-note": declare(set_note, "agentbox set-note <vm|all> <domain> --note TEXT [--regex PATTERN] [--json]",
                        options={"note": True, "regex": True, "json": False}, option_exit=1, control_room=True,
                        kind="allowlist", describe=activity.listed("Note on the Allowlist entry {}")),
    "expire-allowlist": declare(expire, "agentbox expire-allowlist [--json]", options={"json": False},
                                 own_entries=True, kind="allowlist", option_exit=1),
    "migrate-allowlist": declare(migrate, "agentbox migrate-allowlist [--json]", options={"json": False},
                                   kind="allowlist", describe=lambda a: activity.said("Take over the old Allowlist files"),
                                  option_exit=1),
    "check-regex": declare(check_regex, "agentbox check-regex <list> --regex PATTERN [<host>...] [--scope vm|all] [--json]",
                           options={"regex": True, "scope": True, "json": False}, read_only=True,
                           control_room=True, option_exit=1),
}
