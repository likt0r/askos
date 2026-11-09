"""The Allowlist: which domains VMs may reach through the proxy.

Storage. One root-owned file, <CONF>/allowlist.json, that the proxy reads
directly and re-reads whenever it changes:

    {"entries": [{"domain": "docs.rs", "scope": "priv", "expires": 1793703600,
                  "note": "Rust docs", "added": 1793700000, "added_by": "bewr"}]}

An entry covers its domain and every subdomain. scope is "all" (every VM) or
the name of one VM; expires is the end of a Temporary allowlist entry (epoch
seconds), null for a permanent one; added and added_by say when and by whom
(the actor) it was made. There is at most one entry per domain and scope.

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
import contextlib
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter

from . import activity, paths, registry, settings, state_files
from .errors import Fail, die
from .network import DOMAIN, done, valid_domain, when
from .sanitize import clean, field

ALL = "all"
# Durations of a Temporary allowlist entry (--for), in seconds
DURATIONS = {"15min": 15 * 60, "1h": 3600, "8h": 8 * 3600, "7d": 7 * 86400}
# --temporary without --for; Settings (#22) will make this configurable
MAX_NOTE = 200
USES_WINDOW = 86400                  # uses are counted over the last 24 hours
# what the proxy logs per connection an entry allowed (see proxy/agentbox-proxy)
ALLOWED = re.compile(r"\[[a-z0-9-]+\] ALLOWED \S+ \(allowlist (\S+) (\S+)\)")
FIELDS = {"domain": str, "scope": str, "expires": (int, type(None)), "note": str, "added": int, "added_by": str}


def store_file():
    return os.path.join(paths.CONF, "allowlist.json")


def default_duration():
    """The duration --temporary means: temporary_entry_default of the Settings."""
    return settings.load()["temporary_entry_default"]


# --- arguments -----------------------------------------------------------------

def _parse(args, known):
    """(positional arguments, options): --name value or --name=value for options
    that take a value (known[name] True), --name for flags."""
    positional, opts, i = [], {}, 0
    while i < len(args):
        a = args[i]
        i += 1
        if not a.startswith("--"):
            positional.append(a)
            continue
        name, eq, value = a[2:].partition("=")
        if name not in known:
            die("unknown option: " + clean(a, False)[:60], error="invalid_argument",
                option="--" + clean(name, False)[:60])
        if not known[name]:
            if eq:
                die(f"--{name} takes no value", error="invalid_argument", option=f"--{name}")
            opts[name] = True
            continue
        if not eq:
            if i >= len(args):
                die(f"--{name} needs a value", error="invalid_argument", option=f"--{name}")
            value, i = args[i], i + 1
        if name in opts:
            die(f"--{name} given twice", error="invalid_argument", option=f"--{name}")
        opts[name] = value
    return positional, opts


def scope_arg(s):
    """"all" or a VM of the registry."""
    return s if s == ALL else registry.check(s)


def _target(positional, more=0):
    """Scope and domain from the first two positional arguments."""
    scope = scope_arg(positional[0] if positional else "")
    domain = valid_domain(positional[1] if len(positional) > 1 else "")
    if len(positional) > 2 + more:
        die("too many arguments: " + clean(positional[2 + more], False)[:60], error="invalid_argument")
    return scope, domain


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
    if value is None:
        return None
    note = value.strip()
    if len(note) > MAX_NOTE or not note.isprintable():
        die(f"--note: at most {MAX_NOTE} printable characters on one line", error="invalid_note", option="--note")
    return note


def classify(domain):
    """Signals of a destination from agentbox-suspicious as (text, suspicious, why the
    hand-maintained Watchlist cannot be read or None). The classifier prints the Signals
    and exits non-zero on strong suspicion, 3 with the reason on stderr while the
    Watchlist cannot be read; without a classifier every domain counts as suspicious."""
    try:
        r = subprocess.run([paths.SUSPICIOUS, domain], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           env=paths.classifier_env())
    except OSError:
        return "", True, None
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
    text = when(t)
    return text[len(today):] if text.startswith(today) else text


# --- storage ---------------------------------------------------------------------

def _problem(e):
    if not isinstance(e, dict):
        return "not a JSON object"
    for key, kind in FIELDS.items():
        if key not in e or isinstance(e[key], bool) or not isinstance(e[key], kind):
            return f"field {key} missing or of the wrong type"
    if not DOMAIN.fullmatch(e["domain"]):
        return f"invalid domain {clean(e['domain'], False)[:60]}"
    if e["scope"] != ALL and (not registry.NAME.fullmatch(e["scope"]) or registry.reserved(e["scope"])):
        return f"invalid scope {clean(e['scope'], False)[:20]}"
    if len(e["note"]) > MAX_NOTE:
        return "note too long"
    return None


def _valid(data, path):
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        die(f"{path}: broken (no list entries)", error="action_failed")
    seen = set()
    for e in data["entries"]:
        why = _problem(e)
        if why:
            die(f"{path}: broken entry ({why})", error="action_failed")
        key = (e["domain"], e["scope"])
        if key in seen:
            die(f"{path}: {e['domain']} ({e['scope']}) twice", error="action_failed")
        seen.add(key)
    return data["entries"]


def legacy_files():
    """The Allowlist files from before allowlist.json, as (path, scope)."""
    try:
        names = sorted(os.listdir(paths.CONF))
    except FileNotFoundError:
        return []
    except OSError as e:
        die(f"{paths.CONF}: {e.strerror}", error="action_failed")
    out = []
    for n in names:
        vm = n[len("allowlist-"):] if n.startswith("allowlist-") else None
        if n == "allowlist":
            out.append((os.path.join(paths.CONF, n), ALL))
        elif vm and registry.NAME.fullmatch(vm) and not registry.reserved(vm):
            out.append((os.path.join(paths.CONF, n), vm))
    return out


def _from_legacy(files, now):
    """(entries, lines that are no domain) of the legacy files, read as the proxy read them."""
    entries, skipped, seen = [], [], set()
    for path, scope in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except FileNotFoundError:
            continue
        except OSError as e:
            die(f"{path}: {e.strerror}", error="action_failed")
        for line in lines:
            domain = line.split("#", 1)[0].strip().lower().rstrip(".")
            if not domain or (domain, scope) in seen:
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


def read():
    """(entries, legacy files they come from): allowlist.json, or while it does
    not exist the legacy files, which the proxy uses then as well."""
    path = store_file()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        files = legacy_files()
        return _from_legacy(files, int(time.time()))[0], files
    except OSError as e:
        die(f"{path}: {e.strerror}", error="action_failed")
    except ValueError:
        die(f"{path}: not valid JSON", error="action_failed")
    return _valid(data, path), []


def _order(entries):
    return sorted(entries, key=lambda e: (e["scope"] != ALL, e["scope"], e["domain"]))


def _write(entries, legacy=()):
    """allowlist.json (readable by the proxy's user), then the legacy files moved aside."""
    data = json.dumps({"entries": _order(entries)}, indent=2, ensure_ascii=False) + "\n"
    state_files.write_atomic(store_file(), data.encode(), 0o644)
    for path, _ in legacy:
        try:
            os.replace(path, path + ".migrated")
        except FileNotFoundError:
            pass
        except OSError as e:
            die(f"{path}: {e.strerror}", error="action_failed")


@contextlib.contextmanager
def changing():
    """Read-modify-write of the entries under the store's lock. The list may be
    changed in place; it is written when it changed or still came from the
    legacy files (the first change migrates them)."""
    with state_files.locked(store_file()):
        entries, legacy = read()
        before = json.dumps(_order(entries), sort_keys=True)
        yield entries
        if legacy or json.dumps(_order(entries), sort_keys=True) != before:
            _write(entries, legacy)


def find(entries, domain, scope):
    return next((e for e in entries if e["domain"] == domain and e["scope"] == scope), None)


def _existing(entries, domain, scope):
    e = find(entries, domain, scope)
    if e is None:
        die(f"{scope_text(scope)}: {domain} is not on the Allowlist", error="allowlist_entry_not_found",
            scope=scope, domain=domain)
    return e


# --- for create and delete (vms.py) -------------------------------------------------

def _remove_legacy(name):
    """A legacy file of the VM, and its migrated copy, go as well."""
    path = os.path.join(paths.CONF, f"allowlist-{name}")
    for p in (path, path + ".migrated"):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
        except OSError as e:
            die(f"{p}: {e.strerror}", error="action_failed")


def start_vm(name, source=None):
    """A new VM starts without entries of its own, or with a one-time copy of the
    current entries of `source` (ADR 0001); nothing of an earlier VM of this
    name carries over. Returns the number of entries copied."""
    now = int(time.time())
    with changing() as entries:
        entries[:] = [e for e in entries if e["scope"] != name]
        copies = [entry(e["domain"], name, e["expires"], e["note"], now, activity.actor())
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
    legacy = os.path.join(paths.CONF, f"allowlist-{name}")
    gone = gone or os.path.exists(legacy)
    _remove_legacy(name)
    return gone


# --- uses -------------------------------------------------------------------------

def uses(now):
    """Connections each entry allowed in the last 24 hours, {(domain, scope): n},
    from the proxy logs of every VM; None when the journal cannot be read."""
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
            counts[(m.group(1), m.group(2))] += 1
    return counts


# --- commands ---------------------------------------------------------------------

def show(args):
    """agentbox allowlist [<vm>|all] [--json]: every entry, those that apply to a
    VM (its own and those for all VMs), or those for all VMs; with remaining time
    and uses in the last 24 hours"""
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
        rows.append({**e, "expired": expired,
                     "remaining": None if e["expires"] is None else max(0, e["expires"] - now),
                     "uses_24h": None if counts is None else counts[(e["domain"], e["scope"])]})
    if "json" in opts:
        print(json.dumps(rows, ensure_ascii=False))
        return
    for r in rows:
        left = "permanent" if r["expires"] is None else "expired" if r["expired"] else "until " + _until(r["expires"])
        used = "?" if r["uses_24h"] is None else f"{r['uses_24h']}x"
        print(f"{r['domain']:<40} {scope_text(r['scope']):<16} {left:<18} {used:>6}  {r['note']}".rstrip())


def allow(args):
    """agentbox allow <vm|all> <domain> [--for 15min|1h|8h|7d | --temporary] [--note TEXT]
    [--force] [--json]: a new entry, or an existing one widened but never
    shortened (a permanent one stays permanent); strongly suspicious
    destinations only with --force"""
    positional, opts = _parse(args, {"for": True, "temporary": False, "note": True, "force": False,
                                     "json": False})
    scope, domain = _target(positional)
    seconds = _seconds(opts)
    note = _note(opts.get("note"))
    _signals(domain, opts)
    now = int(time.time())
    expires = None if seconds is None else now + seconds
    with changing() as entries:
        e = find(entries, domain, scope)
        if e is None:
            e = entry(domain, scope, expires, note or "", now, activity.actor())
            entries.append(e)
        else:
            if e["expires"] is not None:
                e["expires"] = None if expires is None else max(e["expires"], expires)
            if note is not None:
                e["note"] = note
    until = f" until {_until(e['expires'])}" if e["expires"] is not None else ""
    done("json" in opts, f"{scope_text(scope)}: {domain} allowed{until}", **e)


def disallow(args):
    """agentbox disallow <vm|all> <domain> [--json]: removes the entry; the proxy
    closes tunnels to it that nothing else allows"""
    positional, opts = _parse(args, {"json": False})
    scope, domain = _target(positional)
    with changing() as entries:
        e = find(entries, domain, scope)
        if e is not None:
            entries.remove(e)
    if e is None:
        activity.unchanged()
        hint = ""
        if scope != ALL and find(read()[0], domain, ALL):
            hint = f" (applies to all VMs: agentbox disallow all {domain})"
        done("json" in opts, f"{scope_text(scope)}: {domain} is not on the Allowlist{hint}",
             scope=scope, domain=domain, removed=False)
        return
    done("json" in opts, f"{scope_text(scope)}: {domain} removed", scope=scope, domain=domain, removed=True)


def extend(args):
    """agentbox extend <vm|all> <domain> --for 15min|1h|8h|7d [--force] [--json]: a
    temporary entry runs that much longer (from now, if it has expired)"""
    positional, opts = _parse(args, {"for": True, "force": False, "json": False})
    scope, domain = _target(positional)
    if "for" not in opts:
        die("extend needs --for 15min, 1h, 8h or 7d", error="invalid_duration")
    seconds = _seconds(opts)
    if _existing(read()[0], domain, scope)["expires"] is None:
        die(f"{scope_text(scope)}: {domain} is allowed permanently", error="allowlist_entry_permanent",
            scope=scope, domain=domain)
    _signals(domain, opts)
    with changing() as entries:
        e = _existing(entries, domain, scope)
        if e["expires"] is None:
            die(f"{scope_text(scope)}: {domain} is allowed permanently", error="allowlist_entry_permanent",
                scope=scope, domain=domain)
        e["expires"] = max(e["expires"], int(time.time())) + seconds
    done("json" in opts, f"{scope_text(scope)}: {domain} allowed until {_until(e['expires'])}", **e)


def make_permanent(args):
    """agentbox make-permanent <vm|all> <domain> [--force] [--json]: a temporary
    entry no longer expires"""
    positional, opts = _parse(args, {"force": False, "json": False})
    scope, domain = _target(positional)
    if _existing(read()[0], domain, scope)["expires"] is not None:
        _signals(domain, opts)
    with changing() as entries:
        e = _existing(entries, domain, scope)
        was = e["expires"]
        e["expires"] = None
    if was is None:
        activity.unchanged()
    done("json" in opts, f"{scope_text(scope)}: {domain} allowed permanently", **e)


def set_note(args):
    """agentbox set-note <vm|all> <domain> --note TEXT [--json]: the entry's note
    (empty to clear it)"""
    positional, opts = _parse(args, {"note": True, "json": False})
    scope, domain = _target(positional)
    if "note" not in opts:
        die("set-note needs --note TEXT", error="invalid_argument", option="--note")
    note = _note(opts["note"])
    with changing() as entries:
        e = _existing(entries, domain, scope)
        e["note"] = note
    done("json" in opts, f"{scope_text(scope)}: note on {domain} saved", **e)


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
        activity.record("allowlist", "allowlist.expired",
                        f"Temporary Allowlist entry {e['domain']}{' for all VMs' if everywhere else ''} expired "
                        f"({when(e['expires'])})", None if everywhere else e["scope"],
                        params={"domain": e["domain"], "scope": e["scope"]}, expired=e["expires"])
    if "json" in opts:
        print(json.dumps({"expired": _order(gone)}, ensure_ascii=False))
        return
    for e in _order(gone):
        print(f"{scope_text(e['scope'])}: {e['domain']} expired")


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
        added = [e for e in found if not find(entries, e["domain"], e["scope"])]
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
            "migrate-allowlist": migrate}
