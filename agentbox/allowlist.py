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
the pattern matches, but the proxy checks every connection: never one with strong Signals,
and nothing while the Watchlist cannot be read, so --force does not apply to it. Adding one
shows which hosts of the scope's proxy log of the last 7 days it would allow, and needs
--confirm-pattern. Scope, expiry, notes, uses and closing tunnels work as for a domain.

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

Strong Signals. allow, extend and make-permanent refuse destinations the
classifier calls strongly suspicious unless given --force; the Control Room
never passes --force. While the hand-maintained Watchlist cannot be read they
refuse every destination without --force: which Signals are strong is unknown
then, and the proxy, which keeps Open web closed for that reason, lets
Allowlist entries through.
"""
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter

from . import activity, entry_store, list_records, options, paths, patterns, registry, settings, state_files, times
from .errors import Fail, die
from .network import CLIENT_REASONS, DOMAIN, done, valid_domain
from .sanitize import clean, field

ALL = list_records.ALL
# Durations of a Temporary allowlist entry (--for), in seconds
DURATIONS = {"15min": 15 * 60, "1h": 3600, "8h": 8 * 3600, "7d": 7 * 86400}
# --temporary without --for; Settings (#22) will make this configurable
MAX_NOTE = list_records.MAX_TEXT
USES_WINDOW = 86400                  # uses are counted over the last 24 hours
# what the proxy logs per connection an entry allowed (see proxy/agentbox-proxy): a domain,
# or "regex <pattern>" (a domain is never the single label regex)
ALLOWED = re.compile(r"\[[a-z0-9-]+\] ALLOWED \S+ \(allowlist (regex )?(\S+) (\S+)\)")
# besides domain or regex (patterns.py)
FIELDS = list_records.ALLOWLIST_FIELDS
# the preview of a regex entry: the hosts of the proxy log of these days that it matches,
# from at most so many of the newest lines, at most so many of them shown
PREVIEW_DAYS = 7
PREVIEW_LINES = paths.tunable("AGENTBOX_PREVIEW_LINES", 200_000)
PREVIEW_SHOWN = 100
# proxy lines with a host; a BLOCKED line for a client reason names the VM's address instead
LOGGED = re.compile(r"\[[a-z0-9-]+\] (ALLOWED|BLOCKED) ([a-z0-9.-]+) \((.*)\)")


def store_file():
    return os.path.join(paths.CONF, list_records.ALLOWLIST)


def default_duration():
    """The duration --temporary means: temporary_entry_default of the Settings."""
    return settings.load()["temporary_entry_default"]


# --- arguments -----------------------------------------------------------------

def _parse(args, known):
    """(positional arguments, options), for the Allowlist and the lists (exit code 1)."""
    return options.parse(args, known, exit=1)


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
        die("--force does not apply to a regex entry: it never allows a destination with strong Signals",
            error="invalid_argument", option="--force")


def _seconds(opts):
    """The duration asked for, in seconds, or None for permanent."""
    if "for" in opts and "temporary" in opts:
        die("--for and --temporary not together", error="invalid_duration")
    if "temporary" in opts:
        return DURATIONS[default_duration()]
    if "for" in opts:
        if opts["for"] not in DURATIONS:
            die("--for must be 15min, 1h, 8h or 7d", error="invalid_duration",
                value=clean(str(opts["for"]), False)[:20])
        return DURATIONS[opts["for"]]
    return None


def _note(value):
    return entry_store.text(value, "note", MAX_NOTE)


def classify(domain):
    """Signals of a destination from agentbox-suspicious as (text, suspicious, why the
    hand-maintained Watchlist cannot be read or None). The classifier prints the Signals
    and exits non-zero on strong suspicion, 3 with the reason on stderr while the
    Watchlist cannot be read; without a classifier, or when it does not answer in time,
    every domain counts as suspicious."""
    try:
        r = subprocess.run([paths.SUSPICIOUS, domain], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           env=paths.classifier_env(), timeout=paths.CLASSIFIER_SECONDS)
    except OSError:
        return "", True, None
    except subprocess.TimeoutExpired:
        return "agentbox-suspicious does not answer", True, None
    unreadable = clean(r.stderr.decode("utf-8", "replace").strip(), False)[-300:] if r.returncode == 3 else None
    return r.stdout.decode("utf-8", "replace").rstrip("\n"), r.returncode != 0, unreadable


def _signals(domain, opts):
    """Strongly suspicious destinations only with --force, and while the Watchlist cannot
    be read every destination."""
    from .lists import UNREADABLE
    tags, suspicious, unreadable = classify(domain)
    if "force" in opts:
        return
    if unreadable is not None:
        die(f"{UNREADABLE}: {unreadable or 'reason unknown'}; whether {domain} is strongly suspicious cannot be "
            f"checked. Allowing it only with --force", error="watchlist_unreadable", domain=domain)
    if suspicious:
        die(f"{domain} is suspicious ({tags}). Allowing it only with --force", error="domain_suspicious",
            domain=domain)


def scope_text(scope):
    return "all VMs" if scope == ALL else scope


def _until(t):
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
    path = os.path.join(paths.CONF, list_records.LEGACY_ALLOWLIST[1].format(name))
    for p in (path, path + ".migrated"):
        state_files.remove(p)


def start_vm(name, source=None):
    """A new VM starts without entries of its own, or with a one-time copy of the
    current entries of `source` (ADR 0001); nothing of an earlier VM of this
    name carries over. Returns the number of entries copied."""
    now = int(time.time())
    with changing() as entries:
        entries[:] = [e for e in entries if e["scope"] != name]
        copies = [_new(patterns.key(e), name, e["expires"], e["note"], now, activity.actor())
                  for e in entries if e["scope"] == source and (e["expires"] is None or e["expires"] > now)]
        entries.extend(copies)
    _remove_legacy(name)
    return len(copies)


def drop_vm(name):
    """The VM's own entries go with it; whether there were any."""
    with changing() as entries:
        before = len(entries)
        entries[:] = [e for e in entries if e["scope"] != name]
        gone = len(entries) != before
    legacy = os.path.join(paths.CONF, list_records.LEGACY_ALLOWLIST[1].format(name))
    gone = gone or os.path.exists(legacy)
    _remove_legacy(name)
    return gone


# --- uses -------------------------------------------------------------------------

def uses(now):
    """Connections each entry allowed in the last 24 hours, {(("domain", domain) or ("regex",
    pattern), scope): n}, from the proxy logs of every VM; None when the journal cannot be read."""
    idents = [f"agentbox-proxy-{n}" for n in registry.names()]
    try:
        log = activity._journal((now - USES_WINDOW, None), idents, (activity.PROXY_USER,),
                                grep=r"^\[[a-z0-9-]+\] ALLOWED ")
    except Fail as f:
        print(f"agentbox: uses not readable ({f.message})", file=sys.stderr)
        return None
    counts = Counter()
    for e in log:
        m = ALLOWED.fullmatch(field(e, "MESSAGE", limit=1024) or "")
        if m:
            counts[(("regex" if m.group(1) else "domain", m.group(2)), m.group(3))] += 1
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
        log, unreadable = activity._journal((now - PREVIEW_DAYS * 86400, None), [f"agentbox-proxy-{n}" for n in names],
                                            (activity.PROXY_USER,), grep=r"^\[[a-z0-9-]+\] (ALLOWED|BLOCKED) ",
                                            cap=PREVIEW_LINES), None
    except Fail as f:
        log, unreadable = [], f.message
    compiled = list_records.compile_pattern(pattern)
    counts = {}
    for e in log:
        m = LOGGED.fullmatch(field(e, "MESSAGE", limit=1024) or "")
        if m and m.group(3) in CLIENT_REASONS:
            continue
        if m and compiled is not None and list_records.pattern_matches(compiled, m.group(2)):
            c = counts.setdefault(m.group(2), {"allowed": 0, "blocked": 0})
            c["allowed" if m.group(1) == "ALLOWED" else "blocked"] += 1
    hosts = sorted(counts)
    shown = hosts[:PREVIEW_SHOWN]
    from .detection import _classifier
    rep = _classifier("--signals", *shown)
    if not (isinstance(rep, dict) and isinstance(rep.get("hosts"), dict)
            and all(isinstance(rep["hosts"].get(h), dict) and isinstance(rep["hosts"][h].get("signals"), list)
                    and isinstance(rep["hosts"][h].get("strong"), bool) for h in shown)
            and isinstance(rep.get("watchlist_unreadable"), (str, type(None)))):
        die("agentbox-suspicious: invalid answer", error="action_failed")
    rows = [{"host": h, **counts[h], "signals": [str(s) for s in rep["hosts"][h]["signals"]],
             "strong": rep["hosts"][h]["strong"]} for h in shown]
    truncated = len(log) >= PREVIEW_LINES
    stamps = [u // 10**6 for u in map(times.journal_usec, log) if u is not None and u >= 0] if truncated else []
    return {"hosts": rows, "more": len(hosts) - len(shown),
            "watchlist_unreadable": clean(rep["watchlist_unreadable"], False)[:300]
            if rep["watchlist_unreadable"] else None, "truncated": truncated,
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
    positional, opts = _parse(args, {"json": False})
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
        left = "permanent" if r["expires"] is None else "expired" if r["expired"] else "until " + _until(r["expires"])
        used = "?" if r["uses_24h"] is None else f"{r['uses_24h']}x"
        what = r["domain"] or f"regex {clean(r['regex'], False)}"
        note = f"[{r['refused']}; it allows nothing] {r['note']}" if r["refused"] else r["note"]
        print(f"{what:<40} {scope_text(r['scope']):<16} {left:<18} {used:>6}  {note}".rstrip())


def allow(args):
    """agentbox allow <vm|all> <domain> [--for 15min|1h|8h|7d | --temporary] [--note TEXT]
    [--force] [--json]: a new entry, or an existing one widened but never
    shortened (a permanent one stays permanent); strongly suspicious
    destinations only with --force. With --regex PATTERN in place of the domain a Regex
    entry: refused unless it keeps the rules (patterns.py), and without --confirm-pattern it
    only shows which hosts of the proxy log it would allow (preview)."""
    positional, opts = _parse(args, {"for": True, "temporary": False, "note": True, "force": False, "regex": True,
                                     "confirm-pattern": False, "json": False})
    scope, ref = _target(positional, opts)
    seconds = _seconds(opts)
    note = _note(opts.get("note"))
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
    until = f" until {_until(e['expires'])}" if e["expires"] is not None else ""
    done("json" in opts, f"{scope_text(scope)}: {entry_store.label(ref)} allowed{until}", **patterns.both(e))


def disallow(args):
    """agentbox disallow <vm|all> <domain>|--regex PATTERN [--json]: removes the entry; the
    proxy closes tunnels to it that nothing else allows"""
    positional, opts = _parse(args, {"regex": True, "json": False})
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
    [--json]: a temporary entry runs that much longer (from now, if it has expired)"""
    positional, opts = _parse(args, {"for": True, "force": False, "regex": True, "json": False})
    scope, ref = _target(positional, opts)
    if "for" not in opts:
        die("extend needs --for 15min, 1h, 8h or 7d", error="invalid_duration")
    seconds = _seconds(opts)
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
    done("json" in opts, f"{scope_text(scope)}: {entry_store.label(ref)} allowed until {_until(e['expires'])}",
         **patterns.both(e))


def make_permanent(args):
    """agentbox make-permanent <vm|all> <domain>|--regex PATTERN [--force] [--json]: a
    temporary entry no longer expires"""
    positional, opts = _parse(args, {"force": False, "regex": True, "json": False})
    scope, ref = _target(positional, opts)
    _no_force(ref, opts)
    if _existing(read()[0], ref, scope)["expires"] is not None and ref[0] == "domain":
        _signals(ref[1], opts)
    with changing() as entries:
        e = _existing(entries, ref, scope)
        was = e["expires"]
        e["expires"] = None
    if was is None:
        activity.unchanged()
    done("json" in opts, f"{scope_text(scope)}: {entry_store.label(ref)} allowed permanently", **patterns.both(e))


def set_note(args):
    """agentbox set-note <vm|all> <domain>|--regex PATTERN --note TEXT [--json]: the
    entry's note (empty to clear it)"""
    positional, opts = _parse(args, {"note": True, "regex": True, "json": False})
    scope, ref = _target(positional, opts)
    if "note" not in opts:
        die("set-note needs --note TEXT", error="invalid_argument", option="--note")
    note = _note(opts["note"])
    with changing() as entries:
        e = _existing(entries, ref, scope)
        e["note"] = note
    done("json" in opts, f"{scope_text(scope)}: note on {entry_store.label(ref)} saved", **patterns.both(e))


def expire(args):
    """agentbox expire-allowlist [--json] (timer, every minute): removes expired
    entries and records each expiry in Activity"""
    positional, opts = _parse(args, {"json": False})
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
    if "json" in opts:
        print(json.dumps({"expired": [patterns.both(e) for e in _order(gone)]}, ensure_ascii=False))
        return
    for e in _order(gone):
        print(f"{scope_text(e['scope'])}: {patterns.shown(e)} expired")


def check_regex(args):
    """agentbox check-regex <allowlist|watchlist|legitimate|harmless> --regex PATTERN
    [<host>...] [--scope vm|all] [--json]: whether the pattern can be an entry of that list
    (else the command fails with the reason, as adding it would), which of the hosts it
    matches, and for the Allowlist the preview of what it would allow for the scope (all VMs
    unless --scope)"""
    positional, opts = _parse(args, {"regex": True, "scope": True, "json": False})
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
    positional, opts = _parse(args, {"json": False})
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


COMMANDS = {"allowlist": show, "allow": allow, "disallow": disallow, "extend": extend,
            "make-permanent": make_permanent, "set-note": set_note, "expire-allowlist": expire,
            "migrate-allowlist": migrate, "check-regex": check_regex}
